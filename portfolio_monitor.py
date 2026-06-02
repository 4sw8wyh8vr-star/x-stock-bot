"""
Monitors the user's watchlist and holdings for technical signals.
Runs every 15 minutes during market hours and sends Telegram alerts
when meaningful signals fire on any ticker.
"""

import asyncio
import html
import logging
from datetime import datetime, time

import anthropic
import httpx
import pytz
import yfinance as yf

from config import Config
from portfolio_store import PortfolioStore

logger = logging.getLogger(__name__)

MARKET_TZ = pytz.timezone("US/Eastern")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
SCAN_INTERVAL = 15 * 60   # seconds between scans

# Signal thresholds
VOL_SPIKE = 2.0       # relative volume considered high
RSI_OVERSOLD = 35     # below this = potential buy opportunity
RSI_OVERBOUGHT = 70   # above this = overbought warning

ASSESSMENT_PROMPT = """\
You are a stock analyst giving Adrien, a non-technical investor, a quick plain-English \
read on whether a stock showing technical signals is worth acting on right now.

You will be given a stock's current signals. Your job is to synthesize them into \
a clear, direct verdict using this exact structure:

Paragraph 1 — what the signals are saying in plain English (no jargon)
Paragraph 2 — whether this looks like a good entry, a wait, or a pass — and why
End with: "Bottom line: ..." — one sentence verdict

Rules:
- No markdown, no bullet points, no # or * symbols
- Plain paragraphs only
- Be direct — give a real opinion
- Keep it to 3 to 5 short sentences total
"""


def _calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not losses:
        return 100.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _is_market_hours() -> bool:
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _e(text) -> str:
    return html.escape(str(text))


