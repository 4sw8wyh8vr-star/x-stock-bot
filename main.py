"""
X Stock Bot — two concurrent tasks:
  1. Monitor @aleabitoreddit for new tweets → Claude analysis → Telegram alert
  2. Listen for Telegram messages → Claude stock analyst chat (with full post history)
"""

import asyncio
import logging

from config import Config
from stock_analyzer import StockAnalyzer
from telegram_bot import TelegramBot, build_message
from telegram_chat import TelegramChatHandler
from x_monitor import Tweet, XMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("x_stock_bot")


def _merge_tweets(store: list[Tweet], new_tweets: list[Tweet]) -> None:
    """Add tweets to the store, avoiding duplicates. Keeps newest 200."""
    existing_ids = {t.id for t in store}
    for t in new_tweets:
        if t.id not in existing_ids:
            store.append(t)
            existing_ids.add(t.id)
    # Trim to most recent 200
    if len(store) > 200:
        store.sort(key=lambda t: t.id)
        del store[:-200]


async def x_monitor_loop(
    config: Config,
    monitor: XMonitor,
    analyzer: StockAnalyzer,
    telegram: TelegramBot,
    tweet_history: list[Tweet],
) -> None:
    """Poll X for new tweets, analyse them, send Telegram alerts, and maintain history."""
    await monitor.initialize()

    logger.info(
        "X monitor started — watching @%s every %ds, min confidence=%s",
        config.X_USERNAME,
        config.POLL_INTERVAL_SECONDS,
        config.MIN_CONFIDENCE,
    )

    # Seed: fetch the most recent 40 tweets for initial history
    seed_tweets = await monitor.get_new_tweets(count=40)
    _merge_tweets(tweet_history, seed_tweets)
    last_id = seed_tweets[-1].id if seed_tweets else None
    logger.info(
        "Seeded with %d tweets (last_id=%s, history size=%d)",
        len(seed_tweets), last_id, len(tweet_history),
    )

    while True:
        try:
            new_tweets = await monitor.get_new_tweets(since_id=last_id)
            _merge_tweets(tweet_history, new_tweets)

            for tweet in new_tweets:
                logger.info("Processing tweet %s: %s…", tweet.id, tweet.text[:80])
                last_id = tweet.id

                analysis = analyzer.analyze_tweet(tweet.text)
                logger.info(
                    "  → stock_related=%s signal=%s confidence=%s tickers=%s",
                    analysis.is_stock_related,
                    analysis.signal,
                    analysis.confidence,
                    analysis.tickers,
                )

                if not analysis.is_stock_related:
                    logger.info("  → skipped (not stock-related)")
                    continue

                conf_rank = config.CONFIDENCE_ORDER.get(analysis.confidence, 0)
                if conf_rank < config.min_confidence_rank():
                    logger.info("  → skipped (confidence too low)")
                    continue

                snapshots = {}
                if analysis.tickers:
                    snapshots = analyzer.get_stock_snapshots(analysis.tickers)

                message = build_message(tweet, analysis, snapshots)
                success = telegram.send(message)
                logger.info("  → Telegram %s", "sent ✓" if success else "FAILED ✗")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("X monitor error: %s", e)

        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


async def main() -> None:
    config = Config()
    monitor = XMonitor(config)
    analyzer = StockAnalyzer(config)
    telegram = TelegramBot(config)

    # Shared tweet store — populated by x_monitor_loop, read by TelegramChatHandler
    tweet_history: list[Tweet] = []

    chat = TelegramChatHandler(config, tweet_history)

    await asyncio.gather(
        x_monitor_loop(config, monitor, analyzer, telegram, tweet_history),
        chat.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
