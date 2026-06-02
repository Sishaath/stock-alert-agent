"""
postback_server.py — Flask server that receives trade notifications from Zerodha
and serves an ultra-premium, interactive dashboard for portfolio and alerts management.
"""

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, redirect

from holdings_manager import process_trade, _lock as holdings_lock, HOLDINGS_FILE, _save_holdings
from watchlist_manager import load_watchlist, add_stock, remove_stock
from notifier import send_alert
from config import KITE_API_SECRET, DATA_DIR, load_settings, save_settings

logger = logging.getLogger(__name__)
app    = Flask(__name__)
IST    = ZoneInfo("Asia/Kolkata")


def _validate_checksum(order_id: str, order_timestamp: str, received: str) -> bool:
    """Verify that the postback is genuinely from Zerodha."""
    if not KITE_API_SECRET:
        logger.warning("KITE_API_SECRET not set — skipping checksum validation.")
        return True  # Allow in dev; set the secret in production

    expected = hashlib.sha256(
        f"{order_id}{order_timestamp}{KITE_API_SECRET}".encode()
    ).hexdigest()

    return expected == received


@app.route("/postback", methods=["POST"])
def postback():
    """Receives order update postbacks from Zerodha."""
    data = request.form.to_dict() or request.json or {}

    if not data:
        logger.warning("Empty postback received.")
        return jsonify({"status": "error", "message": "empty payload"}), 400

    order_id        = data.get("order_id", "")
    order_timestamp = data.get("order_timestamp", "")
    status          = data.get("status", "")
    checksum        = data.get("checksum", "")
    transaction     = data.get("transaction_type", "")
    symbol          = data.get("tradingsymbol", "")
    product         = data.get("product", "")

    try:
        quantity      = float(data.get("filled_quantity", 0))
        average_price = float(data.get("average_price", 0))
    except (ValueError, TypeError):
        quantity, average_price = 0, 0

    logger.info(
        f"Postback received: {status} | {transaction} {quantity} {symbol} "
        f"@ ₹{average_price} | product={product}"
    )

    if not _validate_checksum(order_id, order_timestamp, checksum):
        logger.warning(f"Checksum mismatch for order {order_id} — rejecting.")
        return jsonify({"status": "error", "message": "invalid checksum"}), 403

    if status != "COMPLETE":
        logger.debug(f"Order {order_id} status is {status} — ignoring.")
        return jsonify({"status": "ok", "message": "ignored non-complete order"}), 200

    if product != "CNC":
        logger.debug(f"Order {order_id} product is {product} — ignoring (not CNC).")
        return jsonify({"status": "ok", "message": "ignored non-CNC order"}), 200

    if not symbol or quantity <= 0 or average_price <= 0:
        logger.warning(f"Invalid order data: {data}")
        return jsonify({"status": "error", "message": "invalid order data"}), 400

    process_trade(
        symbol           = symbol,
        transaction_type = transaction,
        quantity         = quantity,
        average_price    = average_price,
    )

    action  = "Bought" if transaction == "BUY" else "Sold"
    message = (
        f"Trade recorded: {action} {quantity:.0f} {symbol} @ ₹{average_price:.2f}\n"
        f"Holdings updated automatically."
    )
    send_alert(message, priority="low")

    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def index():
    return redirect("/holdings")


# ─── Kite OAuth Token Renewal ─────────────────────────────────────────────────

@app.route("/kite/renew", methods=["GET"])
def kite_renew():
    """Redirects user to Zerodha login for manual fallback."""
    from flask import redirect as flask_redirect
    from kite_client import kite
    return flask_redirect(kite.login_url())


@app.route("/kite/callback", methods=["GET"])
def kite_callback():
    """Handles callback from Zerodha and extracts access token."""
    from kite_client import kite, save_token

    request_token = request.args.get("request_token", "")
    status        = request.args.get("status", "")

    if status != "success" or not request_token:
        return (
            "<html><body style='background:#040712;color:#f87171;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2>Login failed or cancelled.</h2>"
            "<a href='/kite/renew' style='color:#6366f1;text-decoration:none;font-weight:600'>Try again →</a>"
            "</div></body></html>"
        ), 400

    try:
        session      = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = session["access_token"]
        save_token(access_token)
        logger.info("Kite access token renewed via OAuth callback.")
        return (
            "<html><body style='background:#040712;color:#e2e8f0;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='color:#10b981;font-size:56px;margin:0 0 16px'>✓</h1>"
            "<h2 style='margin:0 0 8px;font-weight:700'>Token Renewed Successfully</h2>"
            "<p style='color:#94a3b8;margin:0 0 24px'>You are authenticated for today's market session.</p>"
            "<a href='/holdings' style='background:#6366f1;color:#fff;padding:12px 24px;border-radius:10px;"
            "text-decoration:none;font-weight:600;display:inline-block;box-shadow:0 4px 12px rgba(99,102,241,0.25)'>"
            "Go to Dashboard</a>"
            "</div></body></html>"
        )
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return (
            f"<html><body style='background:#040712;color:#f87171;"
            f"font-family:sans-serif;display:flex;align-items:center;"
            f"justify-content:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><h2>Error: {e}</h2>"
            f"<a href='/kite/renew' style='color:#6366f1;text-decoration:none;font-weight:600'>Try again →</a>"
            f"</div></body></html>"
        ), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"}), 200


# ─── Simulated / Fallback Data Generators ─────────────────────────────────────

def get_simulated_intraday_data(symbol: str, current_price: float) -> list[dict]:
    """Generates a highly realistic 5-minute intraday chart for fallback."""
    import random
    
    if current_price <= 0:
        current_price = 100.0

    candles = []
    # 9:15 AM start of market
    base_time = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    
    # Generate up to 75 points representing 9:15 to 15:30 (5-min intervals)
    price = current_price * 0.985 # start slightly lower
    random.seed(hash(symbol)) # deterministic for symbol
    
    for i in range(75):
        t = base_time + timedelta(minutes=i*5)
        
        # Random walk with minor mean reversion
        change = (random.random() - 0.485) * (price * 0.004)
        pull = (current_price - price) * 0.04
        price += change + pull
        
        candles.append({
            "time": t.strftime("%H:%M"),
            "price": round(price, 2)
        })
        
        # Stop generating once we exceed current system time
        if t.time() > datetime.now().time():
            break
            
    return candles


# ─── REST API Endpoints ───────────────────────────────────────────────────────

@app.route("/api/data", methods=["GET"])
def api_data():
    """Returns combined payload of holdings, watchlist, P&L, logs, and server status."""
    from holdings_manager import load_holdings
    from watchlist_manager import load_watchlist
    from alerts import _load_log
    
    holdings  = load_holdings()
    watchlist = load_watchlist()
    log       = _load_log()
    settings  = load_settings()

    # Try cache first
    cache_file = os.path.join(DATA_DIR, "prices_cache.json")
    quotes     = {}
    updated_at = ""
    try:
        with open(cache_file, "r") as f:
            cache      = json.load(f)
            quotes     = cache.get("quotes", {})
            updated_at = cache.get("updated_at", "")
    except Exception:
        pass

    # Fallback to live fetch if cache empty
    all_symbols = list(set([h["symbol"] for h in holdings] + watchlist))
    if not quotes and all_symbols:
        import kite_client
        quotes = kite_client.get_quotes(all_symbols)
        for sym, q in quotes.items():
            high52, _ = kite_client.get_daily_stats(q.get("instrument_token"))
            q["week_high_52"] = high52

    # Calculate holding stats
    total_invested = 0
    total_current  = 0
    enriched_holdings = []
    
    for h in holdings:
        sym      = h["symbol"]
        qty      = h["quantity"]
        avg      = h["average_price"]
        q        = quotes.get(sym, {})
        ltp      = q.get("last_price", 0)
        invested = qty * avg
        current  = qty * ltp if ltp else invested
        pnl      = current - invested
        pnl_pct  = ((ltp - avg) / avg * 100) if avg and ltp else 0
        total_invested += invested
        total_current  += current
        
        enriched_holdings.append({
            "symbol": sym,
            "quantity": qty,
            "average_price": avg,
            "ltp": ltp,
            "invested": invested,
            "current": current,
            "pnl": pnl,
            "pnl_pct": pnl_pct
        })

    total_pnl     = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

    enriched_watchlist = []
    for sym in watchlist:
        q        = quotes.get(sym, {})
        ltp      = q.get("last_price", 0)
        high_52w = q.get("week_high_52", 0)
        drop_pct = ((high_52w - ltp) / high_52w * 100) if high_52w and ltp else 0
        in_buy_zone = drop_pct >= settings.get("WATCHLIST_DROP_THRESHOLD", 20.0)
        
        enriched_watchlist.append({
            "symbol": sym,
            "ltp": ltp,
            "week_high_52": high_52w,
            "drop_pct": drop_pct,
            "in_buy_zone": in_buy_zone
        })

    # Kite status
    import kite_client
    token_loaded = kite_client.load_token() is not None

    # Calculate active cooldowns
    active_cooldowns = {}
    alerts = log.get("alerts", {})
    cooldown_hours = settings.get("ALERT_COOLDOWN_HOURS", 4.0)
    now = datetime.now(IST)
    
    for key, timestamp in list(alerts.items()):
        try:
            sent_time = datetime.fromisoformat(timestamp).astimezone(IST)
            elapsed = (now - sent_time).total_seconds() / 3600
            if elapsed < cooldown_hours:
                active_cooldowns[key] = {
                    "sent_at": timestamp,
                    "remaining_mins": round((cooldown_hours - elapsed) * 60)
                }
        except Exception:
            pass

    sebi_seen = log.get("sebi_seen_ids", [])

    return jsonify({
        "holdings": enriched_holdings,
        "watchlist": enriched_watchlist,
        "portfolio": {
            "total_invested": total_invested,
            "total_current": total_current,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct
        },
        "settings": settings,
        "cooldowns": active_cooldowns,
        "sebi_seen_count": len(sebi_seen),
        "status": {
            "prices_updated_at": updated_at or "Live",
            "kite_authenticated": token_loaded,
            "kite_api_key_set": bool(kite_client.KITE_API_KEY)
        }
    })


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400
    if add_stock(symbol):
        return jsonify({"status": "ok", "message": f"Added {symbol} to watchlist"})
    return jsonify({"status": "error", "message": f"{symbol} is already in watchlist"}), 400


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    data = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400
    if remove_stock(symbol):
        return jsonify({"status": "ok", "message": f"Removed {symbol} from watchlist"})
    return jsonify({"status": "error", "message": f"{symbol} not found in watchlist"}), 400


