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
            lines.append(f"<b>${_e(ticker)}</b> — ${snap.price:.2f}  {change_str}")
            details = []
            if snap.market_cap_b:
                details.append(f"Mkt cap ${_e(snap.market_cap_b)}B")
            if snap.pe_ratio:
                details.append(f"P/E {snap.pe_ratio:.1f}")
            if snap.week52_low and snap.week52_high:
                details.append(f"52w low ${snap.week52_low:.2f} / high ${snap.week52_high:.2f}")
            if details:
                lines.append("  " + "   ·   ".join(details))

    lines += ["", "", f'<a href="{_e(tweet.url)}">View original post →</a>']
    return "\n".join(lines)
