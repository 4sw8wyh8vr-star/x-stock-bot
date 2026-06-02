"""
Sends two daily digests:
  - 9:15 AM ET  (pre-market): top 5 holdings + top 5 watchlist to watch today
  - 4:05 PM ET  (post-close): top 5 holdings + top 5 watchlist final recap
"""

import asyncio
import html
import logging
from datetime import date, datetime, time

import anthropic
import httpx
import pytz

from config import Config
from portfolio_store import PortfolioStore

logger = logging.getLogger(__name__)

MARKET_TZ = pytz.timezone("US/Eastern")
MORNING_TIME = time(9, 15)
CLOSE_TIME   = time(16, 5)

VOL_SPIKE    = 2.0
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 70


def _e(text) -> str:
    return html.escape(str(text))


def _score(s: dict, h=None) -> float:
    """Higher score = more worth paying attention to."""
    score = 0.0
    if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
        score += s["rel_volume"] * 3
    if s.get("rsi"):
        if s["rsi"] <= RSI_OVERSOLD:
            score += (RSI_OVERSOLD - s["rsi"]) * 0.5
        elif s["rsi"] >= RSI_OVERBOUGHT:
            score += (s["rsi"] - RSI_OVERBOUGHT) * 0.5
    score += abs(s.get("change_pct", 0)) * 2
    if s.get("above_50ma") is False:
        score += 5
    if h and h.avg_cost:
        open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
        if open_pct <= -15:
            score += 8
    return score


def _format_row(ticker: str, s: dict, h=None, mode: str = "morning") -> str:
    day_dir = "▲" if s["change_pct"] >= 0 else "▼"
    line = f"<b>${_e(ticker)}</b>  {_e(s['company'])}\n"
    line += f"  ${s['price']:.2f}  {day_dir}{abs(s['change_pct']):.1f}%"

    if h and h.avg_cost:
        open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
        op_sign = "+" if open_pct >= 0 else ""
        line += f"   Open P&amp;L: {op_sign}{open_pct:.1f}%"

    signals = []
    if s.get("rel_volume") and s["rel_volume"] >= VOL_SPIKE:
        signals.append(f"🔥 Vol {s['rel_volume']}x")
    if s.get("rsi") and s["rsi"] <= RSI_OVERSOLD:
        signals.append(f"📉 RSI {s['rsi']} oversold")
    if s.get("rsi") and s["rsi"] >= RSI_OVERBOUGHT:
        signals.append(f"⚠️ RSI {s['rsi']} overbought")
    if s.get("above_50ma") is False:
        signals.append("⚠️ below 50MA")
    if s.get("obv_trend") and "Rising" in s.get("obv_trend", ""):
        signals.append("OBV ↑")
    elif s.get("obv_trend") and "Falling" in s.get("obv_trend", ""):
        signals.append("OBV ↓")

    if signals:
        line += "\n  " + "   ·   ".join(signals)
    return line