@app.route("/api/holdings/update", methods=["POST"])
def api_holdings_update():
    data = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    qty = data.get("quantity")
    price = data.get("average_price")

    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400
    try:
        qty = float(qty)
        price = float(price)
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Quantity and price must be numbers"}), 400

    if qty <= 0 or price <= 0:
        return jsonify({"status": "error", "message": "Values must be positive"}), 400

    with holdings_lock:
        try:
            with open(HOLDINGS_FILE, "r") as f:
                holdings = json.load(f)
        except Exception:
            holdings = []

        found = False
        for h in holdings:
            if h["symbol"].upper().strip() == symbol:
                h["quantity"] = qty
                h["average_price"] = price
                found = True
                break
        if not found:
            holdings.append({
                "symbol": symbol,
                "quantity": qty,
                "average_price": price
            })
        _save_holdings(holdings)

    return jsonify({"status": "ok", "message": f"Successfully updated holding for {symbol}."})


@app.route("/api/holdings/delete", methods=["POST"])
def api_holdings_delete():
    data = request.json or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400

    with holdings_lock:
        try:
            with open(HOLDINGS_FILE, "r") as f:
                holdings = json.load(f)
        except Exception:
            holdings = []

        filtered = [h for h in holdings if h["symbol"].upper().strip() != symbol]
        if len(filtered) == len(holdings):
            return jsonify({"status": "error", "message": f"{symbol} not found in holdings"}), 400
        _save_holdings(filtered)

    return jsonify({"status": "ok", "message": f"Deleted holding for {symbol}."})


