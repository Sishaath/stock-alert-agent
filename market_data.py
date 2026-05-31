"""
market_data.py — Fetches real-time market data directly from NSE website APIs.

No login. No tokens. No passwords. No daily renewal.
Uses the same internal APIs that power nseindia.com — data is real-time.

Functions:
    get_quotes(symbols)        — live price, volume, 52-week high for all symbols
    get_30day_avg_volume(sym)  — historical average daily volume (for spike detection)
"""

import logging
import time
from datetime import datetime, timedelta

import requests

from config import (
    NSE_BASE_URL,
    NSE_QUOTE_URL,
    NSE_HISTORY_URL,
    VOLUME_HISTORY_DAYS,
)

logger = logging.getLogger(__name__)

# NSE blocks plain requests without browser-like headers and session cookies.
# We visit the NSE homepage first to get valid cookies, then hit the APIs.
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class _NSESession:
    """Cookie-aware HTTP session for NSE APIs."""

    def __init__(self):
        self.session = requests.Session()
        self._init_cookies()

    def _init_cookies(self) -> None:
        """Hit the NSE homepage to receive valid session cookies."""
        try:
            self.session.get(
                NSE_BASE_URL,
                headers={**_NSE_HEADERS, "Accept": "text/html,application/xhtml+xml"},
                timeout=20,
            )
            logger.debug("NSE session cookies initialised.")
        except requests.RequestException as e:
            logger.warning(f"NSE cookie init failed (will retry on first request): {e}")

    def get(self, url: str, **kwargs) -> requests.Response | None:
        """GET request with auto-retry on 403 (stale cookies)."""
        try:
            resp = self.session.get(url, headers=_NSE_HEADERS, timeout=20, **kwargs)
            if resp.status_code == 403:
                logger.warning("NSE returned 403 — refreshing cookies and retrying.")
                self._init_cookies()
                resp = self.session.get(url, headers=_NSE_HEADERS, timeout=20, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"NSE request timed out: {url}")
            return None
        except requests.RequestException as e:
            logger.error(f"NSE request failed [{url}]: {e}")
            return None


# Single shared session for all market data calls
_session = _NSESession()


# ─── Live Quote ───────────────────────────────────────────────────────────────

def _get_quote(symbol: str) -> dict | None:
    """
    Fetch a real-time quote for one NSE stock.

    Returns dict with:
        last_price   — current market price (real-time)
        volume       — shares traded so far today
        week_high_52 — 52-week high price
        week_low_52  — 52-week low price
    """
    resp = _session.get(NSE_QUOTE_URL, params={"symbol": symbol.upper()})
    if resp is None:
        logger.warning(f"No response from NSE for {symbol}")
        return None

    try:
        data       = resp.json()
        price_info = data.get("priceInfo", {})
        week_hl    = price_info.get("weekHighLow", {})
        trade_info = data.get("marketDeptOrderBook", {}).get("tradeInfo", {})

        last_price   = float(price_info.get("lastPrice", 0))
        week_high_52 = float(week_hl.get("max", 0))
        week_low_52  = float(week_hl.get("min", 0))
        volume       = int(str(trade_info.get("totalTradedVolume", 0)).replace(",", "") or 0)

        if last_price <= 0:
            logger.warning(f"NSE returned zero price for {symbol} — market may be closed.")
            return None

        return {
            "last_price":   last_price,
            "volume":       volume,
            "week_high_52": week_high_52,
            "week_low_52":  week_low_52,
        }

    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Failed to parse NSE quote for {symbol}: {e}")
        return None


def get_quotes(symbols: list[str]) -> dict:
    """
    Fetch real-time quotes for a list of NSE stock symbols.

    Args:
        symbols: List of NSE tickers, e.g. ["RELIANCE", "TCS", "INFY"]

    Returns:
        Dict mapping symbol -> { last_price, volume, week_high_52, week_low_52 }
        Symbols that fail are silently omitted.
    """
    results: dict = {}

    for symbol in symbols:
        quote = _get_quote(symbol)
        if quote:
            results[symbol] = quote
            logger.debug(
                f"{symbol}: ₹{quote['last_price']} | "
                f"Vol: {quote['volume']:,} | "
                f"52W High: ₹{quote['week_high_52']}"
            )
        # Small delay between calls — be polite to NSE servers
        time.sleep(0.4)

    logger.info(f"Fetched live quotes for {len(results)}/{len(symbols)} symbols.")
    return results


# ─── 30-Day Average Volume ────────────────────────────────────────────────────

def get_30day_avg_volume(symbol: str) -> float | None:
    """
    Compute the average daily trading volume over the last 30 trading days
    using NSE historical data.

    Called once per day at market open and cached in main.py.

    Args:
        symbol: NSE stock ticker, e.g. "RELIANCE"

    Returns:
        Average daily volume as float, or None on failure.
    """
    to_date   = datetime.now().date()
    from_date = to_date - timedelta(days=VOLUME_HISTORY_DAYS + 15)  # buffer for holidays

    params = {
        "symbol": symbol.upper(),
        "series": '["EQ"]',
        "from":   from_date.strftime("%d-%m-%Y"),
        "to":     to_date.strftime("%d-%m-%Y"),
    }

    resp = _session.get(NSE_HISTORY_URL, params=params)
    if resp is None:
        return None

    try:
        rows = resp.json().get("data", [])
        if not rows:
            logger.warning(f"No historical data returned for {symbol}")
            return None

        # Take only the last 30 trading days and average their volumes
        recent = rows[-VOLUME_HISTORY_DAYS:]
        volumes = [
            int(str(row.get("CH_TOT_TRADED_QTY", 0)).replace(",", "") or 0)
            for row in recent
            if row.get("CH_TOT_TRADED_QTY")
        ]

        if not volumes:
            return None

        avg = round(sum(volumes) / len(volumes), 0)
        logger.debug(f"{symbol}: 30-day avg volume = {avg:,.0f}")
        return avg

    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Failed to compute avg volume for {symbol}: {e}")
        return None
