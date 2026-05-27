"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

# Always load .env from the project directory, regardless of working directory
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_FILE, override=True)


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required env var: {key}")
    return value


class Config:
    # The account you want to MONITOR (no @)
    X_USERNAME: str = _require("X_USERNAME").lstrip("@")

    # Anthropic
    ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")

    # Tuning
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))
    MIN_CONFIDENCE: str = os.getenv("MIN_CONFIDENCE", "medium").lower()

    CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

    def min_confidence_rank(self) -> int:
        return self.CONFIDENCE_ORDER.get(self.MIN_CONFIDENCE, 1)
