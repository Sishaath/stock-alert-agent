"""
main.py — Entry point. Runs the postback server and alert scheduler together.

Two things run in parallel:
  1. Flask server (background thread) — receives trade postbacks from Zerodha
  2. Scheduler (main thread)          — monitors prices and sends alerts

Deploy on Railway as a 'web' service so it gets a public URL for postbacks.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

import market_data
import scrapers
from alerts import (
    check_holdings_price_drop,
    check_watchlist_price_drop,
    check_volume_spike,
    check_fii_dii_alert,
    check_sebi_filings_alert,
)
from holdings_manager import load_holdings
from watchlist_manager import load_watchlist
from notifier import send_alert
from postback_server import run_server
from config import (
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MINUTE,
    DATA_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")
IST    = ZoneInfo("Asia/Kolkata")

# ─── Daily volume cache ───────────────────────────────────────────────────────
_daily_cache: dict      = {}   # { symbol: { week_high_52, avg_volume } }
_cache_date: str        = ""
_fii_dii_last_date: str = ""


def _save_price_cache(quotes: dict) -> None:
    """Save latest quotes to disk so the dashboard can read them."""
    cache_file = os.path.join(DATA_DIR, "prices_cache.json")
    try:
        with open(cache_file, "w") as f:
            json.dump({
                "quotes":     quotes,
                "updated_at": datetime.now(IST).isoformat(),
            }, f)
    except Exception as e:
        logger.error(f"Failed to save price cache: {e}")


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MINUTE,  second=0, microsecond=0)
    close_ = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    return open_ <= now <= close_


def refresh_daily_cache(all_symbols: list[str]) -> None:
    """Fetch 52-week high and 30-day avg volume for all symbols once per day."""
    global _daily_cache, _cache_date
    today = str(datetime.now(IST).date())
    if _cache_date == today and _daily_cache:
        return
    logger.info(f"Refreshing daily cache (52W high + avg volume) for {len(all_symbols)} symbols...")
    cache = {}
    for symbol in all_symbols:
        high, avg_vol = market_data.get_52week_high_and_avg_volume(symbol)
        cache[symbol] = {"week_high_52": high, "avg_volume": avg_vol}
        time.sleep(0.3)
    _daily_cache = cache
    _cache_date  = today
    logger.info(f"Daily cache ready: {len(cache)} symbols.")


# ─── Task 1: Market checks every 5 minutes ───────────────────────────────────

def run_market_checks() -> None:
    if not is_market_open():
        logger.debug("Market closed — skipping.")
        return

    logger.info("── Market check ──────────────────────────────────────")

    holdings  = load_holdings()
    watchlist = load_watchlist()

    holding_symbols = [h["symbol"] for h in holdings]
    all_symbols     = list(set(holding_symbols + watchlist))

    if not all_symbols:
        logger.warning("Holdings and watchlist are both empty — nothing to monitor.")
        return

    refresh_daily_cache(all_symbols)

    quotes = market_data.get_quotes(all_symbols)
    if not quotes:
        logger.error("No quotes returned from Finnhub — skipping this cycle.")
        return

    # Merge 52-week high from daily cache into each quote
    for symbol, quote in quotes.items():
        if symbol in _daily_cache:
            quote["week_high_52"] = _daily_cache[symbol]["week_high_52"]

    # Build avg_volumes dict for volume spike check
    avg_volumes = {
        sym: data["avg_volume"]
        for sym, data in _daily_cache.items()
        if data.get("avg_volume")
    }

    # Save to cache so the dashboard can display prices
    _save_price_cache(quotes)

    check_holdings_price_drop(holdings, quotes)
    check_watchlist_price_drop(watchlist, quotes)
    check_volume_spike(quotes, avg_volumes)

    logger.info("── Market check done ─────────────────────────────────")


# ─── Task 2: FII/DII once daily at ~9:00 AM IST ──────────────────────────────

def maybe_run_fii_dii() -> None:
    global _fii_dii_last_date
    now   = datetime.now(IST)
    today = str(now.date())
    if now.weekday() < 5 and now.hour == 9 and now.minute < 10 and _fii_dii_last_date != today:
        logger.info("── FII/DII check ─────────────────────────────────────")
        check_fii_dii_alert(scrapers.get_fii_dii_data())
        _fii_dii_last_date = today
        logger.info("── FII/DII done ──────────────────────────────────────")


# ─── Task 3: SEBI/NSE filings every 30 minutes ───────────────────────────────

def run_sebi_check() -> None:
    logger.info("── Filing check ──────────────────────────────────────")
    holdings    = load_holdings()
    watchlist   = load_watchlist()
    all_symbols = list(set([h["symbol"] for h in holdings] + watchlist))
    if not all_symbols:
        return
    announcements = scrapers.get_all_announcements(all_symbols)
    check_sebi_filings_alert(announcements, all_symbols)
    logger.info("── Filing check done ─────────────────────────────────")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("  Stock Market Alert Agent — Starting")
    logger.info("=" * 60)

    # Start postback server in background thread
    # daemon=True means it shuts down automatically when main thread exits
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    logger.info("Postback server started in background thread.")

    holdings  = load_holdings()
    watchlist = load_watchlist()
    logger.info(f"Holdings : {[h['symbol'] for h in holdings]}")
    logger.info(f"Watchlist: {watchlist}")

    send_alert(
        f"Stock Alert Agent started!\n"
        f"Holdings: {', '.join(h['symbol'] for h in holdings) or 'none set yet'}\n"
        f"Watchlist: {', '.join(watchlist) or 'none set yet'}\n"
        f"Postback server ready — trades will auto-update holdings.",
        priority="low",
    )

    schedule.every(5).minutes.do(run_market_checks)
    schedule.every(5).minutes.do(maybe_run_fii_dii)
    schedule.every(30).minutes.do(run_sebi_check)

    run_sebi_check()  # immediate check at startup

    logger.info("Scheduler running.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
