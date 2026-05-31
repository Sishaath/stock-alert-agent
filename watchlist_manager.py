"""
watchlist_manager.py — Reads and manages the stock watchlist from watchlist.json.

You can edit watchlist.json directly at any time to add or remove stocks —
no code changes needed. The agent picks up changes on the next scheduled run.

Watchlist format (watchlist.json):
    ["RELIANCE", "TCS", "INFY", "HDFCBANK"]
"""

import json
import logging
from config import WATCHLIST_FILE

logger = logging.getLogger(__name__)


def load_watchlist() -> list[str]:
    """
    Load and return the list of stock symbols from watchlist.json.
    Returns an empty list if the file is missing or invalid.
    """
    try:
        with open(WATCHLIST_FILE, "r") as f:
            watchlist = json.load(f)

        if not isinstance(watchlist, list):
            logger.error(f"{WATCHLIST_FILE} must contain a JSON array of strings.")
            return []

        # Normalize all symbols to uppercase and strip whitespace
        return [str(sym).upper().strip() for sym in watchlist if sym]

    except FileNotFoundError:
        logger.warning(f"{WATCHLIST_FILE} not found — creating an empty one.")
        _save_watchlist([])
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {WATCHLIST_FILE}: {e}")
        return []


def _save_watchlist(watchlist: list[str]) -> None:
    """Persist the watchlist to watchlist.json."""
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)
    logger.debug(f"Watchlist saved: {watchlist}")


def add_stock(symbol: str) -> bool:
    """
    Add a stock symbol to the watchlist.

    Args:
        symbol: Stock symbol to add (e.g. "WIPRO")

    Returns:
        True if the stock was added, False if it was already present.
    """
    symbol = symbol.upper().strip()
    watchlist = load_watchlist()

    if symbol in watchlist:
        logger.info(f"{symbol} is already in the watchlist.")
        return False

    watchlist.append(symbol)
    _save_watchlist(watchlist)
    logger.info(f"Added {symbol} to watchlist. Current watchlist: {watchlist}")
    return True


def remove_stock(symbol: str) -> bool:
    """
    Remove a stock symbol from the watchlist.

    Args:
        symbol: Stock symbol to remove (e.g. "WIPRO")

    Returns:
        True if the stock was removed, False if it was not found.
    """
    symbol = symbol.upper().strip()
    watchlist = load_watchlist()

    if symbol not in watchlist:
        logger.info(f"{symbol} is not in the watchlist.")
        return False

    watchlist.remove(symbol)
    _save_watchlist(watchlist)
    logger.info(f"Removed {symbol} from watchlist. Current watchlist: {watchlist}")
    return True