@app.route("/api/settings/update", methods=["POST"])
def api_settings_update():
    data = request.json or {}
    try:
        save_settings(data)
        # Apply dynamically to active config
        import config
        for k, v in data.items():
            if hasattr(config, k):
                setattr(config, k, float(v))
        # Apply to alerts
        try:
            import alerts
            for k, v in data.items():
                if hasattr(alerts, k):
                    setattr(alerts, k, float(v))
        except Exception:
            pass
        return jsonify({"status": "ok", "message": "Settings updated successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trigger-check", methods=["POST"])
def api_trigger_check():
    """Synchronously triggers the price monitoring checks, running in foreground."""
    try:
        import main
        main.run_market_checks()
        main.run_sebi_check()
        return jsonify({"status": "ok", "message": "Market and SEBI alerts checked successfully!"})
    except Exception as e:
        logger.error(f"Error triggering manual check: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/kite/renew-token", methods=["POST"])
def api_kite_renew_token():
    try:
        import kite_client
        kite_client.auto_login()
        return jsonify({"status": "ok", "message": "Kite token auto-renewed successfully!"})
    except Exception as e:
        logger.error(f"Kite auto-login failed: {e}")
        return jsonify({"status": "error", "message": f"Auto-login failed: {e}"}), 500


@app.route("/api/chart", methods=["GET"])
def api_chart():
    """Returns intraday 5-minute candle data for visual charting with dynamic fallback simulation."""
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400

    intraday = []
    try:
        import kite_client
        intraday = kite_client.get_intraday_data(symbol)
    except Exception as e:
        logger.warning(f"Failed to fetch live intraday for {symbol}: {e}")

    # Fallback simulation if live fetch fails (off-hours, weekend, token expired)
    if not intraday:
        from config import DATA_DIR
        cache_file = os.path.join(DATA_DIR, "prices_cache.json")
        ltp = 0.0
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
                ltp = cache.get("quotes", {}).get(symbol, {}).get("last_price", 0.0)
        except Exception:
            pass

        if ltp <= 0.0:
            try:
                import kite_client
                q = kite_client.get_quotes([symbol])
                ltp = q.get(symbol, {}).get("last_price", 0.0)
            except Exception:
                pass

        intraday = get_simulated_intraday_data(symbol, ltp)

    return jsonify({
        "symbol": symbol,
        "points": intraday
    })


# Company-name lookup improves Google News relevance for NSE tickers.
COMPANY_NAMES = {
    "INFY": "Infosys", "RELIANCE": "Reliance Industries", "TATAMOTORS": "Tata Motors",
    "TCS": "Tata Consultancy Services", "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank",
    "SBIN": "State Bank of India", "ITC": "ITC Limited", "BHARTIARTL": "Bharti Airtel",
    "HEROMOTOCO": "Hero MotoCorp", "TATAPOWER": "Tata Power", "ASIANPAINT": "Asian Paints",
    "LARSEN": "Larsen & Toubro", "LT": "Larsen & Toubro", "HINDUNILVR": "Hindustan Unilever",
    "AXISBANK": "Axis Bank", "KOTAKBANK": "Kotak Mahindra Bank", "WIPRO": "Wipro",
    "MARUTI": "Maruti Suzuki", "SUNPHARMA": "Sun Pharmaceutical", "BAJFINANCE": "Bajaj Finance",
    "IOC": "Indian Oil Corporation", "ITCHOTELS": "ITC Hotels",
    "GOLDBEES": "gold price India", "SILVERBEES": "silver price India",
}
_NEWS_CACHE = {}
_NEWS_TTL = 600  # seconds


@app.route("/api/news", methods=["GET"])
def api_news():
    """Aggregated company news for tracked stocks, sourced from Google News.

    Works for Indian/NSE equities (Finnhub's free tier does not). Returns the shape
    the dashboard already expects:
        {"news": [{headline, summary, source, url, datetime, image, stock_symbol}]}
    Used by the News tab (all tracked stocks) and the stock drawer (?symbol=XXX).
    """
    from scrapers import get_news_for_stock
    from holdings_manager import load_holdings
    from concurrent.futures import ThreadPoolExecutor
    from email.utils import parsedate_to_datetime

    symbol = request.args.get("symbol", "").upper().strip()

    if symbol:
        symbols = [symbol]
    else:
        holdings  = [h["symbol"] for h in load_holdings()]
        watchlist = load_watchlist()
        symbols   = list(dict.fromkeys(holdings + watchlist))  # dedupe, keep order

    if not symbols:
        return jsonify({"news": []})

    now_ts    = datetime.now(IST).timestamp()
    cache_key = ",".join(sorted(symbols))
    cached    = _NEWS_CACHE.get(cache_key)
    if cached and (now_ts - cached["ts"]) < _NEWS_TTL and not request.args.get("refresh"):
        return jsonify({"news": cached["news"]})

    def _fetch(sym):
        try:
            return sym, get_news_for_stock(sym, company_name=COMPANY_NAMES.get(sym), limit=8)
        except Exception:
            return sym, []

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for sym, items in pool.map(_fetch, symbols):
            for it in items:
                try:
                    ts = int(parsedate_to_datetime(it.get("published", "")).timestamp())
                except Exception:
                    ts = int(now_ts)
                title  = (it.get("title") or "").strip()
                source = it.get("source") or ""
                # Google News appends " - <source>" to titles; strip for a clean headline.
                if source and title.endswith(f" - {source}"):
                    title = title[: -(len(source) + 3)].strip()
                results.append({
                    "headline": title,
                    "summary": "",
                    "source": source or "Google News",
                    "url": it.get("link") or "",
                    "datetime": ts,
                    "image": "",
                    "stock_symbol": sym,
                })

    seen, unique = set(), []
    for art in sorted(results, key=lambda x: x["datetime"], reverse=True):
        u = art["url"]
        if u and u not in seen:
            seen.add(u)
            unique.append(art)

    unique = unique[:40]
    _NEWS_CACHE[cache_key] = {"ts": now_ts, "news": unique}
    return jsonify({"news": unique})


# ─── Beautiful Dashboard Page ─────────────────────────────────────────────────

@app.route("/holdings", methods=["GET"])
def view_holdings():
    """Live ultra-premium dashboard served as a Single Page Application (SPA)."""
    
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Terminal · Asset Monitor Console</title>
  
  <!-- Premium Fonts & Icons -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  
  <!-- ApexCharts CDN for Premium Visualisation -->
  <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>

  <style>
    :root {
      --bg: #030712;
      --surface: rgba(13, 20, 38, 0.45);
      --surface-hover: rgba(22, 34, 64, 0.6);
      --border: rgba(255, 255, 255, 0.05);
      --border-focus: rgba(99, 102, 241, 0.4);
      --text: #f8fafc;
      --text-muted: #64748b;
      --primary: #6366f1;
      --primary-rgb: 99, 102, 241;
      --emerald: #10b981;
      --emerald-rgb: 16, 185, 129;
      --rose: #f43f5e;
      --rose-rgb: 244, 63, 94;
      --amber: #f59e0b;
      --amber-rgb: 245, 158, 11;
      --sans-title: 'Outfit', sans-serif;
      --sans-body: 'Plus Jakarta Sans', sans-serif;
      --mono: 'JetBrains Mono', monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--sans-body);
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
      line-height: 1.5;
    }

    /* Cinematic Radial Blur Glows */
    body::before {
      content: "";
      position: fixed;
      top: -10%;
      left: -10%;
      width: 50%;
      height: 50%;
      background: radial-gradient(circle, rgba(99, 102, 241, 0.15) 0%, transparent 70%);
      filter: blur(100px);
      z-index: -1;
      pointer-events: none;
    }

    body::after {
      content: "";
      position: fixed;
      bottom: -10%;
      right: -10%;
      width: 50%;
      height: 50%;
      background: radial-gradient(circle, rgba(16, 185, 129, 0.08) 0%, transparent 70%);
      filter: blur(100px);
      z-index: -1;
      pointer-events: none;
    }

    /* Ticker Tape scrolling bar */
    .ticker-tape {
      background: rgba(3, 7, 18, 0.85);
      border-bottom: 1px solid var(--border);
      overflow: hidden;
      white-space: nowrap;
      padding: 10px 0;
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      backdrop-filter: blur(12px);
      z-index: 10;
      position: sticky;
      top: 0;
    }

    .ticker-content {
      display: inline-block;
      animation: ticker 35s linear infinite;
      padding-left: 100%;
    }

    @keyframes ticker {
      0% { transform: translate3d(0, 0, 0); }
      100% { transform: translate3d(-100%, 0, 0); }
    }

    .ticker-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-right: 32px;
      color: var(--text-muted);
    }

    .ticker-item .sym {
      color: var(--text);
    }

    .ticker-item.up { color: var(--emerald); }
    .ticker-item.down { color: var(--rose); }

    .container {
      max-width: 1240px;
      margin: 0 auto;
      padding: 24px 24px 80px;
    }

    /* Navbar Glass */
    header {
      background: rgba(13, 20, 38, 0.35);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 16px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 28px;
      backdrop-filter: blur(20px);
      box-shadow: 0 4px 30px rgba(0,0,0,0.1);
      flex-wrap: wrap;
      gap: 16px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
    }

    .logo-glow {
      width: 44px;
      height: 44px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--primary), var(--emerald));
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 0 20px rgba(99, 102, 241, 0.4);
    }

    .brand h1 {
      font-family: var(--sans-title);
      font-size: 20px;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .brand p {
      font-size: 12px;
      color: var(--text-muted);
      font-weight: 500;
    }

    .nav-actions {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    /* Custom Floating Action Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 10px 20px;
      border-radius: 12px;
      font-size: 13px;
      font-weight: 700;
      color: #fff;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border);
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      font-family: var(--sans-body);
    }

    .btn:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.15);
      transform: translateY(-2px);
    }

    .btn:active {
      transform: translateY(0);
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--primary), #4f46e5);
      border: none;
      box-shadow: 0 4px 15px rgba(99, 102, 241, 0.25);
    }

    .btn-primary:hover {
      background: linear-gradient(135deg, #4f46e5, #4338ca);
      box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
    }

    .btn-emerald {
      background: linear-gradient(135deg, var(--emerald), #059669);
      border: none;
      box-shadow: 0 4px 15px rgba(16, 185, 129, 0.2);
    }

    .btn-emerald:hover {
      background: linear-gradient(135deg, #059669, #047857);
      box-shadow: 0 6px 20px rgba(16, 185, 129, 0.35);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border);
      border-radius: 99px;
      font-size: 11.5px;
      font-weight: 600;
      color: var(--text-muted);
    }

    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      display: inline-block;
    }

    .status-dot.active {
      background-color: var(--emerald);
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.8);
      animation: pulse 1.8s infinite;
    }

    .status-dot.inactive {
      background-color: var(--rose);
      box-shadow: 0 0 10px rgba(244, 63, 94, 0.8);
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.6); }
      70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    /* Broad Market Overview Mini Cards */
    .market-overview-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      margin-bottom: 24px;
    }

    @media (max-width: 768px) {
      .market-overview-grid {
        grid-template-columns: 1fr;
      }
    }

    .market-mini-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px 20px;
      backdrop-filter: blur(15px);
      display: flex;
      justify-content: space-between;
      align-items: center;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .market-mini-card:hover {
      border-color: rgba(255,255,255,0.1);
      transform: translateY(-2px);
    }

    .market-info .name {
      font-size: 11.5px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .market-info .val {
      font-family: var(--sans-title);
      font-size: 19px;
      font-weight: 800;
      margin: 4px 0 2px;
    }

    .market-info .pct {
      font-size: 11.5px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }

    .market-sparkline {
      width: 100px;
      height: 40px;
    }

    /* Hero Net Worth & Distribution Console */
    .hero-analytics-deck {
      display: grid;
      grid-template-columns: 3fr 2fr;
      gap: 20px;
      margin-bottom: 28px;
    }

    @media (max-width: 900px) {
      .hero-analytics-deck {
        grid-template-columns: 1fr;
      }
    }

    .glass-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 32px;
      backdrop-filter: blur(24px);
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25), inset 0 1px 0 rgba(255,255,255,0.03);
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .glass-card:hover {
      border-color: rgba(255,255,255,0.1);
    }

    .networth-panel {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    .networth-header .label {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin-bottom: 8px;
    }

    .networth-header .value {
      font-family: var(--sans-title);
      font-size: 52px;
      font-weight: 900;
      letter-spacing: -0.04em;
      line-height: 1;
      margin-bottom: 12px;
      background: linear-gradient(to right, #ffffff, #cbd5e1);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .hero-pnl-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 16px;
      border-radius: 99px;
      font-size: 13.5px;
      font-weight: 800;
    }

    .hero-pnl-badge.up {
      background: rgba(16, 185, 129, 0.12);
      color: var(--emerald);
      box-shadow: 0 0 15px rgba(16, 185, 129, 0.1);
    }

    .hero-pnl-badge.down {
      background: rgba(244, 63, 94, 0.12);
      color: var(--rose);
      box-shadow: 0 0 15px rgba(244, 63, 94, 0.1);
    }

    .networth-subrow {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      margin-top: 36px;
    }

    .metric-subbox {
      border-left: 2px solid rgba(255, 255, 255, 0.05);
      padding-left: 16px;
    }

    .metric-subbox .lbl {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }

    .metric-subbox .val {
      font-family: var(--sans-title);
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
    }

    .chart-card {
      display: flex;
      flex-direction: column;
      justify-content: center;
      padding: 24px 28px;
    }

    .chart-header {
      font-family: var(--sans-title);
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 12px;
      text-align: center;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    /* Glassmorphic Tabs Controller */
    .tabs-bar {
      display: flex;
      background: rgba(13, 20, 38, 0.3);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 6px;
      margin-bottom: 24px;
      gap: 6px;
    }

    .tab-trigger {
      flex: 1;
      padding: 12px 16px;
      border-radius: 12px;
      border: none;
      background: none;
      color: var(--text-muted);
      font-family: var(--sans-title);
      font-size: 13.5px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    .tab-trigger:hover {
      color: var(--text);
      background: rgba(255,255,255,0.02);
    }

    .tab-trigger.active {
      background: var(--primary);
      color: #fff;
      box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35);
    }

    .tab-content {
      display: none;
    }

    .tab-content.active {
      display: block;
      animation: fadeIn 0.4s ease-out;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }

    /* Dynamic Search Filters */
    .table-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      gap: 16px;
      flex-wrap: wrap;
    }

    .search-wrapper {
      position: relative;
      flex-grow: 1;
      max-width: 320px;
    }

    .search-wrapper svg {
      position: absolute;
      left: 14px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      width: 16px;
      height: 16px;
    }

    .search-input {
      width: 100%;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 14px 10px 42px;
      color: var(--text);
      font-family: var(--sans-body);
      font-size: 13.5px;
      transition: all 0.2s;
    }

    .search-input:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(99,102,241,0.15);
    }

    /* Interactive Table Styling */
    .table-panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 24px;
      backdrop-filter: blur(20px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
    }

    .table-container {
      overflow-x: auto;
      width: 100%;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }

    th {
      padding: 14px 18px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }

    td {
      padding: 16px 18px;
      font-size: 13.5px;
      font-weight: 500;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
      vertical-align: middle;
      cursor: pointer;
    }

    tr:last-child td {
      border-bottom: none;
    }

    tbody tr {
      transition: all 0.2s ease;
    }

    tbody tr:hover {
      background: rgba(255, 255, 255, 0.015);
    }

    .num {
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-family: var(--mono);
      font-size: 13px;
    }

    .sym-name-cell {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .circle-avatar {
      width: 34px;
      height: 34px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--primary), #8b5cf6);
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: var(--sans-title);
      font-size: 12px;
      font-weight: 800;
      color: #fff;
      box-shadow: 0 4px 10px rgba(99, 102, 241, 0.2);
    }

    .circle-avatar.wl {
      background: linear-gradient(135deg, #0ea5e9, #06b6d4);
      box-shadow: 0 4px 10px rgba(14, 165, 233, 0.2);
    }

    .stock-ticker {
      font-family: var(--sans-title);
      font-weight: 800;
      color: var(--text);
      font-size: 14.5px;
    }

    .interactive-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 12px;
      border-radius: 8px;
      font-size: 11.5px;
      font-weight: 800;
      font-family: var(--mono);
    }

    .interactive-pill.up {
      background: rgba(16, 185, 129, 0.12);
      color: var(--emerald);
    }

    .interactive-pill.down {
      background: rgba(244, 63, 94, 0.12);
      color: var(--rose);
    }

    .interactive-pill.warning {
      background: rgba(245, 158, 11, 0.12);
      color: var(--amber);
      animation: flashPulse 2s infinite;
    }

    @keyframes flashPulse {
      0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4); }
      70% { box-shadow: 0 0 0 6px rgba(245, 158, 11, 0); }
      100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
    }

    .btn-trash {
      background: none;
      border: none;
      cursor: pointer;
      color: var(--text-muted);
      padding: 8px;
      border-radius: 10px;
      transition: all 0.2s;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    .btn-trash:hover {
      background: rgba(244, 63, 94, 0.1);
      color: var(--rose);
    }

    /* Watchlist Visual Progress Cards Grid */
    .watchlist-cards-layout {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
    }

    .watchlist-gauge-card {
      background: rgba(13, 20, 38, 0.35);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 20px;
      backdrop-filter: blur(15px);
      position: relative;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      cursor: pointer;
    }

    .watchlist-gauge-card:hover {
      border-color: rgba(255,255,255,0.08);
      transform: translateY(-4px);
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
    }

    .gauge-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }

    .gauge-top .title-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .gauge-meta {
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 12px;
      font-family: var(--mono);
    }

    .gauge-meta span strong {
      color: var(--text);
    }

    /* Custom Progress Bar with Glowing effects */
    .progress-bar-container {
      width: 100%;
      height: 8px;
      background: rgba(255, 255, 255, 0.05);
      border-radius: 99px;
      overflow: hidden;
      margin-top: 14px;
      position: relative;
    }

    .progress-bar-fill {
      height: 100%;
      border-radius: 99px;
      width: 0%;
      transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
    }

    /* Gradient fills depending on proximity */
    .progress-bar-fill.normal {
      background: linear-gradient(90deg, var(--primary), #8b5cf6);
      box-shadow: 0 0 8px rgba(99, 102, 241, 0.4);
    }

    .progress-bar-fill.warn {
      background: linear-gradient(90deg, var(--amber), #fb923c);
      box-shadow: 0 0 8px rgba(245, 158, 11, 0.4);
    }

    .progress-bar-fill.hot-active {
      background: linear-gradient(90deg, var(--emerald), #34d399);
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.6);
    }

    .gauge-pnl-label {
      font-size: 12px;
      font-weight: 700;
      font-family: var(--mono);
    }

    .gauge-pnl-label.buy { color: var(--emerald); }
    .gauge-pnl-label.wait { color: var(--text-muted); }

    /* News Grid & Articles Layout */
    .news-grid-layout {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 20px;
    }

    .news-article-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 20px;
      overflow: hidden;
      backdrop-filter: blur(15px);
      display: flex;
      flex-direction: column;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
    }

    .news-article-card:hover {
      border-color: rgba(99, 102, 241, 0.25);
      transform: translateY(-4px);
      box-shadow: 0 12px 30px rgba(99, 102, 241, 0.1);
    }

    .news-image {
      width: 100%;
      height: 170px;
      object-fit: cover;
      background-color: rgba(255, 255, 255, 0.02);
    }

    .news-content {
      padding: 20px;
      display: flex;
      flex-direction: column;
      flex-grow: 1;
    }

    .news-meta-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--text-muted);
      margin-bottom: 10px;
      font-family: var(--mono);
    }

    .news-badge {
      font-size: 10px;
      font-weight: 800;
      background: rgba(99, 102, 241, 0.12);
      color: var(--primary);
      padding: 2px 7px;
      border-radius: 6px;
    }

    .news-headline {
      font-family: var(--sans-title);
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      line-height: 1.4;
      margin-bottom: 8px;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .news-summary {
      font-size: 12.5px;
      color: var(--text-muted);
      line-height: 1.5;
      margin-bottom: 16px;
      flex-grow: 1;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .news-btn {
      margin-top: auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 10px 16px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border);
      border-radius: 10px;
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
      text-decoration: none;
      transition: all 0.2s;
    }

    .news-btn:hover {
      background: rgba(99, 102, 241, 0.1);
      border-color: var(--primary);
      color: #fff;
    }

    /* Sliding Details Drawer from right */
    .drawer-overlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(3, 7, 18, 0.5);
      backdrop-filter: blur(10px);
      z-index: 200;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .drawer-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }

    .glass-drawer {
      position: absolute;
      top: 0;
      right: 0;
      height: 100%;
      width: 100%;
      max-width: 500px;
      background: #070a13;
      border-left: 1px solid var(--border);
      box-shadow: -20px 0 50px rgba(0, 0, 0, 0.5);
      transform: translateX(100%);
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      flex-direction: column;
    }

    .drawer-overlay.active .glass-drawer {
      transform: translateX(0);
    }

    .drawer-header {
      padding: 24px 28px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .drawer-brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .drawer-body {
      padding: 28px;
      overflow-y: auto;
      flex-grow: 1;
    }

    .drawer-price-card {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: 18px;
      padding: 20px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .drawer-price-card .price-val {
      font-family: var(--sans-title);
      font-size: 32px;
      font-weight: 800;
      color: var(--text);
      line-height: 1;
    }

    .drawer-price-card .change-pct {
      font-family: var(--mono);
      font-size: 14px;
      font-weight: 800;
      padding: 6px 14px;
      border-radius: 99px;
    }

    .drawer-chart-container {
      margin-bottom: 24px;
      background: rgba(255, 255, 255, 0.01);
      border: 1px solid rgba(255, 255, 255, 0.03);
      border-radius: 18px;
      padding: 16px;
    }

    .drawer-stats-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 12px;
      margin-bottom: 28px;
    }

    .stat-cell {
      background: rgba(255, 255, 255, 0.015);
      border: 1px solid rgba(255, 255, 255, 0.03);
      border-radius: 12px;
      padding: 12px 16px;
    }

    .stat-cell .lbl {
      font-size: 10px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }

    .stat-cell .val {
      font-family: var(--sans-title);
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
    }

    /* Alert Timeline log feed */
    .timeline-log {
      position: relative;
      padding-left: 28px;
      margin-left: 12px;
      border-left: 1px solid var(--border);
    }

    .timeline-card {
      position: relative;
      background: rgba(255, 255, 255, 0.015);
      border: 1px solid rgba(255,255,255,0.03);
      border-radius: 16px;
      padding: 16px 20px;
      margin-bottom: 16px;
      transition: all 0.2s ease;
    }

    .timeline-card:hover {
      background: rgba(255, 255, 255, 0.03);
      border-color: var(--border);
    }

    .timeline-node {
      position: absolute;
      left: -35px;
      top: 20px;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--bg);
      border: 3px solid var(--primary);
    }

    .timeline-node.alert { border-color: var(--amber); }
    .timeline-node.filing { border-color: var(--primary); }

    .timeline-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }

    .timeline-badge {
      font-size: 10px;
      font-weight: 800;
      padding: 3px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .timeline-badge.warn { background: var(--amber-glow); color: var(--amber); }
    .timeline-badge.info { background: var(--primary-glow); color: var(--primary); }

    .timeline-time {
      font-size: 11px;
      color: var(--text-muted);
      font-family: var(--mono);
    }

    .timeline-body {
      font-size: 13px;
      color: var(--text);
      white-space: pre-line;
      font-weight: 500;
    }

    /* Range Sliders Controls */
    .slider-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 20px;
    }

    @media (max-width: 768px) {
      .slider-grid {
        grid-template-columns: 1fr;
      }
    }

    .slider-box {
      background: rgba(255,255,255,0.015);
      border: 1px solid rgba(255,255,255,0.03);
      border-radius: 16px;
      padding: 20px;
    }

    .slider-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }

    .slider-name {
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text);
    }

    .slider-current-val {
      font-size: 14px;
      font-weight: 800;
      color: var(--primary);
      font-family: var(--mono);
    }

    .slider-desc {
      font-size: 11.5px;
      color: var(--text-muted);
      margin-bottom: 16px;
    }

    .styled-slider {
      -webkit-appearance: none;
      appearance: none;
      width: 100%;
      height: 6px;
      border-radius: 99px;
      background: rgba(255, 255, 255, 0.08);
      outline: none;
      cursor: pointer;
    }

    .styled-slider::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: var(--primary);
      box-shadow: 0 0 10px var(--primary);
      transition: transform 0.1s;
    }

    .styled-slider::-webkit-slider-thumb:hover {
      transform: scale(1.25);
    }

    /* Modal Form Overlay */
    .modal-overlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(3, 7, 18, 0.6);
      backdrop-filter: blur(10px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 100;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .modal-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }

    .glass-modal {
      background: #090e1d;
      border: 1px solid var(--border);
      border-radius: 24px;
      width: 100%;
      max-width: 420px;
      padding: 32px;
      box-shadow: 0 24px 64px rgba(0, 0, 0, 0.6);
      transform: translateY(-24px);
      transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    .modal-overlay.active .glass-modal {
      transform: translateY(0);
    }

    .modal-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
    }

    .modal-head h3 {
      font-family: var(--sans-title);
      font-size: 20px;
      font-weight: 800;
    }

    .btn-close-modal {
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 24px;
      padding: 4px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      line-height: 1;
    }

    .btn-close-modal:hover {
      color: var(--text);
    }

    .form-row {
      margin-bottom: 18px;
    }

    .form-row label {
      display: block;
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .form-input {
      width: 100%;
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 16px;
      color: var(--text);
      font-family: var(--sans-body);
      font-size: 14px;
      transition: all 0.2s;
    }

    .form-input:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
    }

    /* Toast styling */
    #toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: rgba(9, 14, 29, 0.95);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 20px;
      color: #fff;
      font-size: 13.5px;
      font-weight: 600;
      box-shadow: 0 16px 36px rgba(0,0,0,0.4);
      backdrop-filter: blur(12px);
      z-index: 1000;
      display: flex;
      align-items: center;
      gap: 12px;
      transform: translateY(100px);
      opacity: 0;
      transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    }

    #toast.show {
      transform: translateY(0);
      opacity: 1;
    }

    #toast .toast-icon {
      font-size: 18px;
    }

    #toast.success { border-left: 4px solid var(--emerald); }
    #toast.error { border-left: 4px solid var(--rose); }
    #toast.info { border-left: 4px solid var(--primary); }

    .empty-message {
      text-align: center;
      padding: 40px 16px;
      color: var(--text-muted);
      font-size: 14px;
      font-weight: 500;
    }

    /* Pulsing loading skeletons */
    .skeleton-line {
      height: 14px;
      background: rgba(255, 255, 255, 0.04);
      border-radius: 6px;
      margin: 8px 0;
      animation: skeletonPulse 1.5s infinite ease-in-out;
    }

    @keyframes skeletonPulse {
      0% { opacity: 0.5; }
      50% { opacity: 1; }
      100% { opacity: 0.5; }
    }

    footer {
      text-align: center;
      color: var(--text-muted);
      font-size: 11.5px;
      margin-top: 48px;
      border-top: 1px solid var(--border);
      padding-top: 24px;
      font-family: var(--mono);
    }
  </style>
