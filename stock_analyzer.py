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
You are a financial tweet analyst working for Adrien, a non-technical investor \
who wants to understand WHY a stock is positioning to be a smart buy — not just \
what it does.

Your job is to read a tweet and produce a JSON response with these fields:

1. is_stock_related — true if the tweet discusses specific stocks or equities.
2. tickers — ticker symbols mentioned or clearly implied. Only include ones you are certain about.
3. signal — the author's apparent direction: BUY, SELL, HOLD, WATCH, or NONE.
4. confidence — your confidence in that signal: low, medium, or high.
5. summary — 1 to 2 plain sentences summarising the author's view.
6. simple_explanation — this is the most important field. It must be clearly split \
   into two labelled sections so Adrien always knows whose opinion is whose:

   SECTION 1 — label it exactly "Serenity's take:" \
   What is she actually saying or signaling in this tweet? What does she believe about \
   this stock? Is she bullish, cautious, or just flagging something? Quote or closely \
   paraphrase her key point. 2 to 3 sentences only — strictly what she said, not your view.

   SECTION 2 — label it exactly "Claude's assessment:" \
   Now give YOUR independent view. Use a plain-English analogy to explain the theme if \
   needed. Say whether the setup looks compelling or not and why. If you see risks she \
   did not mention, say so. If you disagree with her framing, say so. \
   End with: "Bottom line: ..." — one sentence verdict.

   Rules for simple_explanation:
   - Plain paragraphs only. No bullet points, no dashes, no markdown, no # or * symbols.
   - Translate every piece of jargon immediately when you use it.
   - Never use a ticker symbol without first saying what the company does in plain English.
   - Keep each section to 3 to 4 sentences.

Respond ONLY with a valid JSON object — no markdown fences, no extra text:
{
  "is_stock_related": true | false,
  "tickers": ["AAPL", "NVDA"],
  "signal": "BUY" | "SELL" | "HOLD" | "WATCH" | "NONE",
  "confidence": "low" | "medium" | "high",
  "summary": "1-2 sentence summary.",
  "simple_explanation": "Multi-paragraph plain-English investment thesis."
}

If the tweet is not stock related, set is_stock_related to false and leave simple_explanation empty.
Never fabricate ticker symbols.
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
    company_name: str
    price: float
    change_pct: float
    market_cap_b: float | None
    pe_ratio: float | None
    week52_low: float | None
    week52_high: float | None
    # New technical signals
    rel_volume: float | None        # today's volume ÷ 30-day average (e.g. 2.4 = 2.4x normal)
    ma_50: float | None             # 50-day moving average price
    above_50ma: bool | None         # is price currently above the 50-day MA?


class StockAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def analyze_tweet(self, text: str) -> TweetAnalysis:
        """Ask Claude to parse the tweet and return structured analysis."""
        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
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
        """Fetch live price, fundamentals, and technical signals for each ticker."""
        results: dict[str, StockSnapshot] = {}
        for ticker in tickers:
            try:
                t = yf.Ticker(ticker)
                info = t.info

                price = info.get("currentPrice") or info.get("regularMarketPrice", 0.0)
                prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose", price)
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
                mkt_cap = info.get("marketCap")

                # --- Technical signals ---
                # Relative volume: today vs 30-day average
                vol_today = info.get("volume") or info.get("regularMarketVolume")
                vol_avg = info.get("averageVolume")  # ~3-month average from yfinance
                rel_volume = round(vol_today / vol_avg, 1) if vol_today and vol_avg else None

                # 50-day moving average
                ma_50 = info.get("fiftyDayAverage")
                above_50ma = (price > ma_50) if (price and ma_50) else None

                results[ticker] = StockSnapshot(
                    ticker=ticker,
                    company_name=info.get("shortName", ticker),
                    price=price,
                    change_pct=change_pct,
                    market_cap_b=round(mkt_cap / 1e9, 2) if mkt_cap else None,
                    pe_ratio=info.get("trailingPE"),
                    week52_low=info.get("fiftyTwoWeekLow"),
                    week52_high=info.get("fiftyTwoWeekHigh"),
                    rel_volume=rel_volume,
                    ma_50=ma_50,
                    above_50ma=above_50ma,
                )
            except Exception as e:
                logger.warning(f"Could not fetch data for {ticker}: {e}")
        return results
