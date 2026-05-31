"""
config.py — Central configuration for all thresholds, URLs, and settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Notifications (ntfy.sh) ──────────────────────────────────────────────────
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "my_stock_alerts")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"

# ─── Finnhub API ──────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# ─── Kite Connect ─────────────────────────────────────────────────────────────
KITE_API_KEY    = os.getenv("KITE_API_KEY",    "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_USER_ID    = os.getenv("KITE_USER_ID",    "")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD",   "")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET", "")

# ─── Alert Thresholds & Settings (Dynamic Loader) ──────────────────────────────
import json

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

def load_settings() -> dict:
    default_settings = {
        "HOLDINGS_DROP_THRESHOLD": 20.0,
        "WATCHLIST_DROP_THRESHOLD": 20.0,
        "VOLUME_SPIKE_MULTIPLIER": 3.0,
        "FII_DII_THRESHOLD_CRORES": 3000.0,
        "ALERT_COOLDOWN_HOURS": 4.0,
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                for k, v in saved.items():
                    if k in default_settings:
                        default_settings[k] = float(v)
    except Exception as e:
        import logging
        logging.getLogger("config").error(f"Failed to load settings: {e}")
    return default_settings

def save_settings(settings: dict) -> None:
    try:
        valid_keys = {
            "HOLDINGS_DROP_THRESHOLD",
            "WATCHLIST_DROP_THRESHOLD",
            "VOLUME_SPIKE_MULTIPLIER",
            "FII_DII_THRESHOLD_CRORES",
            "ALERT_COOLDOWN_HOURS",
        }
        to_save = {k: float(v) for k, v in settings.items() if k in valid_keys}
        with open(SETTINGS_FILE, "w") as f:
            json.dump(to_save, f, indent=2)
    except Exception as e:
        import logging
        logging.getLogger("config").error(f"Failed to save settings: {e}")

_settings = load_settings()

HOLDINGS_DROP_THRESHOLD  = _settings["HOLDINGS_DROP_THRESHOLD"]
WATCHLIST_DROP_THRESHOLD = _settings["WATCHLIST_DROP_THRESHOLD"]
VOLUME_SPIKE_MULTIPLIER  = _settings["VOLUME_SPIKE_MULTIPLIER"]
FII_DII_THRESHOLD_CRORES = _settings["FII_DII_THRESHOLD_CRORES"]
ALERT_COOLDOWN_HOURS     = _settings["ALERT_COOLDOWN_HOURS"]

# ─── Indian Market Hours (IST) ────────────────────────────────────────────────
MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MINUTE  = 15
MARKET_CLOSE_HOUR   = 15
MARKET_CLOSE_MINUTE = 30

# ─── Data Fetching ────────────────────────────────────────────────────────────
VOLUME_HISTORY_DAYS = 30

# ─── NSE Endpoints ────────────────────────────────────────────────────────────
NSE_BASE_URL          = "https://www.nseindia.com"
NSE_QUOTE_URL         = "https://www.nseindia.com/api/quote-equity"
NSE_HISTORY_URL       = "https://www.nseindia.com/api/historical/cm/equity"
NSE_FII_DII_URL       = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&symbol={symbol}"
)

# ─── SEBI Endpoint ────────────────────────────────────────────────────────────
SEBI_PRESS_RELEASES_URL = "https://www.sebi.gov.in/media/press-releases.html"

# ─── Persistent storage directory ─────────────────────────────────────────────
# Locally  : files live in the current directory (default ".")
# Railway  : set DATA_DIR=/data and attach a Volume mounted at /data
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

# On first run, copy default files from the app directory into DATA_DIR
# (Railway volumes start empty — this seeds them with the repo defaults)
import shutil as _shutil
_app_dir = os.path.dirname(os.path.abspath(__file__))
for _fname in ["holdings.json", "watchlist.json", "alerts_log.json"]:
    _dest = os.path.join(DATA_DIR, _fname)
    _src  = os.path.join(_app_dir, _fname)
    if not os.path.exists(_dest) and os.path.exists(_src):
        _shutil.copy(_src, _dest)

# ─── File Paths ───────────────────────────────────────────────────────────────
HOLDINGS_FILE   = os.path.join(DATA_DIR, "holdings.json")
WATCHLIST_FILE  = os.path.join(DATA_DIR, "watchlist.json")
ALERTS_LOG_FILE = os.path.join(DATA_DIR, "alerts_log.json")
