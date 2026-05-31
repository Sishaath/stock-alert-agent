"""
alerts.py — Core alert logic for all five alert types.

Each check function evaluates incoming market data and fires a push notification
if the threshold is breached AND the cooldown period has elapsed.

Cooldown is managed via alerts_log.json:
  - Regular alerts (price drop, volume spike, FII/DII): 4-hour cooldown
  - SEBI/NSE filing alerts: permanent deduplication (each filing fires exactly once)

Alert types:
  1. check_holdings_price_drop  — stock in your Zerodha holdings drops 20%+ from avg buy
  2. check_watchlist_price_drop — watchlist stock drops 20%+ from its 52-week high
  3. check_volume_spike         — any stock has 3x+ today's volume vs 30-day average
  4. check_fii_dii_alert        — net FII or DII flow exceeds ₹3,000 Cr
  5. check_sebi_filings_alert   — new NSE/SEBI announcement for a tracked stock
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from notifier import send_alert
from config import (
    ALERTS_LOG_FILE,
    ALERT_COOLDOWN_HOURS,
    HOLDINGS_DROP_THRESHOLD,
    WATCHLIST_DROP_THRESHOLD,
    VOLUME_SPIKE_MULTIPLIER,
    FII_DII_THRESHOLD_CRORES,
)

logger = logging.getLogger(__name__)
IST    = ZoneInfo("Asia/Kolkata")


# ─── Alert Log Helpers ────────────────────────────────────────────────────────

def _load_log() -> dict:
    """
    Load the alert log from disk.
    Returns a safe default structure if the file is missing or corrupted.
    """
    try:
        with open(ALERTS_LOG_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("alerts", {})
        data.setdefault("sebi_seen_ids", [])
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerts": {}, "sebi_seen_ids": []}


def _save_log(log: dict) -> None:
    """Write the alert log back to disk."""
    with open(ALERTS_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def _can_alert(alert_key: str) -> bool:
    """
    Return True if the cooldown period has passed since the last time
    this exact alert key was fired. Returns True if the alert has never fired.

    Alert keys are strings like "RELIANCE_holdings_drop" or "fii_dii_flow".
    """
    log      = _load_log()
    last_str = log["alerts"].get(alert_key)

    if not last_str:
        return True  # Never fired before

    try:
        last_sent     = datetime.fromisoformat(last_str).astimezone(IST)
        now           = datetime.now(IST)
        elapsed_hours = (now - last_sent).total_seconds() / 3600
        return elapsed_hours >= ALERT_COOLDOWN_HOURS
    except (ValueError, TypeError):
        return True  # Malformed timestamp — allow the alert


def _record_alert(alert_key: str) -> None:
    """Save the current timestamp for an alert key to start the cooldown clock."""
    log                      = _load_log()
    log["alerts"][alert_key] = datetime.now(IST).isoformat()
    _save_log(log)


def _fire(alert_key: str, message: str, priority: str = "high") -> None:
    """
    Send a push notification and record it — but only if the cooldown has elapsed.
    Silently skips if the same alert fired within the last ALERT_COOLDOWN_HOURS.
    """
    if not _can_alert(alert_key):
        logger.debug(f"Cooldown active for '{alert_key}' — skipping duplicate alert.")
        return

    if send_alert(message, priority=priority):
        _record_alert(alert_key)


# ─── 1. Holdings Price Drop ───────────────────────────────────────────────────

def check_holdings_price_drop(holdings: list[dict], quotes: dict) -> None:
    """
    Alert if any holding has fallen >= HOLDINGS_DROP_THRESHOLD % below
    the average price you paid (your cost basis).

    Example alert:
        HDFC (holdings) — down 22.4% from avg buy price
        Avg buy: ₹1,650.00 | Current: ₹1,280.50
        Significant drop in your holding. Review your position.

    Args:
        holdings: Output of kite_client.get_holdings()
        quotes:   Output of kite_client.get_quotes()
    """
    for stock in holdings:
        symbol    = stock["symbol"]
        avg_price = stock["average_price"]

        if avg_price <= 0:
            continue  # No cost basis recorded — skip

        quote = quotes.get(symbol)
        if not quote:
            logger.warning(f"No live quote for holding '{symbol}' — skipping.")
            continue

        current = quote["last_price"]
        if current <= 0:
            continue

        drop_pct = ((avg_price - current) / avg_price) * 100

        if drop_pct >= HOLDINGS_DROP_THRESHOLD:
            alert_key = f"{symbol}_holdings_drop"
            message   = (
                f"{symbol} (holdings) — down {drop_pct:.1f}% from avg buy price\n"
                f"Avg buy: ₹{avg_price:,.2f} | Current: ₹{current:,.2f}\n"
                f"Significant drop in your holding. Review your position."
            )
            logger.info(f"[ALERT] Holdings drop: {symbol} down {drop_pct:.1f}%")
            _fire(alert_key, message)


# ─── 2. Watchlist Price Drop ──────────────────────────────────────────────────

def check_watchlist_price_drop(watchlist: list[str], quotes: dict) -> None:
    """
    Alert if any watchlist stock has fallen >= WATCHLIST_DROP_THRESHOLD %
    from its 52-week high — a common signal for potential accumulation.

    Example alert:
        RELIANCE (watchlist) — down 22% from 52-week high
        52-week high: ₹3,200.00 | Current: ₹2,496.00
        Good accumulation zone. Consider buying.

    Args:
        watchlist: Output of watchlist_manager.load_watchlist()
        quotes:    Output of kite_client.get_quotes()
    """
    for symbol in watchlist:
        quote = quotes.get(symbol)
        if not quote:
            logger.warning(f"No live quote for watchlist stock '{symbol}' — skipping.")
            continue

        high_52w = quote["week_high_52"]
        current  = quote["last_price"]

        if high_52w <= 0 or current <= 0:
            continue

        drop_pct = ((high_52w - current) / high_52w) * 100

        if drop_pct >= WATCHLIST_DROP_THRESHOLD:
            alert_key = f"{symbol}_watchlist_drop"
            message   = (
                f"{symbol} (watchlist) — down {drop_pct:.1f}% from 52-week high\n"
                f"52-week high: ₹{high_52w:,.2f} | Current: ₹{current:,.2f}\n"
                f"Good accumulation zone. Consider buying."
            )
            logger.info(f"[ALERT] Watchlist drop: {symbol} down {drop_pct:.1f}%")
            _fire(alert_key, message)


# ─── 3. Volume Spike ──────────────────────────────────────────────────────────

def check_volume_spike(quotes: dict, avg_volumes: dict) -> None:
    """
    Alert if any tracked stock's today volume has exceeded
    VOLUME_SPIKE_MULTIPLIER × its 30-day average daily volume.

    Today's volume is the total shares traded so far. If even mid-session
    it's 3x the daily average, that's an unusually strong signal.

    Example alert:
        INFY — Volume spike! 4.2x normal volume
        Today's volume: 4,200,000 | 30-day avg: 1,000,000
        Unusual activity detected. Check for news or institutional moves.

    Args:
        quotes:      Output of kite_client.get_quotes() — contains today's volume
        avg_volumes: Dict of { symbol: 30-day avg volume } from the daily cache
    """
    for symbol, quote in quotes.items():
        current_vol = quote.get("volume", 0)
        avg_vol     = avg_volumes.get(symbol)

        if not avg_vol or avg_vol <= 0 or current_vol <= 0:
            continue

        spike_ratio = current_vol / avg_vol

        if spike_ratio >= VOLUME_SPIKE_MULTIPLIER:
            alert_key = f"{symbol}_volume_spike"
            message   = (
                f"{symbol} — Volume spike! {spike_ratio:.1f}x normal volume\n"
                f"Today's volume: {current_vol:,} | 30-day avg: {avg_vol:,.0f}\n"
                f"Unusual activity detected. Check for news or institutional moves."
            )
            logger.info(f"[ALERT] Volume spike: {symbol} at {spike_ratio:.1f}x average")
            _fire(alert_key, message)


# ─── 4. FII/DII Cash Flow ─────────────────────────────────────────────────────

def check_fii_dii_alert(fii_dii_data: list[dict]) -> None:
    """
    Alert if net FII or DII cash market flow exceeds FII_DII_THRESHOLD_CRORES
    in absolute terms (large inflow OR large outflow both trigger).

    Example alert:
        FII/DII Cash Flow — Significant Move Detected
        FII/FPI: Net ₹-4,532 Cr (Outflow) | Buy ₹12,100 Cr | Sell ₹16,632 Cr
        DII:     Net ₹+3,210 Cr (Inflow)  | Buy ₹9,800 Cr  | Sell ₹6,590 Cr
        Heavy FII selling detected. Watch market direction carefully.

    Args:
        fii_dii_data: Output of scrapers.get_fii_dii_data()
    """
    if not fii_dii_data:
        logger.warning("FII/DII data is empty — skipping alert check.")
        return

    lines     = []
    triggered = False

    for entry in fii_dii_data:
        category   = entry.get("category", "Unknown")
        net_value  = float(entry.get("netValue",  0) or 0)
        buy_value  = float(entry.get("buyValue",  0) or 0)
        sell_value = float(entry.get("sellValue", 0) or 0)

        direction = "Inflow" if net_value >= 0 else "Outflow"
        lines.append(
            f"{category}: Net ₹{net_value:+,.0f} Cr ({direction}) | "
            f"Buy ₹{buy_value:,.0f} Cr | Sell ₹{sell_value:,.0f} Cr"
        )

        if abs(net_value) >= FII_DII_THRESHOLD_CRORES:
            triggered = True

    if not triggered:
        logger.info("FII/DII flow within normal range — no alert needed.")
        return

    body    = "\n".join(lines)
    message = (
        f"FII/DII Cash Flow — Significant Move Detected\n"
        f"{body}\n"
        f"Large institutional flow today. Watch market direction carefully."
    )
    logger.info("[ALERT] FII/DII significant cash flow detected.")
    _fire("fii_dii_flow", message, priority="high")


# ─── 5. SEBI / NSE Corporate Filings ─────────────────────────────────────────

def check_sebi_filings_alert(
    announcements: list[dict],
    all_symbols: list[str],
) -> None:
    """
    Send a one-time alert for every new NSE/SEBI announcement related to
    stocks in your holdings or watchlist.

    Unlike other alerts, filings use permanent deduplication (via sebi_seen_ids),
    not a time-based cooldown — each unique filing triggers exactly one alert,
    ever.

    Example alert:
        RELIANCE — New Corporate Filing
        Subject: Board Meeting — Q4 Results Declaration
        Posted:  30-May-2026 14:30:00
        Check NSE website for full details.

    Args:
        announcements: Output of scrapers.get_all_announcements()
        all_symbols:   Combined list of holding symbols + watchlist symbols
    """
    if not announcements:
        return

    log      = _load_log()
    seen_ids = set(log.get("sebi_seen_ids", []))
    new_seen = []

    relevant_symbols = {s.upper() for s in all_symbols}

    for item in announcements:
        filing_id = str(item.get("id") or "").strip()
        symbol    = item.get("symbol", "").upper()
        subject   = item.get("subject", "No subject provided")
        date_str  = item.get("datetime", "")

        if not filing_id:
            continue  # Can't deduplicate without an ID

        if filing_id in seen_ids:
            continue  # Already alerted for this exact filing

        if symbol not in relevant_symbols:
            continue  # Not a stock we're tracking

        # New, relevant filing — send an alert
        message = (
            f"{symbol} — New Corporate/Regulatory Filing\n"
            f"Subject: {subject}\n"
            f"Posted: {date_str}\n"
            f"Check NSE website for full details."
        )

        if send_alert(message, priority="high"):
            new_seen.append(filing_id)
            logger.info(f"[ALERT] New filing for {symbol}: {subject[:70]}")

    # Persist newly seen IDs so we never alert for them again
    if new_seen:
        log["sebi_seen_ids"] = list(seen_ids | set(new_seen))
        _save_log(log)
        logger.info(f"Marked {len(new_seen)} new filings as seen.")
