"""
market_data.py — Real-time Indian stock data via Finnhub API.

Works from any server globally — no geo-blocking, no tokens, no daily renewal.
Free tier: 60 API calls/minute (plenty for 10-15 stocks every 5 minutes).

Sign up at finnhub.io to get a free API key.
Add FINNHUB_API_KEY to your .env and Railway environment variables.
"""

import logging
import os
import time
from datetime import datetime, timedelta

import requests

from config import VOLUME_HISTORY_DAYS

logger     = logging.getLogger(__name__)
_BASE_URL  = "https://finnhub.io/api/v1"
_API_KEY   = os.getenv("FINNHUB_API_KEY", "")


# ETFs are often listed under BSE on Finnhub when NSE doesn't return data
_BSE_ETFS = {"GOLDBEES", "SILVERBEES", "LIQUIDBEES", "CPSEETF", "NETFIT"}

def _sym(symbol: str) -> str:
    """Convert NSE ticker to Finnhub format. ETFs fall back to BSE prefix."""
    s = symbol.upper()
    exchange = "BSE" if s in _BSE_ETFS else "NSE"
    return f"{exchange}:{s}"


def _get(endpoint: str, params: dict) -> dict | None:
    """Make a Finnhub API call. Returns parsed JSON or None on failure."""
    if not _API_KEY:
        logger.error("FINNHUB_API_KEY is not set.")
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/{endpoint}",
            params={**params, "token": _API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        logger.error(f"Finnhub timeout on /{endpoint}")
        return None
    except Exception as e:
        logger.error(f"Finnhub error on /{endpoint}: {e}")
        return None


# ─── Live Quote ───────────────────────────────────────────────────────────────

def _get_quote(symbol: str) -> dict | None:
    """
    Fetch current price + today's partial volume for one NSE stock.
    Makes 2 API calls: one for price, one for today's intraday volume.
    """
    # Current price
    price_data = _get("quote", {"symbol": _sym(symbol)})
    if not price_data or float(price_data.get("c", 0)) == 0:
        logger.warning(f"No price data for {symbol} (market may be closed or symbol invalid)")
        return None

    current_price = round(float(price_data["c"]), 2)

    # Today's traded volume — fetch last 5 daily candles, take latest
    to_ts   = int(datetime.now().timestamp())
    from_ts = int((datetime.now() - timedelta(days=7)).timestamp())
    candles = _get("stock/candle", {
        "symbol":     _sym(symbol),
        "resolution": "D",
        "from":       from_ts,
        "to":         to_ts,
    })
    volume = 0
    if candles and candles.get("s") == "ok" and candles.get("v"):
        volume = int(candles["v"][-1])

    return {
        "last_price":   current_price,
        "volume":       volume,
        "week_high_52": 0,   # injected later from daily cache
        "week_low_52":  0,
    }


def get_quotes(symbols: list[str]) -> dict:
    """
    Fetch live quotes for a list of NSE stocks.

    Returns { symbol: { last_price, volume, week_high_52, week_low_52 } }
    week_high_52 starts as 0 — main.py merges the daily cache into it.
    """
    results: dict = {}
    for symbol in symbols:
        quote = _get_quote(symbol)
        if quote:
            results[symbol] = quote
            logger.debug(f"{symbol}: ₹{quote['last_price']} | Vol: {quote['volume']:,}")
        time.sleep(0.3)   # stay well within 60 calls/min rate limit

    logger.info(f"Fetched live quotes for {len(results)}/{len(symbols)} symbols.")
    return results


# ─── Historical Data (cached once daily) ─────────────────────────────────────

def get_52week_high_and_avg_volume(symbol: str) -> tuple[float, float | None]:
    """
    Fetch one year of daily candles and return:
        - 52-week high price
        - 30-day average daily volume

    Called once per day and cached — avoids repeated historical API calls.
    """
    to_ts   = int(datetime.now().timestamp())
    from_ts = int((datetime.now() - timedelta(days=380)).timestamp())  # ~1 year + buffer

    candles = _get("stock/candle", {
        "symbol":     _sym(symbol),
        "resolution": "D",
        "from":       from_ts,
        "to":         to_ts,
    })

    if not candles or candles.get("s") != "ok":
        logger.warning(f"No historical candles for {symbol}")
        return 0.0, None

    highs   = candles.get("h", [])
    volumes = candles.get("v", [])

    week_high_52 = round(max(highs), 2) if highs else 0.0

    recent_vols  = [v for v in volumes[-VOLUME_HISTORY_DAYS:] if v > 0]
    avg_volume   = round(sum(recent_vols) / len(recent_vols), 0) if recent_vols else None

    logger.debug(f"{symbol}: 52W High=₹{week_high_52} | 30D Avg Vol={avg_volume:,.0f}" if avg_volume else f"{symbol}: 52W High=₹{week_high_52}")
    return week_high_52, avg_volume


def get_30day_avg_volume(symbol: str) -> float | None:
    """Convenience wrapper used by main.py volume cache."""
    _, avg_vol = get_52week_high_and_avg_volume(symbol)
    return avg_vol
