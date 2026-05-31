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
    """Load saved token at startup. Returns True if token loaded."""
    token = load_token()
    if token:
        kite.set_access_token(token)
        logger.info("Kite token loaded from file.")
        return True
    logger.warning("No saved token found — auto-login will run at 6 AM.")
    return False


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
    resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":      KITE_USER_ID,
            "request_id":   request_id,
            "twofa_value":  totp_code,
            "twofa_type":   "totp",
            "skip_session": "",
        },
        timeout=15,
        allow_redirects=False,  # Capture redirect without following it
    )

    # Step 3: Parse request_token from redirect Location header
    location      = resp.headers.get("Location", "")
    params        = parse_qs(urlparse(location).query)
    request_token = params.get("request_token", [None])[0]

    if not request_token:
        raise Exception(
            f"Could not find request_token in redirect. "
            f"Location header: '{location}'"
        )

    logger.info("TOTP step passed. Exchanging token...")

    # Step 4: Exchange request_token for access_token
    session_data = kite.generate_session(
        request_token, api_secret=KITE_API_SECRET
    )
    access_token = session_data["access_token"]

    save_token(access_token)
    logger.info("Auto-login complete. Token valid for today's session.")
    return access_token


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


def get_30day_avg_volume(instrument_token: int) -> float | None:
    """Compute 30-day average daily volume from Kite historical data."""
    if not instrument_token:
        return None
    try:
        to_date   = datetime.now().date()
        from_date = to_date - timedelta(days=45)
        history   = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval="day",
        )
        if not history:
            return None
        # Average the volume of the last 30 candles (plain Python, no pandas)
        volumes = [row["volume"] for row in history[-30:] if row.get("volume")]
        if not volumes:
            return None
        return round(sum(volumes) / len(volumes), 0)
    except Exception as e:
        logger.error(f"Historical data error for token {instrument_token}: {e}")
        return None
