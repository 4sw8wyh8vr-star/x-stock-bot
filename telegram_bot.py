"""Send Telegram messages via the Bot API (HTML parse mode)."""

import html
import logging

import httpx

from config import Config

logger = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "BUY":   "🟢",
    "SELL":  "🔴",
    "HOLD":  "🟡",
    "WATCH": "👁️",
    "NONE":  "⬜",
}

def _e(text: str) -> str:
    """Escape a string for Telegram HTML mode."""
    return html.escape(str(text))


class TelegramBot:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def send(self, text: str) -> bool:
        """Send an HTML-formatted message. Returns True on success."""
        try:
            resp = httpx.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            if not resp.is_success:
                logger.error(
                    "Telegram send failed HTTP %d: %s", resp.status_code, resp.text
                )
                return False
            return True
        except httpx.HTTPError as e:
            logger.error("Telegram send failed: %s", e)
            return False


def build_message(tweet, analysis, snapshots: dict) -> str:
    """Compose the Telegram message from tweet + analysis + stock data (HTML)."""
    signal_emoji = SIGNAL_EMOJI.get(analysis.signal, "⬜")
    confidence_bar = {"low": "▓░░", "medium": "▓▓░", "high": "▓▓▓"}.get(analysis.confidence, "░░░")

    lines = [
        f"<b>── NEW POST FROM @{_e(tweet.author)} ──</b>",
        "",
        f"<i>{_e(tweet.text)}</i>",
        "",
        "",
        f"<b>── SIGNAL ──</b>",
        f"{signal_emoji} {_e(analysis.signal)}   {confidence_bar} {_e(analysis.confidence).upper()} CONFIDENCE",
    ]

    # Plain-English breakdown
    if analysis.simple_explanation:
        lines += [
            "",
            "",
            "<b>── WHAT THIS MEANS ──</b>",
            _e(analysis.simple_explanation),
        ]
    elif analysis.summary:
        lines += [
            "",
            "",
            "<b>── SUMMARY ──</b>",
            _e(analysis.summary),
        ]

    # Live stock data
    if snapshots:
        lines += ["", "", "<b>── LIVE PRICES ──</b>"]
        for ticker, snap in snapshots.items():
            direction = "▲" if snap.change_pct >= 0 else "▼"
            change_str = f"{direction}{abs(snap.change_pct):.2f}%"
            lines.append(f"<b>${_e(ticker)}</b>  {_e(snap.company_name)}")
            lines.append(f"${snap.price:.2f}  {change_str}")

            # Fundamentals row
            fund = []
            if snap.market_cap_b:
                fund.append(f"Mkt cap ${_e(snap.market_cap_b)}B")
            if snap.week52_low and snap.week52_high:
                fund.append(f"52w ${snap.week52_low:.2f} – ${snap.week52_high:.2f}")
            if fund:
                lines.append("  " + "   ·   ".join(fund))

            # Technical signals row
            tech = []
            if snap.rel_volume is not None:
                vol_flag = " 🔥" if snap.rel_volume >= 2.0 else ""
                tech.append(f"Volume {snap.rel_volume}x avg{vol_flag}")
            if snap.ma_50 is not None and snap.above_50ma is not None:
                ma_label = "above" if snap.above_50ma else "below"
                ma_emoji = "✅" if snap.above_50ma else "⚠️"
                tech.append(f"{ma_emoji} {ma_label} 50-day MA (${snap.ma_50:.2f})")
            if tech:
                lines.append("  " + "   ·   ".join(tech))
            lines.append("")  # spacing between tickers

    lines += ["", "", f'<a href="{_e(tweet.url)}">View original post →</a>']
    return "\n".join(lines)
