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

import kite_client
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
_daily_cache: dict        = {}
_cache_date: str          = ""
_fii_dii_last_date: str   = ""
_auto_login_date: str     = ""
RAILWAY_URL = os.getenv("RAILWAY_URL", "https://web-production-009fd.up.railway.app")


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


def refresh_daily_cache(quotes: dict) -> None:
    """
    Build daily cache of 30-day avg volumes using instrument tokens from quotes.
    52-week high comes directly from kite.quote() so no separate fetch needed.
    """
    global _daily_cache, _cache_date
    today = str(datetime.now(IST).date())
    if _cache_date == today and _daily_cache:
        return
    logger.info("Refreshing daily cache (52W high + avg volume)...")
    cache = {}
    for symbol, quote in quotes.items():
        token            = quote.get("instrument_token")
        high52, avg_vol  = kite_client.get_daily_stats(token)
        cache[symbol]    = {"week_high_52": high52, "avg_volume": avg_vol}
        time.sleep(0.2)
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

    quotes = kite_client.get_quotes(all_symbols)
    if not quotes:
        logger.error("No quotes from Kite — token may be expired. Check /kite/renew.")
        send_alert(
            f"Kite token expired — no price data\n"
            f"Tap to renew: {RAILWAY_URL}/kite/renew",
            priority="urgent",
        )
        return

    refresh_daily_cache(quotes)

    # Merge 52-week high from daily cache into each quote (Kite quote lacks it)
    for symbol, quote in quotes.items():
        if symbol in _daily_cache:
            quote["week_high_52"] = _daily_cache[symbol]["week_high_52"]

    # avg_volumes for volume spike check
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

def run_auto_login() -> None:
    """
    Automatically log in to Zerodha at 6:05 AM IST every weekday.
    Uses stored credentials + auto-generated TOTP. No human input needed.
    """
    global _auto_login_date
    now   = datetime.now(IST)
    today = str(now.date())

    if now.weekday() < 5 and now.hour == 6 and now.minute < 10 and _auto_login_date != today:
        logger.info("── Auto-login ─────────────────────────────────────────")
        try:
            kite_client.auto_login()
            _auto_login_date = today
            send_alert(
                "Kite token auto-renewed. Ready for today's market session.",
                priority="low",
            )
            logger.info("── Auto-login successful ──────────────────────────")
        except Exception as e:
            logger.error(f"Auto-login failed: {e}")
            send_alert(
                f"Auto-login FAILED: {e}\n"
                f"Manual fallback: {RAILWAY_URL}/kite/renew",
                priority="urgent",
            )


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

    # Load saved token — if none exists, login immediately
    if not kite_client.init_token():
        logger.info("No saved token — running auto-login now...")
        try:
            kite_client.auto_login()
            logger.info("Startup auto-login successful.")
        except Exception as e:
            logger.error(f"Startup auto-login failed: {e}")
            send_alert(
                f"Auto-login failed on startup: {e}\n"
                f"Manual fallback: {RAILWAY_URL}/kite/renew",
                priority="urgent",
            )

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

    schedule.every(5).minutes.do(run_auto_login)
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
