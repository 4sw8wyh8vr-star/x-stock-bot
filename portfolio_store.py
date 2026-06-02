"""
Persistent storage for the user's watchlist and holdings.
Saved as portfolio.json alongside the bot files.
"""

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")


@dataclass
class Holding:
    ticker: str
    shares: float
    avg_cost: float | None = None  # average purchase price per share


class PortfolioStore:
    def __init__(self):
        self._watchlist: list[str] = []
        self._holdings: dict[str, Holding] = {}
        self._load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not os.path.exists(STORE_FILE):
            return
        try:
            with open(STORE_FILE) as f:
                data = json.load(f)
            self._watchlist = data.get("watchlist", [])
            for ticker, h in data.get("holdings", {}).items():
                self._holdings[ticker] = Holding(
                    ticker=ticker,
                    shares=h["shares"],
                    avg_cost=h.get("avg_cost"),
                )
            logger.info(
                "Portfolio loaded: %d watchlist, %d holdings",
                len(self._watchlist), len(self._holdings),
            )
        except Exception as e:
            logger.error("Failed to load portfolio: %s", e)

    def _save(self) -> None:
        try:
            data = {
                "watchlist": self._watchlist,
                "holdings": {
                    ticker: {"shares": h.shares, "avg_cost": h.avg_cost}
                    for ticker, h in self._holdings.items()
                },
            }
            with open(STORE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save portfolio: %s", e)

    # ------------------------------------------------------------------ #
    #  Watchlist                                                           #
    # ------------------------------------------------------------------ #

    def add_watch(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker not in self._watchlist:
            self._watchlist.append(ticker)
            self._save()
            return True
        return False  # already watching

    def remove_watch(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker in self._watchlist:
            self._watchlist.remove(ticker)
            self._save()
            return True
        return False

    def get_watchlist(self) -> list[str]:
        return list(self._watchlist)

    # ------------------------------------------------------------------ #
    #  Holdings                                                            #
    # ------------------------------------------------------------------ #

    def set_holding(self, ticker: str, shares: float, avg_cost: float | None = None) -> None:
        ticker = ticker.upper()
        self._holdings[ticker] = Holding(ticker=ticker, shares=shares, avg_cost=avg_cost)
        self._save()

    def remove_holding(self, ticker: str) -> bool:
        ticker = ticker.upper()
        if ticker in self._holdings:
            del self._holdings[ticker]
            self._save()
            return True
        return False

    def get_holdings(self) -> dict[str, Holding]:
        return dict(self._holdings)

    # ------------------------------------------------------------------ #
    #  Combined                                                            #
    # ------------------------------------------------------------------ #

    def all_tickers(self) -> list[str]:
        """All unique tickers across watchlist and holdings."""
        return list(set(self._watchlist) | set(self._holdings.keys()))
