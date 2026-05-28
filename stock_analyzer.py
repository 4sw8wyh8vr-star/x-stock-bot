"""
Use Claude to interpret stock tweets, then pull live data with yfinance.
"""

import json
import logging
from dataclasses import dataclass, field

import anthropic
import yfinance as yf

from config import Config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a financial tweet analyst. Your job is to read a tweet and determine:

1. Whether it discusses specific stocks / equities (not crypto, bonds, or general market commentary).
2. Which ticker symbols are mentioned or clearly implied.
3. The author's apparent signal: BUY, SELL, HOLD, or WATCH (watching/researching, no clear direction).
4. Your confidence in that signal: low, medium, or high.
5. A plain-English summary of what the author is saying.
6. A simple_explanation: a clear, jargon-free breakdown written for someone who is NOT a finance expert.
   - Use a real-world analogy if a technical concept is involved (e.g. "think of it like...")
   - Explain WHY each stock mentioned matters in this context
   - Translate any industry jargon into plain English
   - End with a single "Bottom line:" sentence saying what this means for the investor
   Keep it concise but clear — 3 to 6 short paragraphs max.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{
  "is_stock_related": true | false,
  "tickers": ["AAPL", "NVDA"],
  "signal": "BUY" | "SELL" | "HOLD" | "WATCH" | "NONE",
  "confidence": "low" | "medium" | "high",
  "summary": "1-2 sentence summary of the author's view.",
  "simple_explanation": "Plain-English breakdown with analogy and bottom line."
}

Rules:
- Only include tickers you are highly confident about. Do not guess.
- If the tweet is ambiguous or just a retweet caption, set is_stock_related to false.
- Never fabricate ticker symbols.
- simple_explanation must always be plain English — no jargon, no ticker symbols without explanation.
"""


@dataclass
class TweetAnalysis:
    is_stock_related: bool = False
    tickers: list[str] = field(default_factory=list)
    signal: str = "NONE"
    confidence: str = "low"
    summary: str = ""
    simple_explanation: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class StockSnapshot:
    ticker: str
    price: float
    change_pct: float
    market_cap_b: float | None
    pe_ratio: float | None
    week52_low: float | None
    week52_high: float | None
    company_name: str


class StockAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def analyze_tweet(self, text: str) -> TweetAnalysis:
        """Ask Claude to parse the tweet and return structured analysis."""
        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            raw_json = message.content[0].text.strip()
            data = json.loads(raw_json)
            return TweetAnalysis(
                is_stock_related=data.get("is_stock_related", False),
                tickers=data.get("tickers", []),
                signal=data.get("signal", "NONE"),
                confidence=data.get("confidence", "low"),
                summary=data.get("summary", ""),
                simple_explanation=data.get("simple_explanation", ""),
                raw=data,
            )
        except (json.JSONDecodeError, KeyError, anthropic.APIError) as e:
            logger.error(f"Analysis failed: {e}")
            return TweetAnalysis()

    def get_stock_snapshots(self, tickers: list[str]) -> dict[str, StockSnapshot]:
        """Fetch live price + fundamentals for each ticker using yfinance."""
        results: dict[str, StockSnapshot] = {}
        for ticker in tickers:
            try:
                info = yf.Ticker(ticker).fast_info
                full_info = yf.Ticker(ticker).info  # slightly slower but has PE etc.
                price = full_info.get("currentPrice") or full_info.get("regularMarketPrice", 0.0)
                prev_close = full_info.get("previousClose") or full_info.get("regularMarketPreviousClose", price)
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
                mkt_cap = full_info.get("marketCap")
                results[ticker] = StockSnapshot(
                    ticker=ticker,
                    price=price,
                    change_pct=change_pct,
                    market_cap_b=round(mkt_cap / 1e9, 1) if mkt_cap else None,
                    pe_ratio=full_info.get("trailingPE"),
                    week52_low=full_info.get("fiftyTwoWeekLow"),
                    week52_high=full_info.get("fiftyTwoWeekHigh"),
                    company_name=full_info.get("shortName", ticker),
                )
            except Exception as e:
                logger.warning(f"Could not fetch data for {ticker}: {e}")
        return results
