"""
config.py — Central configuration for all thresholds, URLs, and settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Notifications (ntfy.sh) ──────────────────────────────────────────────────
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "my_stock_alerts")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"

# ─── Kite postback validation ─────────────────────────────────────────────────
# Used only to verify postbacks are genuinely from Zerodha — no API calls made
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")

# ─── Alert Thresholds ─────────────────────────────────────────────────────────
HOLDINGS_DROP_THRESHOLD  = 20    # % below your average buy price
WATCHLIST_DROP_THRESHOLD = 20    # % below 52-week high
VOLUME_SPIKE_MULTIPLIER  = 3     # x times the 30-day average daily volume
FII_DII_THRESHOLD_CRORES = 3000  # ₹ crore net FII or DII flow

# ─── Duplicate Alert Prevention ───────────────────────────────────────────────
ALERT_COOLDOWN_HOURS = 4

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