class PortfolioMonitor:
    def __init__(self, config: Config, store: PortfolioStore):
        self.config = config
        self.store = store
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        logger.info("Portfolio monitor started")
        while True:
            try:
                tickers = self.store.all_tickers()
                if tickers and _is_market_hours():
                    await self._scan_all(tickers)
                elif tickers:
                    logger.debug("Market closed — skipping portfolio scan")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Portfolio monitor error: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    # ------------------------------------------------------------------ #
    #  Scanning                                                            #
    # ------------------------------------------------------------------ #

    async def _scan_all(self, tickers: list[str]) -> None:
        logger.info("Scanning %d portfolio tickers: %s", len(tickers), ", ".join(tickers))
        for ticker in tickers:
            try:
                signals = await asyncio.to_thread(self._fetch_signals, ticker)
                if signals and self._should_alert(signals):
                    assessment = await self._assess(ticker, signals)
                    await self._send_alert(ticker, signals, assessment)
            except Exception as e:
                logger.warning("Failed to scan %s: %s", ticker, e)

    def _fetch_signals(self, ticker: str) -> dict | None:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            hist = t.history(period="3mo")

            if hist.empty:
                return None

            closes = hist["Close"].tolist()
            price = closes[-1] if closes else 0.0
            prev_close = info.get("previousClose") or (closes[-2] if len(closes) > 1 else price)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0

            vol_today = info.get("volume") or info.get("regularMarketVolume")
            vol_avg = info.get("averageVolume")
            rel_volume = round(vol_today / vol_avg, 1) if vol_today and vol_avg else None

            ma_50 = info.get("fiftyDayAverage")
            above_50ma = (price > ma_50) if (price and ma_50) else None

            week52_high = info.get("fiftyTwoWeekHigh")
            near_52w_high = (price >= week52_high * 0.98) if (price and week52_high) else False

            rsi = _calculate_rsi(closes)

            return {
                "ticker": ticker,
                "company": info.get("shortName", ticker),
                "price": price,
                "change_pct": change_pct,
                "rel_volume": rel_volume,
                "ma_50": ma_50,
                "above_50ma": above_50ma,
                "rsi": rsi,
                "week52_high": week52_high,
                "near_52w_high": near_52w_high,
                "market_cap_b": round(info["marketCap"] / 1e9, 2) if info.get("marketCap") else None,
            }
        except Exception as e:
            logger.warning("Signal fetch failed for %s: %s", ticker, e)
            return None

    def _should_alert(self, s: dict) -> bool:
        """Only fire an alert if at least one meaningful signal is present."""
        if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
            return True
        if s.get("rsi") and (s["rsi"] <= RSI_OVERSOLD or s["rsi"] >= RSI_OVERBOUGHT):
            return True
        if s.get("near_52w_high"):
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Claude assessment                                                   #
    # ------------------------------------------------------------------ #

    async def _assess(self, ticker: str, s: dict) -> str:
        signals_text = f"""
Stock: {s['company']} ({ticker})
Price: ${s['price']:.2f} ({'+' if s['change_pct'] >= 0 else ''}{s['change_pct']:.1f}% today)
Relative volume: {s['rel_volume']}x average ({"HIGH — unusual activity" if s.get('rel_volume', 0) >= VOL_SPIKE else "normal"})
RSI: {s['rsi']} ({"OVERSOLD — potential bounce" if s['rsi'] and s['rsi'] <= RSI_OVERSOLD else "OVERBOUGHT — caution" if s['rsi'] and s['rsi'] >= RSI_OVERBOUGHT else "neutral"})
50-day MA: ${s['ma_50']:.2f} — price is {"ABOVE" if s.get('above_50ma') else "BELOW"} it
Near 52-week high: {"YES" if s.get('near_52w_high') else "no"}
""".strip()

        try:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=400,
                system=ASSESSMENT_PROMPT,
                messages=[{"role": "user", "content": signals_text}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error("Claude assessment failed: %s", e)
            return ""

    # ------------------------------------------------------------------ #
    #  Telegram alert                                                      #
    # ------------------------------------------------------------------ #

    async def _send_alert(self, ticker: str, s: dict, assessment: str) -> None:
        direction = "▲" if s["change_pct"] >= 0 else "▼"

        # Build triggered signals list
        fired = []
        if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
            fired.append(f"🔥 Volume {s['rel_volume']}x above average")
        if s.get("rsi"):
            if s["rsi"] <= RSI_OVERSOLD:
                fired.append(f"📉 RSI {s['rsi']} — oversold (potential bounce)")
            elif s["rsi"] >= RSI_OVERBOUGHT:
                fired.append(f"📈 RSI {s['rsi']} — overbought (caution)")
        if s.get("above_50ma") is not None:
            ma_label = "✅ above" if s["above_50ma"] else "⚠️ below"
            fired.append(f"{ma_label} 50-day MA (${s['ma_50']:.2f})")
        if s.get("near_52w_high"):
            fired.append(f"🏆 Near 52-week high (${s['week52_high']:.2f})")

        # Check if this is a holding
        holdings = self.store.get_holdings()
        holding_line = ""
        if ticker in holdings:
            h = holdings[ticker]
            holding_value = h.shares * s["price"]
            holding_line = f"\n{h.shares} shares  ·  Value: ${holding_value:,.0f}"
            if h.avg_cost:
                pl = (s["price"] - h.avg_cost) * h.shares
                pl_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                pl_sign = "+" if pl >= 0 else ""
                holding_line += f"\nAvg cost ${h.avg_cost:.2f}  ·  P&amp;L {pl_sign}${pl:,.0f} ({pl_sign}{pl_pct:.1f}%)"

        lines = [
            f"<b>── SIGNAL ALERT: ${_e(ticker)} ──</b>",
            "",
            f"<b>{_e(s['company'])}</b>",
            f"${s['price']:.2f}  {direction}{abs(s['change_pct']):.1f}%{holding_line}",
            "",
            "",
            "<b>── SIGNALS ──</b>",
            "\n".join(fired),
        ]

        if assessment:
            lines += [
                "",
                "",
                "<b>── CLAUDE'S TAKE ──</b>",
                _e(assessment),
            ]

        msg = "\n".join(lines)

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.config.TELEGRAM_CHAT_ID,
                        "text": msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10.0,
                )
                if resp.is_success:
                    logger.info("Alert sent for %s", ticker)
                else:
                    logger.error("Telegram alert failed for %s: %s", ticker, resp.text)
            except Exception as e:
                logger.error("Failed to send alert for %s: %s", ticker, e)

    # ------------------------------------------------------------------ #
    #  On-demand portfolio snapshot (called from chat handler)             #
    # ------------------------------------------------------------------ #

    async def snapshot(self) -> str:
        """Return a formatted portfolio + watchlist summary for /portfolio command."""
        holdings = self.store.get_holdings()
        watchlist = self.store.get_watchlist()

        if not holdings and not watchlist:
            return (
                "You have no holdings or watchlist set up yet.\n\n"
                "Use /holding NVDA 50 to add a holding or /watch NVDA to add to your watchlist."
            )

        lines = []
        total_value = 0.0

        if holdings:
            lines.append("<b>── YOUR HOLDINGS ──</b>")
            lines.append("")
            for ticker, h in holdings.items():
                s = await asyncio.to_thread(self._fetch_signals, ticker)
                if not s:
                    lines.append(f"<b>${_e(ticker)}</b> — could not fetch data")
                    continue

                direction = "▲" if s["change_pct"] >= 0 else "▼"
                value = h.shares * s["price"]
                total_value += value

                lines.append(f"<b>${_e(ticker)}</b>  {_e(s['company'])}")
                lines.append(f"${s['price']:.2f}  {direction}{abs(s['change_pct']):.1f}%")
                lines.append(f"{h.shares} shares  ·  Value: ${value:,.0f}")

                if h.avg_cost:
                    pl = (s["price"] - h.avg_cost) * h.shares
                    pl_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                    sign = "+" if pl >= 0 else ""
                    lines.append(f"Avg cost ${h.avg_cost:.2f}  ·  P&amp;L {sign}${pl:,.0f} ({sign}{pl_pct:.1f}%)")

                tech = []
                if s.get("rel_volume"):
                    vol_flag = " 🔥" if s["rel_volume"] >= VOL_SPIKE else ""
                    tech.append(f"Vol {s['rel_volume']}x{vol_flag}")
                if s.get("rsi"):
                    tech.append(f"RSI {s['rsi']}")
                if s.get("above_50ma") is not None:
                    tech.append("✅ above 50MA" if s["above_50ma"] else "⚠️ below 50MA")
                if tech:
                    lines.append("  " + "   ·   ".join(tech))
                lines.append("")

            if total_value:
                lines.append(f"<b>Total portfolio value: ${total_value:,.0f}</b>")
                lines.append("")

        if watchlist:
            lines.append("")
            lines.append("<b>── WATCHLIST ──</b>")
            lines.append("")
            for ticker in watchlist:
                s = await asyncio.to_thread(self._fetch_signals, ticker)
                if not s:
                    lines.append(f"<b>${_e(ticker)}</b> — could not fetch")
                    continue

                direction = "▲" if s["change_pct"] >= 0 else "▼"
                lines.append(f"<b>${_e(ticker)}</b>  {_e(s['company'])}")
                lines.append(f"${s['price']:.2f}  {direction}{abs(s['change_pct']):.1f}%")

                tech = []
                if s.get("rel_volume"):
                    vol_flag = " 🔥" if s["rel_volume"] >= VOL_SPIKE else ""
                    tech.append(f"Vol {s['rel_volume']}x{vol_flag}")
                if s.get("rsi"):
                    tech.append(f"RSI {s['rsi']}")
                if s.get("above_50ma") is not None:
                    tech.append("✅ above 50MA" if s["above_50ma"] else "⚠️ below 50MA")
                if tech:
                    lines.append("  " + "   ·   ".join(tech))
                lines.append("")

        return "\n".join(lines)