class ScheduledDigest:
    def __init__(self, config: Config, store: PortfolioStore, portfolio_monitor):
        self.config = config
        self.store = store
        self.pm = portfolio_monitor
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self.claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._sent_morning: date | None = None
        self._sent_close: date | None = None

    async def run(self) -> None:
        logger.info("Scheduled digest running — morning at 9:15 ET, close at 4:05 ET")
        while True:
            try:
                await self._check_and_send()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Scheduled digest error: %s", e)
            await asyncio.sleep(60)

    async def _check_and_send(self) -> None:
        now = datetime.now(MARKET_TZ)
        if now.weekday() >= 5:
            return
        today = now.date()
        t = now.time()

        if MORNING_TIME <= t < time(9, 17) and self._sent_morning != today:
            logger.info("Sending morning digest")
            await self._send_digest(mode="morning")
            self._sent_morning = today

        if CLOSE_TIME <= t < time(16, 7) and self._sent_close != today:
            logger.info("Sending close digest")
            await self._send_digest(mode="close")
            self._sent_close = today

    async def _get_assessments(self, stocks: list[tuple], mode: str) -> dict[str, str]:
        """Ask Claude for a 2-sentence assessment of each stock's setup."""
        if not stocks:
            return {}

        context_lines = []
        for item in stocks:
            ticker = item[0]
            s = item[1]
            h = item[2] if len(item) > 2 else None

            open_pl_str = ""
            if h and h.avg_cost and s.get("price"):
                open_pct = (s["price"] - h.avg_cost) / h.avg_cost * 100
                open_pl_str = f", open P&L {open_pct:+.1f}% from avg ${h.avg_cost:.2f}"

            signals = []
            if s.get("rel_volume"):
                signals.append(f"volume {s['rel_volume']}x average")
            if s.get("rsi"):
                signals.append(f"RSI {s['rsi']}")
            if s.get("above_50ma") is not None:
                signals.append("above 50MA" if s["above_50ma"] else "below 50MA")
            if s.get("obv_trend"):
                signals.append(f"OBV {s['obv_trend']}")
            if s.get("net_volume") is not None:
                nv = s["net_volume"]
                signals.append(f"5-day net volume {'+' if nv >= 0 else ''}{nv:,}")

            context_lines.append(
                f"{ticker} ({s.get('company', ticker)}): "
                f"${s.get('price', 0):.2f}, {'+' if s.get('change_pct', 0) >= 0 else ''}"
                f"{s.get('change_pct', 0):.1f}% today{open_pl_str}. "
                f"Signals: {', '.join(signals) if signals else 'none notable'}"
            )

        if mode == "morning":
            system = (
                "You are giving Adrien, a non-technical investor, his pre-market morning brief. "
                "For each stock listed, write exactly 2 plain-English sentences: "
                "1) What the pre-market setup or signals mean. "
                "2) What specifically to watch for or be aware of when the market opens. "
                "Be direct — give a real opinion, not generic commentary. "
                "No jargon without explanation. No markdown symbols. No bullet points. "
                "Format your response as: TICKER: [your 2 sentences]. One stock per line."
            )
        else:
            system = (
                "You are giving Adrien, a non-technical investor, his end-of-day recap. "
                "For each stock listed, write exactly 2 plain-English sentences: "
                "1) What today's price action and volume signals tell you about whether this is accumulation, distribution, or noise. "
                "2) What it means for tomorrow — hold, watch for continuation, or be cautious. "
                "Be direct. No jargon without explanation. No markdown. No bullet points. "
                "Format your response as: TICKER: [your 2 sentences]. One stock per line."
            )

        user_msg = "\n".join(context_lines)

        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
            assessments = {}
            for line in raw.split("\n"):
                if ":" in line:
                    ticker_part, _, text = line.partition(":")
                    t = ticker_part.strip().upper()
                    assessments[t] = text.strip()
            return assessments
        except Exception as e:
            logger.error("Claude assessment failed: %s", e)
            return {}

    async def _send_digest(self, mode: str) -> None:
        holdings = self.store.get_holdings()
        watchlist = self.store.get_watchlist()

        if mode == "morning":
            header_h = "── 🌅 MORNING BRIEF: YOUR HOLDINGS ──"
            header_w = "── 🌅 MORNING BRIEF: WATCHLIST ──"
            subheader = "Top 5 to watch before the open"
        else:
            header_h = "── 🔔 CLOSING RECAP: YOUR HOLDINGS ──"
            header_w = "── 🔔 CLOSING RECAP: WATCHLIST ──"
            subheader = "Top 5 movers at close"

        # ── Holdings digest ──
        holding_signals = []
        for ticker, h in holdings.items():
            if self.store.is_muted(ticker):
                continue
            s = await asyncio.to_thread(self.pm._fetch_signals, ticker)
            if s:
                holding_signals.append((ticker, s, h))

        holding_signals.sort(key=lambda x: _score(x[1], x[2]), reverse=True)
        top_holdings = holding_signals[:5]

        assessments_h = await self._get_assessments(top_holdings, mode)

        if top_holdings:
            lines = [f"<b>{_e(header_h)}</b>", f"<i>{subheader}</i>", ""]
            for ticker, s, h in top_holdings:
                lines.append(_format_row(ticker, s, h, mode))
                if ticker in assessments_h:
                    lines.append(f"  <i>{_e(assessments_h[ticker])}</i>")
                lines.append("")
            await self._send("\n".join(lines))
        else:
            await self._send(f"<b>{_e(header_h)}</b>\n\nNo holdings data available.")

        await asyncio.sleep(2)

        # ── Watchlist digest ──
        holding_keys = set(holdings.keys())
        watch_signals = []
        for ticker in watchlist:
            if ticker in holding_keys:
                continue
            s = await asyncio.to_thread(self.pm._fetch_signals, ticker)
            if s:
                watch_signals.append((ticker, s))

        watch_signals.sort(key=lambda x: _score(x[1]), reverse=True)
        top_watch = watch_signals[:5]

        assessments_w = await self._get_assessments(
            [(t, s) for t, s in top_watch], mode
        )

        if top_watch:
            lines = [f"<b>{_e(header_w)}</b>", f"<i>{subheader}</i>", ""]
            for ticker, s in top_watch:
                lines.append(_format_row(ticker, s, mode=mode))
                if ticker in assessments_w:
                    lines.append(f"  <i>{_e(assessments_w[ticker])}</i>")
                lines.append("")
            await self._send("\n".join(lines))
        else:
            await self._send(f"<b>{_e(header_w)}</b>\n\nNo watchlist data available.")

    async def _send(self, text: str) -> None:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.config.TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10.0,
                )
                if not resp.is_success:
                    logger.error("Digest send failed: %s", resp.text)
            except Exception as e:
                logger.error("Digest send error: %s", e)
