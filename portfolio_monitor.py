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
                # Only auto-scan holdings — watchlist is on-demand via /portfolio
                tickers = list(self.store.get_holdings().keys())
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

            # ── Net Volume (approximation from OHLC) ──────────────────
            # Estimates buying vs selling pressure using today's price range.
            # Net vol > 0 = more buying; < 0 = more selling.
            net_volume = None
            if not hist.empty and vol_today:
                today = hist.iloc[-1]
                h, l, c = today["High"], today["Low"], today["Close"]
                if h != l:
                    buy_vol = vol_today * (c - l) / (h - l)
                    sell_vol = vol_today * (h - c) / (h - l)
                    net_volume = int(buy_vol - sell_vol)

            # ── OBV (On-Balance Volume) ───────────────────────────────
            # Cumulative indicator: rising OBV = smart money accumulating,
            # falling OBV = distribution. We report the 10-day trend.
            obv = None
            obv_trend = None
            if len(hist) >= 10:
                obv_series = []
                running = 0
                prev_c = None
                for _, row in hist.iterrows():
                    if prev_c is not None:
                        if row["Close"] > prev_c:
                            running += row["Volume"]
                        elif row["Close"] < prev_c:
                            running -= row["Volume"]
                    obv_series.append(running)
                    prev_c = row["Close"]
                obv = obv_series[-1]
                obv_10d_ago = obv_series[-10]
                if obv > obv_10d_ago * 1.02:
                    obv_trend = "Rising ↑ (accumulation)"
                elif obv < obv_10d_ago * 0.98:
                    obv_trend = "Falling ↓ (distribution)"
                else:
                    obv_trend = "Flat →"

            return {
                "ticker": ticker,
                "company": info.get("shortName", ticker),
                "price": price,
                "change_pct": change_pct,
                "rel_volume": rel_volume,
                "net_volume": net_volume,
                "obv_trend": obv_trend,
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
        if self.store.is_muted(s["ticker"]):
            return False
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

        # Check if this is a holding — show full P&L context
        holdings = self.store.get_holdings()
        holding_line = ""
        if ticker in holdings:
            h = holdings[ticker]
            value = h.shares * s["price"]
            day_pl = (s["change_pct"] / 100) * s["price"] * h.shares
            day_sign = "+" if day_pl >= 0 else ""
            holding_line = f"\n{h.shares} shares  ·  Value: ${value:,.2f}"
            holding_line += f"\nDay P&amp;L: {day_sign}${day_pl:,.2f} ({day_sign}{s['change_pct']:.2f}%)"
            if h.avg_cost:
                open_pl = (s["price"] - h.avg_cost) * h.shares
                open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                op_sign = "+" if open_pl >= 0 else ""
                holding_line += f"\nOpen P&amp;L: {op_sign}${open_pl:,.2f} ({op_sign}{open_pct:.2f}%)  Avg: ${h.avg_cost:.2f}"

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
        """Return a focused digest — summary + only what needs attention."""
        holdings = self.store.get_holdings()
        watchlist = self.store.get_watchlist()

        if not holdings and not watchlist:
            return (
                "Nothing set up yet.\n\n"
                "Use /holding NVDA 50 820 to add a holding\n"
                "or /watch NVDA to add to your watchlist.\n\n"
                "Use /detail NVDA to drill into any stock."
            )

        # Fetch all signals concurrently
        all_tickers = list(holdings.keys()) + [t for t in watchlist if t not in holdings]
        signals: dict[str, dict] = {}
        for ticker in all_tickers:
            s = await asyncio.to_thread(self._fetch_signals, ticker)
            if s:
                signals[ticker] = s

        # ── Portfolio-level totals ──
        total_value = 0.0
        total_open_pl = 0.0
        total_day_pl = 0.0
        for ticker, h in holdings.items():
            s = signals.get(ticker)
            if not s or not s["price"]:
                continue
            value = h.shares * s["price"]
            total_value += value
            total_day_pl += (s["change_pct"] / 100) * value
            if h.avg_cost:
                total_open_pl += (s["price"] - h.avg_cost) * h.shares

        # ── Categorise holdings ──
        flagged = []      # needs attention
        top_movers = []   # significant day moves
        clean = []        # nothing notable

        muted_tickers = []
        for ticker, h in holdings.items():
            s = signals.get(ticker)
            if not s:
                continue

            # Muted = long-term hold, skip alerts but count in totals
            if self.store.is_muted(ticker):
                muted_tickers.append(ticker)
                continue

            flags = []
            if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
                flags.append(f"🔥 Vol {s['rel_volume']}x")
            if s.get("rsi") and s["rsi"] <= RSI_OVERSOLD:
                flags.append(f"📉 RSI {s['rsi']} oversold")
            if s.get("rsi") and s["rsi"] >= RSI_OVERBOUGHT:
                flags.append(f"⚠️ RSI {s['rsi']} overbought")
            if s.get("above_50ma") is False:
                flags.append("⚠️ below 50MA")
            if h.avg_cost and ((s["price"] - h.avg_cost) / h.avg_cost * 100) <= -20:
                open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                flags.append(f"🔴 down {open_pct:.0f}% from cost")

            day_dir = "▲" if s["change_pct"] >= 0 else "▼"
            summary = f"<b>${_e(ticker)}</b>  {day_dir}{abs(s['change_pct']):.1f}%"
            if flags:
                flagged.append((ticker, summary, flags, s))
            elif abs(s["change_pct"]) >= 2.5:
                top_movers.append((ticker, s))
            else:
                clean.append(ticker)

        # Sort movers biggest first
        top_movers.sort(key=lambda x: abs(x[1]["change_pct"]), reverse=True)

        lines = []

        # ── Summary header ──
        lines += ["<b>── PORTFOLIO SUMMARY ──</b>", ""]
        if total_value:
            lines.append(f"Total value:  <b>${total_value:,.0f}</b>")
        day_sign = "+" if total_day_pl >= 0 else ""
        open_sign = "+" if total_open_pl >= 0 else ""
        lines.append(f"Day P&amp;L:    {day_sign}${total_day_pl:,.0f}")
        lines.append(f"Open P&amp;L:   {open_sign}${total_open_pl:,.0f}")

        # ── Flagged ──
        if flagged:
            lines += ["", "", "<b>── NEEDS ATTENTION ──</b>", ""]
            for ticker, summary, flags, s in flagged:
                h = holdings.get(ticker)
                open_str = ""
                if h and h.avg_cost:
                    open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                    op_sign = "+" if open_pct >= 0 else ""
                    open_str = f"  ({op_sign}{open_pct:.1f}% from cost)"
                lines.append(f"{summary}{open_str}")
                lines.append("  " + "   ·   ".join(flags))
                lines.append("")

        # ── Notable movers ──
        if top_movers:
            lines += ["", "<b>── MOVING TODAY ──</b>", ""]
            for ticker, s in top_movers[:6]:
                h = holdings.get(ticker)
                day_dir = "▲" if s["change_pct"] >= 0 else "▼"
                day_pl = (s["change_pct"] / 100) * s["price"] * h.shares if h else 0
                pl_sign = "+" if day_pl >= 0 else ""
                lines.append(
                    f"<b>${_e(ticker)}</b>  {day_dir}{abs(s['change_pct']):.1f}%   "
                    f"{pl_sign}${day_pl:,.0f} today"
                )

        # ── Watchlist alerts ──
        watch_flags = []
        for ticker in watchlist:
            s = signals.get(ticker)
            if not s:
                continue
            flags = []
            if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
                flags.append(f"🔥 Vol {s['rel_volume']}x")
            if s.get("rsi") and s["rsi"] <= RSI_OVERSOLD:
                flags.append(f"📉 RSI {s['rsi']} oversold")
            if s.get("rsi") and s["rsi"] >= RSI_OVERBOUGHT:
                flags.append(f"⚠️ RSI {s['rsi']} overbought")
            if flags:
                day_dir = "▲" if s["change_pct"] >= 0 else "▼"
                watch_flags.append((ticker, s, flags))

        if watch_flags:
            lines += ["", "", "<b>── WATCHLIST ALERTS ──</b>", ""]
            for ticker, s, flags in watch_flags:
                day_dir = "▲" if s["change_pct"] >= 0 else "▼"
                lines.append(f"<b>${_e(ticker)}</b>  {day_dir}{abs(s['change_pct']):.1f}%")
                lines.append("  " + "   ·   ".join(flags))
                lines.append("")

        # ── All clear ──
        if clean:
            lines += ["", f"<b>✅ {len(clean)} positions tracking normally</b>"]
            lines.append(f"<i>{', '.join(f'${t}' for t in clean)}</i>")

        # ── Long-term holds (muted) ──
        if muted_tickers:
            lines += ["", f"<b>🔕 {len(muted_tickers)} long-term holds (alerts off)</b>"]
            lines.append(f"<i>{', '.join(f'${t}' for t in muted_tickers)}</i>")
            lines.append("<i>Use /unmute TICKER to re-enable alerts</i>")

        lines += ["", "<i>/detail TICKER — full breakdown on any stock</i>"]

        return "\n".join(lines)

    async def detail(self, ticker: str) -> str:
        """Full breakdown for a single stock."""
        ticker = ticker.upper()
        holdings = self.store.get_holdings()
        s = await asyncio.to_thread(self._fetch_signals, ticker)

        if not s:
            return f"Couldn't fetch data for ${_e(ticker)}. Check the ticker symbol."

        h = holdings.get(ticker)
        day_dir = "▲" if s["change_pct"] >= 0 else "▼"
        lines = [
            f"<b>── ${_e(ticker)}  {_e(s['company'])} ──</b>",
            "",
            f"${s['price']:.2f}  {day_dir}{abs(s['change_pct']):.2f}%",
        ]

        if h:
            value = h.shares * s["price"]
            day_pl = (s["change_pct"] / 100) * value
            day_sign = "+" if day_pl >= 0 else ""
            lines += [
                f"{h.shares} shares  ·  Value: ${value:,.2f}",
                f"Day P&amp;L: {day_sign}${day_pl:,.2f} ({day_sign}{s['change_pct']:.2f}%)",
            ]
            if h.avg_cost:
                open_pl = (s["price"] - h.avg_cost) * h.shares
                open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                op_sign = "+" if open_pl >= 0 else ""
                lines.append(
                    f"Open P&amp;L: {op_sign}${open_pl:,.2f} ({op_sign}{open_pct:.2f}%)  Avg: ${h.avg_cost:.2f}"
                )

        lines += ["", "<b>── TECHNICALS ──</b>"]

        if s.get("rel_volume") is not None:
            vol_flag = "  🔥 unusual activity" if s["rel_volume"] >= VOL_SPIKE else ""
            lines.append(f"Volume:      {s['rel_volume']}x average{vol_flag}")

        if s.get("net_volume") is not None:
            nv = s["net_volume"]
            nv_sign = "+" if nv >= 0 else ""
            nv_label = "buying pressure" if nv >= 0 else "selling pressure"
            lines.append(f"Net volume:  {nv_sign}{nv:,}  ({nv_label})")

        if s.get("obv_trend"):
            lines.append(f"OBV:         {s['obv_trend']}")

        if s.get("rsi") is not None:
            rsi_note = "  📉 oversold" if s["rsi"] <= RSI_OVERSOLD else "  ⚠️ overbought" if s["rsi"] >= RSI_OVERBOUGHT else ""
            lines.append(f"RSI:         {s['rsi']}{rsi_note}")

        if s.get("ma_50") and s.get("above_50ma") is not None:
            ma_label = "✅ above" if s["above_50ma"] else "⚠️ below"
            lines.append(f"50-day MA:   ${s['ma_50']:.2f}  ({ma_label})")

        if s.get("week52_low") and s.get("week52_high"):
            lines.append(f"52w range:   ${s['week52_low']:.2f} – ${s['week52_high']:.2f}")

        if s.get("market_cap_b"):
            lines.append(f"Mkt cap:     ${s['market_cap_b']}B")

        return "\n".join(lines)

    async def send_snapshot(self) -> None:
        """Fetch snapshot and send to Telegram, splitting if needed."""
        msg = await self.snapshot()
        # Split into chunks under Telegram's 4096 char limit
        chunk_size = 3800
        chunks = []
        current = ""
        for line in msg.split("\n"):
            if len(current) + len(line) + 1 > chunk_size:
                chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                header = f"<b>── PORTFOLIO ({i+1}/{len(chunks)}) ──</b>\n\n"
                chunk = header + chunk
            await self._send(chunk)

    async def _send(self, text: str) -> None:
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.config.TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10.0,
                )
            except Exception as e:
                logger.error("Failed to send message: %s", e)