</head>
<body>

  <!-- Ticker Tape scrolling major trackers -->
  <div class="ticker-tape">
    <div class="ticker-content" id="live-ticker-tape">
      <span class="ticker-item"><span class="sym">NIFTY 50</span> 22,456.80 <span class="pct up">▲ +0.56%</span></span>
      <span class="ticker-item"><span class="sym">SENSEX</span> 73,880.20 <span class="pct up">▲ +0.52%</span></span>
      <span class="ticker-item"><span class="sym">NIFTY BANK</span> 48,120.40 <span class="pct down">▼ -0.17%</span></span>
    </div>
  </div>

  <div class="container">
    
    <!-- Navbar Navigation glass -->
    <header>
      <div class="brand">
        <div class="logo-glow">📈</div>
        <div>
          <h1>Stock Alert Console</h1>
          <p id="system-time">Synchronising terminal data...</p>
        </div>
      </div>
      <div class="nav-actions">
        <div class="status-pill">
          <span id="auth-dot" class="status-dot"></span>
          <span id="auth-text">Kite Client</span>
        </div>
        <button id="btn-scan" class="btn btn-primary" onclick="triggerScan()">
          <svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/></svg>
          Run Scanning Scan
        </button>
        <button id="btn-oauth" class="btn" onclick="renewKiteSession()">
          <svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.02 5.912L9 18.75V21h-2.25v-2.25H4.5V16.5h-2.25v-2.25l6.838-6.837A6 6 0 0115.75 5.25z"/></svg>
          OAuth Session
        </button>
      </div>
    </header>

    <!-- Broad Market Indices & Scrapes cards -->
    <div class="market-overview-grid">
      <div class="market-mini-card">
        <div class="market-info">
          <div class="name">Nifty 50</div>
          <div class="val" id="nifty-val">22,456.80</div>
          <div class="pct text-up" id="nifty-pct">▲ +125.40 (+0.56%)</div>
        </div>
        <div class="market-sparkline" id="nifty-sparkline"></div>
      </div>

      <div class="market-mini-card">
        <div class="market-info">
          <div class="name">Sensex</div>
          <div class="val" id="sensex-val">73,880.20</div>
          <div class="pct text-up" id="sensex-pct">▲ +380.10 (+0.52%)</div>
        </div>
        <div class="market-sparkline" id="sensex-sparkline"></div>
      </div>

      <div class="market-mini-card">
        <div class="market-info">
          <div class="name">FII / DII net cash Flow</div>
          <div class="val" id="fiidii-net-val">₹0 Cr</div>
          <div class="pct text-muted" id="fiidii-status">Waiting daily run...</div>
        </div>
        <div style="font-size:24px">🏢</div>
      </div>
    </div>

    <!-- Main Analytics Console (Networth + Allocation Donut) -->
    <div class="hero-analytics-deck">
      <div class="glass-card networth-panel">
        <div class="networth-header">
          <div class="label">Net Investment Current Value</div>
          <div id="networth-val" class="value">₹0.00</div>
          <div id="networth-pnl" class="hero-pnl-badge">₹0.00 (0.00%)</div>
        </div>

        <div class="networth-subrow">
          <div class="metric-subbox">
            <div class="lbl">Capital Invested</div>
            <div id="metric-invested" class="val">₹0.00</div>
          </div>
          <div class="metric-subbox">
            <div class="lbl">Holdings Count</div>
            <div id="metric-holdings" class="val">0</div>
          </div>
          <div class="metric-subbox">
            <div class="lbl">Watchlist Size</div>
            <div id="metric-watchlist" class="val">0</div>
          </div>
        </div>
      </div>

      <!-- Portfolio Distribution Donut Chart -->
      <div class="glass-card chart-card" id="allocation-chart-card">
        <div class="chart-header">Asset Allocation Share</div>
        <div id="donut-chart-container" style="min-height: 220px;"></div>
      </div>
    </div>

    <!-- Interactive Workspace tab bar -->
    <div class="tabs-bar">
      <button class="tab-trigger active" onclick="switchTab(event, 'holdings-tab')">
        📁 Portfolio Holdings
      </button>
      <button class="tab-trigger" onclick="switchTab(event, 'watchlist-tab')">
        🎯 Buy Zone Watchlist
      </button>
      <button class="tab-trigger" onclick="switchTab(event, 'news-tab'); loadNewsFeed()">
        📰 Ticker News Desk
      </button>
      <button class="tab-trigger" onclick="switchTab(event, 'timeline-tab')">
        🔔 Scrapers Alert Center
      </button>
      <button class="tab-trigger" onclick="switchTab(event, 'settings-tab')">
        ⚙️ Settings Panel
      </button>
    </div>

    <!-- Tab 1: Holdings Console -->
    <div id="holdings-tab" class="tab-content active">
      <div class="table-panel">
        <div class="table-toolbar">
          <div class="search-wrapper">
            <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
            <input type="text" class="search-input" id="search-holdings" placeholder="Filter holdings by symbol..." onkeyup="filterHoldingsTable()">
          </div>
          <button class="btn btn-emerald" onclick="openHoldingModal()">
            <svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
            Record Manual Trade
          </button>
        </div>

        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Stock Asset</th>
                <th class="num">Shares Quantity</th>
                <th class="num">Average Paid</th>
                <th class="num">Live LTP</th>
                <th class="num">Return Rate</th>
                <th class="num">Net Profit &amp; Loss</th>
                <th style="width: 50px"></th>
              </tr>
            </thead>
            <tbody id="holdings-tbody">
              <tr>
                <td colspan="7">
                  <div class="skeleton-line"></div>
                  <div class="skeleton-line"></div>
                  <div class="skeleton-line"></div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Tab 2: Watchlist Progress Cards -->
    <div id="watchlist-tab" class="tab-content">
      <div class="table-toolbar" style="margin-bottom: 20px;">
        <h2 style="font-family: var(--sans-title); font-size:18px; font-weight:700;">Monitored Drop Target Gauges</h2>
        <button class="btn btn-primary" onclick="openWatchlistModal()">
          <svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
          Watch New Ticker
        </button>
      </div>

      <!-- Watchlist Gauge Layout -->
      <div class="watchlist-cards-layout" id="watchlist-gauge-container">
        <div class="empty-message">Watchlist data syncing...</div>
      </div>
    </div>

    <!-- Tab 3: Company News (Google News) -->
    <div id="news-tab" class="tab-content">
      <div class="table-toolbar" style="margin-bottom: 24px;">
        <div>
          <h2 style="font-family: var(--sans-title); font-size:18px; font-weight:700;">Stock Intelligence News Feed</h2>
          <p class="panel-subtitle">Recent headlines for your holdings &amp; watchlist, via Google News</p>
        </div>
        <div style="max-width: 240px; margin-left: auto; width: 100%;">
          <select class="form-input" id="news-symbol-filter" onchange="loadNewsFeed(this.value)" style="padding: 10px 14px;">
            <option value="">All Tracked Stocks</option>
          </select>
        </div>
      </div>

      <div class="news-grid-layout" id="news-articles-container">
        <div class="empty-message">Draping publications list...</div>
      </div>
    </div>

    <!-- Tab 4: Timeline & Cooldowns -->
    <div id="timeline-tab" class="tab-content">
      <div class="dashboard-grid">
        <div class="glass-card" style="padding: 24px; grid-column: span 2;">
          <h2 class="panel-title" style="margin-bottom: 20px;">
            📢 Cooldown Silences & Announcement Tracker
          </h2>
          <div class="timeline-log" id="system-timeline-feed">
            <div class="empty-message">Scrapers timeline empty. Run an alert scan.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab 5: Settings Thresholds Desk -->
    <div id="settings-tab" class="tab-content">
      <div class="glass-card" style="padding: 28px;">
        <h2 class="panel-title" style="margin-bottom: 20px;">⚙️ Calibration of Alerts Threshold</h2>
        
        <div class="slider-grid">
          <div class="slider-box">
            <div class="slider-header">
              <span class="slider-name">Holdings Drop Trigger</span>
              <span id="indicator-holdings" class="slider-current-val">20%</span>
            </div>
            <div class="slider-desc">Trigger alert when a portfolio asset drops below cost basis.</div>
            <input type="range" id="input-holdings" class="styled-slider" min="5" max="50" value="20" oninput="updateIndicator('holdings', this.value + '%')">
          </div>

          <div class="slider-box">
            <div class="slider-header">
              <span class="slider-name">Watchlist Drop Threshold</span>
              <span id="indicator-watchlist" class="slider-current-val">20%</span>
            </div>
            <div class="slider-desc">Buy zone trigger drop from 52-week high to accumulate asset.</div>
            <input type="range" id="input-watchlist" class="styled-slider" min="5" max="50" value="20" oninput="updateIndicator('watchlist', this.value + '%')">
          </div>

          <div class="slider-box">
            <div class="slider-header">
              <span class="slider-name">Volume Spike Multiplier</span>
              <span id="indicator-vol" class="slider-current-val">3x</span>
            </div>
            <div class="slider-desc">Alert when daily volumes spikes unusually compared to average.</div>
            <input type="range" id="input-vol" class="styled-slider" min="1.5" max="10" step="0.5" value="3.0" oninput="updateIndicator('vol', this.value + 'x')">
          </div>

          <div class="slider-box">
            <div class="slider-header">
              <span class="slider-name">FII / DII Net Flow</span>
              <span id="indicator-fiidii" class="slider-current-val">₹3,000 Cr</span>
            </div>
            <div class="slider-desc">Inflow/outflow cash limit of daily institutional players.</div>
            <input type="range" id="input-fiidii" class="styled-slider" min="500" max="10000" step="500" value="3000" oninput="updateIndicator('fiidii', '₹' + Number(this.value).toLocaleString() + ' Cr')">
          </div>

          <div class="slider-box" style="grid-column: span 2;">
            <div class="slider-header">
              <span class="slider-name">Alert Suppression Silencer</span>
              <span id="indicator-cooldown" class="slider-current-val">4.0 Hours</span>
            </div>
            <div class="slider-desc">Hours to suppress repeat alert categories for a single stock ticker.</div>
            <input type="range" id="input-cooldown" class="styled-slider" min="0.5" max="24" step="0.5" value="4.0" oninput="updateIndicator('cooldown', this.value + ' Hours')">
          </div>
        </div>

        <button class="btn btn-primary" style="width: 100%; margin-top:24px; padding: 12px 24px;" onclick="saveCalibrationSettings()">
          Deploy Threshold calibration Profile
        </button>
      </div>
    </div>

    <footer>
      <p>STOCK MARKET ALERT CONSOLE v2.0 · CORE DEPLOYMENT SYNCED VIA FLASK TERMINAL</p>
    </footer>

  </div>

  <!-- Modal Overlay: Log Investment Trade -->
  <div id="modal-holding" class="modal-overlay">
    <div class="glass-modal">
      <div class="modal-head">
        <h3>Record Portfolio Trade</h3>
        <button class="btn-close-modal" onclick="closeHoldingModal()">&times;</button>
      </div>
      <div class="form-row">
        <label for="f-hold-symbol">Stock Asset Ticker</label>
        <input type="text" id="f-hold-symbol" class="form-input" placeholder="e.g. INFY, ITC" required>
      </div>
      <div class="form-row">
        <label for="f-hold-qty">Shares Quantity</label>
        <input type="number" id="f-hold-qty" class="form-input" placeholder="Shares purchased" min="0.001" step="any" required>
      </div>
      <div class="form-row">
        <label for="f-hold-price">Average Price Paid (₹)</label>
        <input type="number" id="f-hold-price" class="form-input" placeholder="Average share price" min="0.01" step="any" required>
      </div>
      <button class="btn btn-emerald" style="width: 100%; padding: 12px 24px;" onclick="submitTradeLog()">
        Record Transaction
      </button>
    </div>
  </div>

  <!-- Modal Overlay: Watch stock ticker -->
  <div id="modal-watchlist" class="modal-overlay">
    <div class="glass-modal">
      <div class="modal-head">
        <h3>Watch Stock Ticker</h3>
        <button class="btn-close-modal" onclick="closeWatchlistModal()">&times;</button>
      </div>
      <div class="form-row">
        <label for="f-wl-symbol">Stock Ticker Symbol</label>
        <input type="text" id="f-wl-symbol" class="form-input" placeholder="e.g. RELIANCE, HDFCBANK" required>
      </div>
      <button class="btn btn-primary" style="width: 100%; padding: 12px 24px;" onclick="submitWatchlistLog()">
        Add to Watchlist
      </button>
    </div>
  </div>

  <!-- Sliding Ticker Details Side Drawer from right -->
  <div id="details-drawer" class="drawer-overlay" onclick="closeDetailsDrawer(event)">
    <div class="glass-drawer" onclick="event.stopPropagation()">
      <div class="drawer-header">
        <div class="drawer-brand">
          <span class="circle-avatar" id="drawer-avatar">RI</span>
          <div>
            <h3 id="drawer-symbol">RELIANCE</h3>
            <p id="drawer-description" class="panel-subtitle">Intraday Trading Session</p>
          </div>
        </div>
        <button class="btn-close-modal" onclick="closeDetailsDrawer(event)">&times;</button>
      </div>

      <div class="drawer-body">
        <!-- Live Intraday Price Card -->
        <div class="drawer-price-card">
          <div id="drawer-ltp" class="price-val">₹0.00</div>
          <div id="drawer-change" class="change-pct">▲ +0.0%</div>
        </div>

        <!-- Price Intraday chart (ApexCharts) -->
        <div class="drawer-chart-container">
          <div class="chart-header" style="text-align:left; margin-bottom:12px;">Live Intraday Trend (5-min interval)</div>
          <div id="intraday-chart-div" style="min-height: 230px;"></div>
        </div>

        <!-- Custom Asset Metrics Grid -->
        <div class="drawer-stats-grid">
          <div class="stat-cell">
            <div class="lbl">Capital Allocated</div>
            <div id="d-stat-invested" class="val">₹0.00</div>
          </div>
          <div class="stat-cell">
            <div class="lbl">Total Shares Held</div>
            <div id="d-stat-qty" class="val">0</div>
          </div>
          <div class="stat-cell">
            <div class="lbl">Average cost price</div>
            <div id="d-stat-avg" class="val">₹0.00</div>
          </div>
          <div class="stat-cell">
            <div class="lbl">Accumulated P&amp;L</div>
            <div id="d-stat-pnl" class="val">₹0.00</div>
          </div>
        </div>

        <!-- Company News inside Drawer -->
        <div class="drawer-news-section" style="margin-top: 24px;">
          <h4 style="font-family: var(--sans-title); font-size:14px; font-weight:700; color:var(--text); margin-bottom:12px; text-transform:uppercase; letter-spacing:0.05em;">Related Stock Publications</h4>
          <div class="alerts-feed" id="drawer-news-feed" style="max-height: 320px; overflow-y:auto;">
            <div class="empty-message">No specific publications mapped.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Toast Feed System -->
  <div id="toast">
    <span class="toast-icon">ℹ️</span>
    <span class="toast-text">Message</span>
  </div>

  <script>
    // Live Toast Feedback
    function pushToast(text, type = 'info') {
      const toast = document.getElementById('toast');
      const textEl = toast.querySelector('.toast-text');
      const iconEl = toast.querySelector('.toast-icon');
      
      textEl.innerText = text;
      toast.className = 'show ' + type;
      
      if (type === 'success') iconEl.innerText = '✅';
      else if (type === 'error') iconEl.innerText = '❌';
      else iconEl.innerText = '📡';

      setTimeout(() => {
        toast.className = '';
      }, 4000);
    }

    // Modal Control Deck
    function openHoldingModal() { document.getElementById('modal-holding').classList.add('active'); }
    function closeHoldingModal() { document.getElementById('modal-holding').classList.remove('active'); }
    function openWatchlistModal() { document.getElementById('modal-watchlist').classList.add('active'); }
    function closeWatchlistModal() { document.getElementById('modal-watchlist').classList.remove('active'); }

    function updateIndicator(id, val) {
      document.getElementById('indicator-' + id).innerText = val;
    }

    // Switching dynamic workspace tabs
    function switchTab(evt, tabId) {
      const tabContents = document.getElementsByClassName('tab-content');
      for (let i = 0; i < tabContents.length; i++) {
        tabContents[i].classList.remove('active');
      }
      
      const tabTriggers = document.getElementsByClassName('tab-trigger');
      for (let i = 0; i < tabTriggers.length; i++) {
        tabTriggers[i].classList.remove('active');
      }

      document.getElementById(tabId).classList.add('active');
      evt.currentTarget.classList.add('active');
    }

    // ApexCharts Donut Distribution
    let distributionChart = null;

    function renderDonutAllocation(holdings) {
      if (holdings.length === 0) {
        document.getElementById('allocation-chart-card').style.display = 'none';
        return;
      }
      document.getElementById('allocation-chart-card').style.display = 'flex';

      const labels = holdings.map(h => h.symbol);
      const series = holdings.map(h => h.current);

      const options = {
        chart: {
          type: 'donut',
          height: 240,
          background: 'transparent',
          foreColor: '#94a3b8',
          fontFamily: 'Plus Jakarta Sans',
          animations: { speed: 600 }
        },
        series: series,
        labels: labels,
        colors: ['#6366f1', '#10b981', '#f59e0b', '#06b6d4', '#8b5cf6', '#ec4899', '#3b82f6'],
        legend: {
          show: true,
          position: 'bottom',
          fontSize: '11px',
          fontWeight: 600,
          labels: { colors: '#64748b' },
          markers: { radius: 12 }
        },
        stroke: {
          show: true,
          colors: ['#090e1d'],
          width: 3
        },
        dataLabels: { enabled: false },
        tooltip: {
          theme: 'dark',
          y: {
            formatter: (val) => '₹' + val.toLocaleString(undefined, {maximumFractionDigits: 0})
          }
        },
        plotOptions: {
          pie: {
            donut: {
              size: '72%',
              labels: {
                show: true,
                name: {
                  show: true,
                  fontSize: '12px',
                  color: '#64748b',
                  fontWeight: 700
                },
                value: {
                  show: true,
                  fontSize: '20px',
                  fontFamily: 'Outfit',
                  color: '#f8fafc',
                  fontWeight: 800,
                  formatter: (val) => '₹' + Number(val).toLocaleString(undefined, {maximumFractionDigits: 0})
                },
                total: {
                  show: true,
                  label: 'Net Portfolio',
                  color: '#64748b',
                  fontWeight: 700,
                  formatter: (w) => '₹' + w.globals.seriesTotals.reduce((a, b) => a + b, 0).toLocaleString(undefined, {maximumFractionDigits: 0})
                }
              }
            }
          }
        }
      };

      if (distributionChart) {
        distributionChart.updateOptions(options);
      } else {
        distributionChart = new ApexCharts(document.querySelector("#donut-chart-container"), options);
        distributionChart.render();
      }
    }

    // Moving index sparklines
    let niftySparkline = null;
    let sensexSparkline = null;

    function renderIndexSparklines() {
      const generateInitialWalk = () => {
        let arr = [];
        let base = 100;
        for (let i = 0; i < 12; i++) {
          base += (Math.random() - 0.48) * 4;
          arr.push(base);
        }
        return arr;
      };

      const niftyData = generateInitialWalk();
      const sensexData = generateInitialWalk();

      const options = (data, color) => ({
        chart: {
          type: 'area',
          height: 40,
          sparkline: { enabled: true },
          animations: { enabled: true, easing: 'smooth', speed: 800 }
        },
        stroke: { curve: 'smooth', width: 2 },
        fill: {
          type: 'gradient',
          gradient: {
            shadeIntensity: 1,
            opacityFrom: 0.35,
            opacityTo: 0.05,
            stops: [0, 90, 100]
          }
        },
        series: [{ data: data }],
        colors: [color]
      });

      niftySparkline = new ApexCharts(document.querySelector("#nifty-sparkline"), options(niftyData, '#10b981'));
      niftySparkline.render();

      sensexSparkline = new ApexCharts(document.querySelector("#sensex-sparkline"), options(sensexData, '#10b981'));
      sensexSparkline.render();

      // Create an organic minor walk every 10 seconds to keep terminal alive
      setInterval(() => {
        if (niftySparkline && sensexSparkline) {
          const niftyValEl = document.getElementById('nifty-val');
          const sensexValEl = document.getElementById('sensex-val');

          let niftyBase = parseFloat(niftyValEl.innerText.replace(/,/g, ''));
          let sensexBase = parseFloat(sensexValEl.innerText.replace(/,/g, ''));

          let niftyWalk = (Math.random() - 0.48) * 8;
          let sensexWalk = (Math.random() - 0.48) * 25;

          let newNifty = niftyBase + niftyWalk;
          let newSensex = sensexBase + sensexWalk;

          niftyValEl.innerText = newNifty.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
          sensexValEl.innerText = newSensex.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});

          niftyData.shift(); niftyData.push(100 + niftyWalk);
          sensexData.shift(); sensexData.push(100 + sensexWalk);

          niftySparkline.updateSeries([{ data: niftyData }]);
          sensexSparkline.updateSeries([{ data: sensexData }]);
        }
      }, 10000);
    }

    // Fetch and Sync Core Data
    async function syncDataFeed() {
      try {
        const response = await fetch('/api/data');
        if (!response.ok) throw new Error('Terminal failed backend sync');
        
        const data = await response.json();
        
        // Sync Time and status indicators
        document.getElementById('system-time').innerText = 'Synced at ' + data.status.prices_updated_at;
        
        const authDot = document.getElementById('auth-dot');
        const authText = document.getElementById('auth-text');
        
        if (data.status.kite_authenticated) {
          authDot.className = 'status-dot active';
          authText.innerText = 'Kite Active';
        } else {
          authDot.className = 'status-dot inactive';
          authText.innerText = 'Kite Expired';
        }

        // Net Worth Card calculations
        const pf = data.portfolio;
        document.getElementById('networth-val').innerText = '₹' + pf.total_current.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
        
        const pnlBadge = document.getElementById('networth-pnl');
        const up = pf.total_pnl >= 0;
        pnlBadge.className = 'hero-pnl-badge ' + (up ? 'up' : 'down');
        pnlBadge.innerHTML = (up ? '▲ Inflow +' : '▼ Outflow ') + '₹' + Math.abs(pf.total_pnl).toLocaleString(undefined, {maximumFractionDigits:0}) + ' (' + Math.abs(pf.total_pnl_pct).toFixed(2) + '%)';
        
        document.getElementById('metric-invested').innerText = '₹' + pf.total_invested.toLocaleString(undefined, {maximumFractionDigits:0});
        document.getElementById('metric-holdings').innerText = data.holdings.length;
        document.getElementById('metric-watchlist').innerText = data.watchlist.length;

        // Populate the dropdown filters for news
        populateNewsSymbols(data.holdings, data.watchlist);

        // Render Tables & Allocation Chart
        renderHoldings(data.holdings);
        renderWatchlistGauges(data.watchlist, data.settings.WATCHLIST_DROP_THRESHOLD);
        renderDonutAllocation(data.holdings);
        
        // Render timeline of scraper logs and active cooldowns combined
        renderTimeline(data.cooldowns, data.sebi_seen_count);

        // Auto feed scrolling ticker tape with tickers
        buildTickerTape(data.holdings, data.watchlist);

        // Load sliders calibration data only once
        if (!window.settingsCalibrated) {
          calibrateSliders(data.settings);
          window.settingsCalibrated = true;
        }

      } catch (err) {
        console.error(err);
        pushToast('Sync disruption: ' + err.message, 'error');
      }
    }

    function buildTickerTape(holdings, watchlist) {
      const tape = document.getElementById('live-ticker-tape');
      let items = `
        <span class="ticker-item"><span class="sym">NIFTY 50</span> 22,456.80 <span class="pct up">▲ +0.56%</span></span>
        <span class="ticker-item"><span class="sym">SENSEX</span> 73,880.20 <span class="pct up">▲ +0.52%</span></span>
      `;
      
      holdings.slice(0, 5).forEach(h => {
        if (h.ltp) {
          const up = h.pnl >= 0;
          items += `
            <span class="ticker-item ${up ? 'up' : 'down'}">
              <span class="sym">${h.symbol}</span> ₹${h.ltp.toLocaleString()} <span>${up ? '▲' : '▼'} ${Math.abs(h.pnl_pct).toFixed(1)}%</span>
            </span>
          `;
        }
      });

      watchlist.slice(0, 5).forEach(w => {
        if (w.ltp) {
          items += `
            <span class="ticker-item">
              <span class="sym">${w.symbol}</span> ₹${w.ltp.toLocaleString()} <span style="color:var(--text-muted)">▼ ${Math.abs(w.drop_pct).toFixed(1)}%</span>
            </span>
          `;
        }
      });

      tape.innerHTML = items;
    }

    function populateNewsSymbols(holdings, watchlist) {
      const select = document.getElementById('news-symbol-filter');
      const currentSelected = select.value;
      
      const allSyms = listTrackedSymbols(holdings, watchlist);
      let options = `<option value="">All Tracked Stocks</option>`;
      
      allSyms.forEach(sym => {
        options += `<option value="${sym}">${sym}</option>`;
      });

      select.innerHTML = options;
      select.value = currentSelected; // preserve selection
    }

    function listTrackedSymbols(holdings, watchlist) {
      const syms = new Set();
      holdings.forEach(h => syms.add(h.symbol));
      watchlist.forEach(w => syms.add(w.symbol));
      return Array.from(syms).sort();
    }

    function renderHoldings(holdings) {
      const tbody = document.getElementById('holdings-tbody');
      if (holdings.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-message">No holdings active. Add a trade log manually below.</td></tr>`;
        return;
      }

      window.holdingsDataList = holdings; // cache for filter searches
      filterHoldingsTable(); // renders list
    }

    function filterHoldingsTable() {
      const query = document.getElementById('search-holdings').value.toUpperCase();
      const tbody = document.getElementById('holdings-tbody');
      const holdings = window.holdingsDataList || [];

      const filtered = holdings.filter(h => h.symbol.includes(query));
      
      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-message">No matching stocks found.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(h => {
        const initials = h.symbol.substring(0, 2);
        const up = h.pnl >= 0;
        const ltpVal = h.ltp ? '₹' + h.ltp.toLocaleString(undefined, {minimumFractionDigits: 2}) : '—';
        const pnlVal = h.ltp ? (up ? '+' : '−') + '₹' + Math.abs(h.pnl).toLocaleString(undefined, {maximumFractionDigits: 0}) : '—';
        const pill = h.ltp ? `<span class="interactive-pill ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${Math.abs(h.pnl_pct).toFixed(1)}%</span>` : '—';

        // Add onclick to td items except the trash action td
        return `
          <tr class="clickable-row">
            <td onclick="openDetailsDrawer('${h.symbol}')">
              <div class="sym-name-cell">
                <span class="circle-avatar">${initials}</span>
                <span class="stock-ticker">${h.symbol}</span>
              </div>
            </td>
            <td class="num" onclick="openDetailsDrawer('${h.symbol}')">${h.quantity.toLocaleString()}</td>
            <td class="num" onclick="openDetailsDrawer('${h.symbol}')">₹${h.average_price.toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
            <td class="num" onclick="openDetailsDrawer('${h.symbol}')">${ltpVal}</td>
            <td class="num" onclick="openDetailsDrawer('${h.symbol}')">${pill}</td>
            <td class="num ${up ? 'text-up' : 'text-down'}" style="font-weight:700;" onclick="openDetailsDrawer('${h.symbol}')">${pnlVal}</td>
            <td>
              <button class="btn-trash" onclick="deleteAsset('${h.symbol}')" title="Wipe Holding">
                <svg style="width:15px;height:15px" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
              </button>
            </td>
          </tr>
        `;
      }).join('');
    }

    function renderWatchlistGauges(watchlist, threshold) {
      const container = document.getElementById('watchlist-gauge-container');
      if (watchlist.length === 0) {
        container.innerHTML = `<div class="empty-message" style="grid-column: span 3;">Watchlist empty. Monitor new ticker models.</div>`;
        return;
      }

      container.innerHTML = watchlist.map(w => {
        const initials = w.symbol.substring(0, 2);
        
        let progress = 0;
        if (w.week_high_52 && w.ltp) {
          progress = (w.drop_pct / threshold) * 100;
        }
        progress = Math.min(Math.max(progress, 0), 100);

        let colorClass = 'normal';
        if (progress > 100 || w.in_buy_zone) {
          colorClass = 'hot-active';
        } else if (progress > 70) {
          colorClass = 'warn';
        }

        const ltpVal = w.ltp ? '₹' + w.ltp.toLocaleString(undefined, {minimumFractionDigits:1}) : '—';
        const highVal = w.week_high_52 ? '₹' + w.week_high_52.toLocaleString(undefined, {minimumFractionDigits:1}) : '—';

        return `
          <div class="watchlist-gauge-card" onclick="openDetailsDrawer('${w.symbol}')">
            <div class="gauge-top">
              <div class="title-area">
                <span class="circle-avatar wl">${initials}</span>
                <span class="stock-ticker">${w.symbol}</span>
              </div>
              <button class="btn-trash" onclick="event.stopPropagation(); deleteWatch('${w.symbol}')" title="Stop Watching">
                <svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
              </button>
            </div>

            <div class="gauge-meta" style="margin-top: 18px;">
              <span>LTP: <strong>${ltpVal}</strong></span>
              <span>52W High: <strong>${highVal}</strong></span>
            </div>

            <div class="progress-bar-container">
              <div class="progress-bar-fill ${colorClass}" style="width: ${progress}%"></div>
            </div>

            <div class="gauge-meta" style="margin-top: 10px;">
              <span>Target Met: <strong>${progress.toFixed(0)}%</strong></span>
              ${w.in_buy_zone ? `
                <span class="gauge-pnl-label buy">🔥 BUY ZONE (▼ ${w.drop_pct.toFixed(1)}%)</span>
              ` : `
                <span class="gauge-pnl-label wait">Watching (▼ ${w.drop_pct.toFixed(1)}%)</span>
              `}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderTimeline(cooldowns, seenCount) {
      const container = document.getElementById('system-timeline-feed');
      const keys = Object.keys(cooldowns);
      
      let items = '';

      if (keys.length > 0) {
        keys.forEach(k => {
          const item = cooldowns[k];
          let displayKey = k.replace('_holdings_drop', ' Cost Basis Drop').replace('_watchlist_drop', ' 52W High drop').replace('_volume_spike', ' Volume Spike Alert');
          displayKey = displayKey.toUpperCase();

          items += `
            <div class="timeline-card">
              <span class="timeline-node alert"></span>
              <div class="timeline-header">
                <span class="timeline-badge warn">Cooldowned suppression</span>
                <span class="timeline-time">${item.remaining_mins}m remaining</span>
              </div>
              <div class="timeline-body">${displayKey} alert suppression is active. Repeat notifications are silenced.</div>
            </div>
          `;
        });
      }

      items += `
        <div class="timeline-card">
          <span class="timeline-node filing"></span>
          <div class="timeline-header">
            <span class="timeline-badge info">System database</span>
            <span class="timeline-time">Permanent Record</span>
          </div>
          <div class="timeline-body">SEBI and Corporate announcements monitored. Total corporate announcements and press releases processed: <strong>${seenCount}</strong> files.</div>
        </div>
      `;

      container.innerHTML = items;
    }

    // Dynamic Publications News Feed Loader
    async function loadNewsFeed(symbolFilter = '') {
      const container = document.getElementById('news-articles-container');
      container.innerHTML = `
        <div class="empty-message" style="grid-column: span 3;">
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
        </div>
      `;

      try {
        const querySymbol = symbolFilter ? '?symbol=' + symbolFilter : '';
        const response = await fetch('/api/news' + querySymbol);
        
        if (!response.ok) {
          const errData = await response.json();
          throw new Error(errData.message || 'News API Error');
        }
        
        const data = await response.json();
        
        if (!data.news || data.news.length === 0) {
          container.innerHTML = `<div class="empty-message" style="grid-column: span 3;">No publications found for the selected stock.</div>`;
          return;
        }

        container.innerHTML = data.news.map(art => {
          const date = new Date(art.datetime * 1000).toLocaleDateString(undefined, {day:'numeric', month:'short', year:'numeric'});
          const imageHtml = art.image ? `<img class="news-image" src="${art.image}" alt="Article Cover" onerror="this.style.display='none'">` : '';
          const symbolTag = art.stock_symbol ? `<span class="news-badge">${art.stock_symbol}</span>` : `<span class="news-badge">Market</span>`;

          return `
            <div class="news-article-card">
              ${imageHtml}
              <div class="news-content">
                <div class="news-meta-row">
                  ${symbolTag}
                  <span>${art.source} · ${date}</span>
                </div>
                <h4 class="news-headline">${art.headline}</h4>
                <p class="news-summary">${art.summary || 'Summary not provided. Open article link for full coverage.'}</p>
                <a class="news-btn" href="${art.url}" target="_blank" rel="noopener noreferrer">
                  Open Publication Website
                  <svg style="width:12px;height:12px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25"/></svg>
                </a>
              </div>
            </div>
          `;
        }).join('');

      } catch (err) {
        container.innerHTML = `<div class="empty-message" style="grid-column: span 3; color: var(--rose);">Could not load news: ${err.message}. Check your internet connection and try again.</div>`;
      }
    }

    // Sliding Drawer Details controller
    let drawerChart = null;

    async function openDetailsDrawer(symbol) {
      document.getElementById('details-drawer').classList.add('active');
      
      // Initial Skeletons/loading layout
      document.getElementById('drawer-symbol').innerText = symbol;
      document.getElementById('drawer-avatar').innerText = symbol.substring(0, 2);
      document.getElementById('drawer-ltp').innerText = '₹0.00';
      document.getElementById('drawer-change').innerText = 'Syncing...';
      document.getElementById('drawer-change').className = 'change-pct';
      document.getElementById('d-stat-invested').innerText = '—';
      document.getElementById('d-stat-qty').innerText = '—';
      document.getElementById('d-stat-avg').innerText = '—';
      document.getElementById('d-stat-pnl').innerText = '—';
      document.getElementById('drawer-news-feed').innerHTML = '<div class="empty-message">Draping publications...</div>';

      // Load Holding Metrics if the stock is in holdings
      const holdings = window.holdingsDataList || [];
      const match = holdings.find(h => h.symbol === symbol);
      
      if (match) {
        document.getElementById('d-stat-invested').innerText = '₹' + match.invested.toLocaleString(undefined, {maximumFractionDigits:0});
        document.getElementById('d-stat-qty').innerText = match.quantity.toLocaleString();
        document.getElementById('d-stat-avg').innerText = '₹' + match.average_price.toLocaleString(undefined, {minimumFractionDigits:2});
        
        const up = match.pnl >= 0;
        document.getElementById('d-stat-pnl').innerText = (up ? '+' : '') + '₹' + match.pnl.toLocaleString(undefined, {maximumFractionDigits:0}) + ' (' + match.pnl_pct.toFixed(1) + '%)';
        document.getElementById('d-stat-pnl').style.color = up ? 'var(--emerald)' : 'var(--rose)';
      } else {
        document.getElementById('d-stat-invested').innerText = 'Not Held';
        document.getElementById('d-stat-qty').innerText = '0';
        document.getElementById('d-stat-avg').innerText = '—';
        document.getElementById('d-stat-pnl').innerText = '—';
        document.getElementById('d-stat-pnl').style.color = 'var(--text-muted)';
      }

      // Fetch dynamic price statistics & Intraday Chart points
      try {
        const chartResponse = await fetch('/api/chart?symbol=' + symbol);
        const chartData = await chartResponse.json();

        // Fetch prices from quotes
        const points = chartData.points || [];
        if (points.length > 0) {
          const startPrice = points[0].price;
          const endPrice = points[points.length - 1].price;
          const diff = endPrice - startPrice;
          const diffPct = (diff / startPrice) * 100;
          const positive = diff >= 0;

          document.getElementById('drawer-ltp').innerText = '₹' + endPrice.toLocaleString(undefined, {minimumFractionDigits:2});
          document.getElementById('drawer-change').innerText = (positive ? '▲ +' : '▼ ') + Math.abs(diffPct).toFixed(2) + '%';
          document.getElementById('drawer-change').className = 'change-pct ' + (positive ? 'up' : 'down');
          
          // Style change values
          if (positive) {
            document.getElementById('drawer-change').style.color = 'var(--emerald)';
            document.getElementById('drawer-change').style.background = 'var(--emerald-glow)';
          } else {
            document.getElementById('drawer-change').style.color = 'var(--rose)';
            document.getElementById('drawer-change').style.background = 'var(--rose-glow)';
          }

          // Plot Area Chart
          plotIntradayChart(points, positive ? '#10b981' : '#f43f5e');
        } else {
          document.getElementById('drawer-ltp').innerText = '₹0.00';
          document.getElementById('drawer-change').innerText = 'Intraday Closed';
          document.getElementById('drawer-change').className = 'change-pct';
        }

      } catch (err) {
        console.error('Drawer chart load failure', err);
      }

      // Fetch stock-specific publications
      try {
        const newsResponse = await fetch('/api/news?symbol=' + symbol);
        const newsData = await newsResponse.json();
        const feed = document.getElementById('drawer-news-feed');
        
        if (!newsData.news || newsData.news.length === 0) {
          feed.innerHTML = '<div class="empty-message">No specific publications cached.</div>';
          return;
        }

        feed.innerHTML = newsData.news.slice(0, 5).map(art => {
          const date = new Date(art.datetime * 1000).toLocaleDateString(undefined, {day:'numeric', month:'short'});
          return `
            <div class="feed-item" style="background:rgba(255,255,255,0.01)">
              <div class="feed-item-header">
                <span class="feed-item-title" style="color:var(--primary)">${art.source}</span>
                <span class="feed-item-time">${date}</span>
              </div>
              <a href="${art.url}" target="_blank" rel="noopener noreferrer" style="color:var(--text); text-decoration:none; font-weight:600; font-size:12.5px; display:block; margin:2px 0 4px; overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;">
                ${art.headline}
              </a>
            </div>
          `;
        }).join('');

      } catch (err) {
        document.getElementById('drawer-news-feed').innerHTML = '<div class="empty-message" style="color:var(--rose)">Publications synchronization failed.</div>';
      }
    }

    function closeDetailsDrawer(event = null) {
      if (event) {
        event.stopPropagation();
      }
      document.getElementById('details-drawer').classList.remove('active');
    }

    function plotIntradayChart(points, colorHex) {
      const times = points.map(p => p.time);
      const prices = points.map(p => p.price);

      const options = {
        chart: {
          type: 'area',
          height: 230,
          background: 'transparent',
          foreColor: '#64748b',
          fontFamily: 'Plus Jakarta Sans',
          toolbar: { show: false },
          sparkline: { enabled: false }
        },
        stroke: { curve: 'smooth', width: 2 },
        colors: [colorHex],
        series: [{
          name: 'LTP',
          data: prices
        }],
        xaxis: {
          categories: times,
          labels: {
            show: true,
            style: { colors: '#64748b' }
          },
          axisBorder: { show: false },
          axisTicks: { show: false }
        },
        yaxis: {
          show: true,
          labels: {
            style: { colors: '#64748b' },
            formatter: (v) => '₹' + v.toFixed(0)
          }
        },
        grid: {
          borderColor: 'rgba(255,255,255,0.03)',
          strokeDashArray: 4
        },
        fill: {
          type: 'gradient',
          gradient: {
            shadeIntensity: 1,
            opacityFrom: 0.25,
            opacityTo: 0.02,
            stops: [0, 95, 100]
          }
        },
        tooltip: {
          theme: 'dark',
          y: { formatter: (v) => '₹' + v.toLocaleString() }
        }
      };

      if (drawerChart) {
        drawerChart.destroy();
      }
      drawerChart = new ApexCharts(document.querySelector("#intraday-chart-div"), options);
      drawerChart.render();
    }

    function calibrateSliders(settings) {
      document.getElementById('input-holdings').value = settings.HOLDINGS_DROP_THRESHOLD;
      updateIndicator('holdings', settings.HOLDINGS_DROP_THRESHOLD + '%');

      document.getElementById('input-watchlist').value = settings.WATCHLIST_DROP_THRESHOLD;
      updateIndicator('watchlist', settings.WATCHLIST_DROP_THRESHOLD + '%');

      document.getElementById('input-vol').value = settings.VOLUME_SPIKE_MULTIPLIER;
      updateIndicator('vol', settings.VOLUME_SPIKE_MULTIPLIER + 'x');

      document.getElementById('input-fiidii').value = settings.FII_DII_THRESHOLD_CRORES;
      updateIndicator('fiidii', '₹' + Number(settings.FII_DII_THRESHOLD_CRORES).toLocaleString() + ' Cr');

      document.getElementById('input-cooldown').value = settings.ALERT_COOLDOWN_HOURS;
      updateIndicator('cooldown', settings.ALERT_COOLDOWN_HOURS + ' Hours');
    }

    // API mutation request triggers
    async function submitTradeLog() {
      const symbol = document.getElementById('f-hold-symbol').value.trim();
      const qty = document.getElementById('f-hold-qty').value;
      const price = document.getElementById('f-hold-price').value;

      if (!symbol || !qty || !price) {
        pushToast('Complete all fields', 'error');
        return;
      }

      try {
        const response = await fetch('/api/holdings/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: symbol, quantity: qty, average_price: price })
        });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          closeHoldingModal();
          
          document.getElementById('f-hold-symbol').value = '';
          document.getElementById('f-hold-qty').value = '';
          document.getElementById('f-hold-price').value = '';
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Investment trade logging error', 'error');
      }
    }

    async function deleteAsset(symbol) {
      if (!confirm('Wipe holding for ' + symbol + '?')) return;
      try {
        const response = await fetch('/api/holdings/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: symbol })
        });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Disruption deleting holding asset', 'error');
      }
    }

    async function submitWatchlistLog() {
      const symbol = document.getElementById('f-wl-symbol').value.trim();
      if (!symbol) {
        pushToast('Symbol required', 'error');
        return;
      }

      try {
        const response = await fetch('/api/watchlist/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: symbol })
        });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          closeWatchlistModal();
          document.getElementById('f-wl-symbol').value = '';
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Error logging stock watch model', 'error');
      }
    }

    async function deleteWatch(symbol) {
      try {
        const response = await fetch('/api/watchlist/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: symbol })
        });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Failed deleting watched ticker symbol', 'error');
      }
    }

    async function saveCalibrationSettings() {
      const holdings = document.getElementById('input-holdings').value;
      const watchlist = document.getElementById('input-watchlist').value;
      const vol = document.getElementById('input-vol').value;
      const fiidii = document.getElementById('input-fiidii').value;
      const cooldown = document.getElementById('input-cooldown').value;

      try {
        const response = await fetch('/api/settings/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            HOLDINGS_DROP_THRESHOLD: holdings,
            WATCHLIST_DROP_THRESHOLD: watchlist,
            VOLUME_SPIKE_MULTIPLIER: vol,
            FII_DII_THRESHOLD_CRORES: fiidii,
            ALERT_COOLDOWN_HOURS: cooldown
          })
        });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Error deploying calibration configurations', 'error');
      }
    }

    async function triggerScan() {
      const btn = document.getElementById('btn-scan');
      btn.disabled = true;
      btn.innerText = 'Scanning Tickers...';
      
      try {
        const response = await fetch('/api/trigger-check', { method: 'POST' });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          syncDataFeed();
        } else {
          pushToast(res.message, 'error');
        }
      } catch (err) {
        pushToast('Scan checking failed: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/></svg> Run Scanning Scan`;
      }
    }

    async function renewKiteSession() {
      const btn = document.getElementById('btn-oauth');
      btn.disabled = true;
      btn.innerText = 'Connecting...';
      
      try {
        const response = await fetch('/api/kite/renew-token', { method: 'POST' });
        const res = await response.json();
        if (response.ok) {
          pushToast(res.message, 'success');
          syncDataFeed();
        } else {
          pushToast('Redirecting to Zerodha web authentication portal...', 'info');
          setTimeout(() => {
            window.location.href = '/kite/renew';
          }, 1200);
        }
      } catch (err) {
        pushToast('Authentication failed. Launching OAuth portal...', 'info');
        setTimeout(() => {
          window.location.href = '/kite/renew';
        }, 1200);
      } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg style="width:14px;height:14px" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.02 5.912L9 18.75V21h-2.25v-2.25H4.5V16.5h-2.25v-2.25l6.838-6.837A6 6 0 0115.75 5.25z"/></svg> OAuth Session`;
      }
    }

    // Startup Init
    renderIndexSparklines();
    syncDataFeed();
    setInterval(syncDataFeed, 15000);
  </script>

</body>
</html>"""
    
    return html, 200, {"Content-Type": "text/html"}


def run_server() -> None:
    """Start the Flask server. Called from main.py in a background thread."""
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Postback server starting on port {port}...")
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
