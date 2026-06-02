"""
kite_client.py — Zerodha KiteConnect with fully automated daily login.

Every day at 6:05 AM IST the agent logs itself in using stored credentials
and a TOTP code it generates automatically. No human involvement ever.

Token is saved to /data/kite_token.json and survives Railway restarts.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pyotp
import requests as req_lib
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from config import (
    DATA_DIR,
    KITE_API_KEY,
    KITE_API_SECRET,
    KITE_USER_ID,
    KITE_PASSWORD,
    KITE_TOTP_SECRET,
)

logger     = logging.getLogger(__name__)
IST        = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = os.path.join(DATA_DIR, "kite_token.json")

kite = KiteConnect(api_key=KITE_API_KEY)


# ─── Token Storage ────────────────────────────────────────────────────────────

def load_token() -> str | None:
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("access_token")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(access_token: str) -> None:
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "saved_at":     datetime.now(IST).isoformat(),
        }, f)
    kite.set_access_token(access_token)
    logger.info("Kite access token saved and applied.")


def init_token() -> bool:
    """Load saved token at startup. Returns True only if token is from today's Kite session.

    Kite tokens expire at 6 AM IST daily. A token saved before today's 6 AM is stale
    even if the file exists — returning False triggers an immediate auto-login on startup.
    """
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("No saved token found — auto-login will run now.")
        return False

    token       = data.get("access_token")
    saved_at_str = data.get("saved_at")

    if not token:
        logger.warning("Token file exists but contains no access_token.")
        return False

    # Validate that the token was issued after today's 6 AM IST cutoff
    try:
        saved_at  = datetime.fromisoformat(saved_at_str)
        today_6am = datetime.now(IST).replace(hour=6, minute=0, second=0, microsecond=0)
        if saved_at < today_6am:
            logger.warning(
                f"Saved token is from {saved_at.strftime('%Y-%m-%d %H:%M IST')} — "
                "before today's 6 AM cutoff. Running auto-login now."
            )
            return False
    except Exception:
        pass  # If saved_at can't be parsed, load the token and let the API fail naturally

    kite.set_access_token(token)
    logger.info(f"Kite token loaded from file (saved at {saved_at_str}).")
    return True


# ─── Automated Login ──────────────────────────────────────────────────────────

def auto_login() -> str:
    """
    Fully automated Zerodha login using stored credentials + auto-generated TOTP.

    Flow:
      1. POST credentials to Zerodha login API
      2. Generate TOTP code using pyotp (no manual input)
      3. POST TOTP to 2FA API
      4. Parse request_token from redirect URL
      5. Exchange for access_token via Kite SDK
      6. Save token to disk

    Returns the new access_token.
    Raises Exception if any step fails.
    """
    if not all([KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET]):
        raise Exception(
            "Missing credentials. Set KITE_USER_ID, KITE_PASSWORD, "
            "KITE_TOTP_SECRET in Railway environment variables."
        )

    session = req_lib.Session()

    # Visit the Kite Connect login URL so Zerodha knows which API key to use
    session.get(kite.login_url(), timeout=15)

    # Step 1: Submit login credentials
    resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": KITE_USER_ID, "password": KITE_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    login_data = resp.json()

    if login_data.get("status") != "success":
        raise Exception(f"Login rejected: {login_data.get('message', login_data)}")

    request_id = login_data["data"]["request_id"]
    logger.info("Login step 1 passed.")

    # Step 2: Submit TOTP (auto-generated, no manual input)
    totp_code = pyotp.TOTP(KITE_TOTP_SECRET).now()
    twofa = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":      KITE_USER_ID,
            "request_id":   request_id,
            "twofa_value":  totp_code,
            "twofa_type":   "totp",
            "skip_session": "",
        },
        timeout=15,
    )
    twofa.raise_for_status()
    logger.info("TOTP step passed.")

    # Step 3: Now that the session is authenticated, re-request the Kite Connect
    # login URL. Zerodha redirects to our redirect_url with ?request_token=XXX.
    # We parse the token out of the redirect Location header.
    request_token = None
    try:
        auth = session.get(kite.login_url(), timeout=15, allow_redirects=False)
        location = auth.headers.get("Location", "")
        # The redirect may chain — follow up to 5 hops looking for request_token
        hops = 0
        while location and "request_token" not in location and hops < 5:
            nxt = session.get(location, timeout=15, allow_redirects=False)
            location = nxt.headers.get("Location", "")
            hops += 1
        params        = parse_qs(urlparse(location).query)
        request_token = params.get("request_token", [None])[0]
    except Exception as e:
        logger.warning(f"Redirect parse note: {e}")

    if not request_token:
        raise Exception(
            "Could not extract request_token after TOTP. "
            "Check that the Kite app's Redirect URL is set (e.g. https://127.0.0.1)."
        )

    logger.info("Got request_token. Exchanging for access_token...")

    # Step 4: Exchange request_token for access_token
    session_data = kite.generate_session(
        request_token, api_secret=KITE_API_SECRET
    )
    access_token = session_data["access_token"]

    save_token(access_token)
    logger.info("Auto-login complete. Token valid for today's session.")
    return access_token


# ─── Portfolio Sync ───────────────────────────────────────────────────────────

def fetch_holdings_from_zerodha() -> list[dict]:
    """
    Fetch the actual current portfolio from Zerodha via kite.holdings().

    Includes both settled quantity and T1 (recently bought, not yet in demat)
    so a stock bought today is immediately reflected.

    Returns list of {"symbol", "quantity", "average_price"}.
    Raises KiteException if token is invalid.
    """
    raw = kite.holdings()
    holdings = []
    for h in raw:
        symbol = (h.get("tradingsymbol") or "").upper().strip()
        qty    = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        avg    = h.get("average_price") or 0
        if symbol and qty > 0 and avg > 0:
            holdings.append({
                "symbol":        symbol,
                "quantity":      round(float(qty), 4),
                "average_price": round(float(avg), 2),
            })
    logger.info(f"Fetched {len(holdings)} holdings from Zerodha.")
    return holdings


# ─── Market Data ──────────────────────────────────────────────────────────────

def get_quotes(symbols: list[str], exchange: str = "NSE") -> dict:
    """
    Fetch live real-time quotes for a list of NSE stocks.
    Returns { symbol: { last_price, volume, week_high_52, week_low_52, instrument_token } }
    """
    if not symbols:
        return {}
    try:
        instruments = [f"{exchange}:{s}" for s in symbols]
        raw         = kite.quote(instruments)
        results     = {}
        for key, data in raw.items():
            symbol = key.split(":", 1)[1]
            results[symbol] = {
                "last_price":       round(float(data.get("last_price",   0)), 2),
                "volume":           int(data.get("volume", 0)),
                "week_high_52":     round(float(data.get("week_high_52", 0)), 2),
                "week_low_52":      round(float(data.get("week_low_52",  0)), 2),
                "instrument_token": data.get("instrument_token"),
            }
        logger.info(f"Fetched quotes for {len(results)}/{len(symbols)} symbols.")
        return results
    except KiteException as e:
        logger.error(f"Kite quote error (token may be expired): {e}")
        return {}
    except Exception as e:
        logger.error(f"Quote fetch error: {e}")
        return {}


def get_daily_stats(instrument_token: int) -> tuple[float, float | None]:
    """
    From one year of daily candles, compute:
      - 52-week high price
      - 30-day average daily volume

    Kite's quote() API does not return 52-week high, so we derive it here.
    Returns (week_high_52, avg_volume_30d). Called once per day and cached.
    """
    if not instrument_token:
        return 0.0, None
    try:
        to_date   = datetime.now().date()
        from_date = to_date - timedelta(days=380)  # ~1 year + buffer
        history   = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval="day",
        )
        if not history:
            return 0.0, None

        highs        = [row["high"] for row in history if row.get("high")]
        week_high_52 = round(max(highs), 2) if highs else 0.0

        volumes      = [row["volume"] for row in history[-30:] if row.get("volume")]
        avg_volume   = round(sum(volumes) / len(volumes), 0) if volumes else None

        return week_high_52, avg_volume
    except Exception as e:
        logger.error(f"Historical data error for token {instrument_token}: {e}")
        return 0.0, None


def get_intraday_data(symbol: str, exchange: str = "NSE") -> list[dict]:
    """
    Fetch today's intraday candle data (5-minute interval) from Kite Connect.
    If it's the weekend or off-hours, fetches candles for the previous active trading day.
    """
    try:
        # Resolve instrument token
        q = get_quotes([symbol], exchange)
        token = q.get(symbol, {}).get("instrument_token")
        if not token:
            return []

        to_date = datetime.now(IST)
        # 9:15 AM IST start of market
        from_date = to_date.replace(hour=9, minute=15, second=0, microsecond=0)

        # Handle weekend or off-hours fallback
        if to_date.weekday() >= 5 or to_date.hour < 9 or (to_date.hour == 9 and to_date.minute < 15):
            days_to_subtract = 1
            if to_date.weekday() == 5:    # Saturday
                days_to_subtract = 1
            elif to_date.weekday() == 6:  # Sunday
                days_to_subtract = 2
            elif to_date.weekday() == 0 and to_date.hour < 9: # Monday morning before market
                days_to_subtract = 3
            else:
                days_to_subtract = 1

            from_date = (to_date - timedelta(days=days_to_subtract)).replace(hour=9, minute=15, second=0, microsecond=0)
            to_date = from_date.replace(hour=15, minute=30, second=0, microsecond=0)

        history = kite.historical_data(
            instrument_token=token,
            from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval="5minute",
        )

        return [
            {
                "time": row["date"].astimezone(IST).strftime("%H:%M"),
                "price": round(float(row["close"]), 2)
            }
            for row in history
        ]
    except Exception as e:
        logger.error(f"Error fetching intraday historical data for {symbol}: {e}")
        return []
