"""
postback_server.py — Flask server that receives trade notifications from Zerodha.

How it works:
    1. You place a trade on Zerodha Kite app (buy or sell)
    2. Zerodha automatically POSTs the order details to this server
    3. This server validates the request and updates holdings.json
    4. The alert agent picks up the new holdings on its next check cycle

No tokens. No daily renewal. Zerodha pushes to us — we never call them.

Setup:
    In your Kite Connect app settings, set Postback URL to:
    https://<your-railway-app>.up.railway.app/postback
"""

import hashlib
import json
import logging
import os

from flask import Flask, request, jsonify

from holdings_manager import process_trade
from notifier import send_alert
from config import KITE_API_SECRET

logger = logging.getLogger(__name__)
app    = Flask(__name__)


def _validate_checksum(order_id: str, order_timestamp: str, received: str) -> bool:
    """
    Verify that the postback is genuinely from Zerodha.

    Zerodha computes: SHA256(order_id + order_timestamp + api_secret)
    We recompute it and compare. If it doesn't match, we reject the request.
    """
    if not KITE_API_SECRET:
        logger.warning("KITE_API_SECRET not set — skipping checksum validation.")
        return True  # Allow in dev; set the secret in production

    expected = hashlib.sha256(
        f"{order_id}{order_timestamp}{KITE_API_SECRET}".encode()
    ).hexdigest()

    return expected == received


@app.route("/postback", methods=["POST"])
def postback():
    """
    Receives order update postbacks from Zerodha.

    Zerodha sends a POST here every time an order status changes
    (placed, open, complete, rejected, cancelled).
    We only act on COMPLETE orders with product CNC (delivery trades).
    """
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
    exchange        = data.get("exchange", "NSE")

    try:
        quantity      = float(data.get("filled_quantity", 0))
        average_price = float(data.get("average_price", 0))
    except (ValueError, TypeError):
        quantity, average_price = 0, 0

    logger.info(
        f"Postback received: {status} | {transaction} {quantity} {symbol} "
        f"@ ₹{average_price} | product={product}"
    )

    # ── Validate checksum ─────────────────────────────────────────────────────
    if not _validate_checksum(order_id, order_timestamp, checksum):
        logger.warning(f"Checksum mismatch for order {order_id} — rejecting.")
        return jsonify({"status": "error", "message": "invalid checksum"}), 403

    # ── Only process COMPLETE delivery (CNC) orders ───────────────────────────
    # CNC = Cash and Carry = delivery trade (long-term holding)
    # MIS = intraday — we don't track these as holdings
    if status != "COMPLETE":
        logger.debug(f"Order {order_id} status is {status} — ignoring.")
        return jsonify({"status": "ok", "message": "ignored non-complete order"}), 200

    if product != "CNC":
        logger.debug(f"Order {order_id} product is {product} — ignoring (not CNC).")
        return jsonify({"status": "ok", "message": "ignored non-CNC order"}), 200

    if not symbol or quantity <= 0 or average_price <= 0:
        logger.warning(f"Invalid order data: {data}")
        return jsonify({"status": "error", "message": "invalid order data"}), 400

    # ── Update holdings ───────────────────────────────────────────────────────
    process_trade(
        symbol           = symbol,
        transaction_type = transaction,
        quantity         = quantity,
        average_price    = average_price,
    )

    # ── Send confirmation notification ────────────────────────────────────────
    action  = "Bought" if transaction == "BUY" else "Sold"
    message = (
        f"Trade recorded: {action} {quantity:.0f} {symbol} @ ₹{average_price:.2f}\n"
        f"Holdings updated automatically."
    )
    send_alert(message, priority="low")

    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def index():
    from flask import redirect
    return redirect("/holdings")


# ─── Kite OAuth Token Renewal ─────────────────────────────────────────────────

@app.route("/kite/renew", methods=["GET"])
def kite_renew():
    """
    Open this URL on your phone to renew the Kite access token.
    Redirects you to Zerodha login — after login you come back here automatically.
    """
    from flask import redirect as flask_redirect
    from kite_client import kite
    return flask_redirect(kite.login_url())


@app.route("/kite/callback", methods=["GET"])
def kite_callback():
    """
    Zerodha redirects here after login with ?request_token=XXX in the URL.
    We exchange it for an access token and save it — no manual steps needed.
    """
    from kite_client import kite, save_token

    request_token = request.args.get("request_token", "")
    status        = request.args.get("status", "")

    if status != "success" or not request_token:
        return (
            "<html><body style='background:#0f172a;color:#f87171;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2>Login failed or cancelled.</h2>"
            "<a href='/kite/renew' style='color:#60a5fa'>Try again →</a>"
            "</div></body></html>"
        ), 400

    try:
        session      = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = session["access_token"]
        save_token(access_token)
        logger.info("Kite access token renewed via OAuth callback.")
        return (
            "<html><body style='background:#0f172a;color:#e2e8f0;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='color:#4ade80;font-size:48px'>✓</h1>"
            "<h2>Token Renewed</h2>"
            "<p style='color:#94a3b8'>You're authenticated for today's market session.</p>"
            "<br><a href='/holdings' style='color:#60a5fa;font-size:18px'>"
            "Go to Dashboard →</a>"
            "</div></body></html>"
        )
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return (
            f"<html><body style='background:#0f172a;color:#f87171;"
            f"font-family:sans-serif;display:flex;align-items:center;"
            f"justify-content:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><h2>Error: {e}</h2>"
            f"<a href='/kite/renew' style='color:#60a5fa'>Try again →</a>"
            f"</div></body></html>"
        ), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — Railway uses this to verify the service is running."""
    return jsonify({"status": "running"}), 200


@app.route("/holdings", methods=["GET"])
def view_holdings():
    """Live dashboard showing holdings and watchlist with current prices."""
    from holdings_manager import load_holdings
    from watchlist_manager import load_watchlist
    from config import DATA_DIR

    holdings  = load_holdings()
    watchlist = load_watchlist()

    holding_symbols = [h["symbol"] for h in holdings]
    all_symbols     = list(set(holding_symbols + watchlist))

    # Try cache first
    cache_file = os.path.join(DATA_DIR, "prices_cache.json")
    quotes     = {}
    updated_at = ""
    try:
        with open(cache_file, "r") as f:
            cache      = json.load(f)
            quotes     = cache.get("quotes", {})
            updated_at = cache.get("updated_at", "")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # If cache is empty, fetch directly from Finnhub
    if not quotes and all_symbols:
        import market_data
        quotes     = market_data.get_quotes(all_symbols)
        updated_at = ""  # will show as live fetch in subtitle

    # Build holdings rows
    holdings_rows = ""
    total_invested = 0
    total_current  = 0
    for h in holdings:
        sym       = h["symbol"]
        qty       = h["quantity"]
        avg       = h["average_price"]
        cur       = quotes.get(sym, {}).get("last_price", 0)
        invested  = qty * avg
        current   = qty * cur if cur else 0
        pnl       = current - invested
        pnl_pct   = ((cur - avg) / avg * 100) if avg and cur else 0
        color     = "#4ade80" if pnl >= 0 else "#f87171"
        arrow     = "▲" if pnl >= 0 else "▼"
        total_invested += invested
        total_current  += current if cur else invested
        cur_str   = f"₹{cur:,.2f}" if cur else "—"
        holdings_rows += f"""
        <tr>
            <td><b>{sym}</b></td>
            <td>{qty:.0f}</td>
            <td>₹{avg:,.2f}</td>
            <td>{cur_str}</td>
            <td style="color:{color}">{arrow} {pnl_pct:.1f}%</td>
            <td style="color:{color}">₹{pnl:,.0f}</td>
        </tr>"""

    total_pnl     = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0
    total_color   = "#4ade80" if total_pnl >= 0 else "#f87171"

    # Build watchlist rows
    watchlist_rows = ""
    for sym in watchlist:
        q        = quotes.get(sym, {})
        cur      = q.get("last_price", 0)
        high_52w = q.get("week_high_52", 0)
        drop_pct = ((high_52w - cur) / high_52w * 100) if high_52w and cur else 0
        color    = "#f87171" if drop_pct >= 20 else "#94a3b8"
        cur_str  = f"₹{cur:,.2f}" if cur else "—"
        h52_str  = f"₹{high_52w:,.2f}" if high_52w else "—"
        watchlist_rows += f"""
        <tr>
            <td><b>{sym}</b></td>
            <td>{h52_str}</td>
            <td>{cur_str}</td>
            <td style="color:{color}">▼ {drop_pct:.1f}%</td>
        </tr>"""

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_str   = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    price_str = ""
    if updated_at:
        try:
            dt        = datetime.fromisoformat(updated_at).astimezone(ZoneInfo("Asia/Kolkata"))
            price_str = f"&nbsp;·&nbsp; Prices as of {dt.strftime('%I:%M %p IST')}"
        except Exception:
            pass
    if not price_str:
        price_str = "&nbsp;·&nbsp; Live fetch from Finnhub"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Alert Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0f172a; color: #e2e8f0; font-family: -apple-system, sans-serif; padding: 24px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
    .sub {{ color: #64748b; font-size: 13px; margin-bottom: 28px; }}
    .cards {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
    .card {{ background: #1e293b; border-radius: 12px; padding: 18px 24px; flex: 1; min-width: 160px; }}
    .card .label {{ font-size: 12px; color: #64748b; margin-bottom: 6px; }}
    .card .value {{ font-size: 22px; font-weight: 700; }}
    h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px; overflow: hidden; margin-bottom: 28px; }}
    th {{ background: #0f172a; padding: 12px 16px; text-align: left; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }}
    td {{ padding: 13px 16px; font-size: 14px; border-top: 1px solid #0f172a; }}
    tr:hover td {{ background: #263348; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; background: #4ade8022; color: #4ade80; }}
  </style>
</head>
<body>
  <h1>Stock Alert Dashboard</h1>
  <p class="sub">Loaded: {now_str}{price_str}</p>

  <div class="cards">
    <div class="card">
      <div class="label">Invested</div>
      <div class="value">₹{total_invested:,.0f}</div>
    </div>
    <div class="card">
      <div class="label">Current Value</div>
      <div class="value">₹{total_current:,.0f}</div>
    </div>
    <div class="card">
      <div class="label">Total P&amp;L</div>
      <div class="value" style="color:{total_color}">₹{total_pnl:,.0f} ({total_pnl_pct:.1f}%)</div>
    </div>
    <div class="card">
      <div class="label">Holdings</div>
      <div class="value">{len(holdings)}</div>
    </div>
  </div>

  <h2>Holdings</h2>
  <table>
    <thead><tr>
      <th>Symbol</th><th>Qty</th><th>Avg Price</th><th>Current</th><th>Return</th><th>P&amp;L</th>
    </tr></thead>
    <tbody>{holdings_rows}</tbody>
  </table>

  <h2>Watchlist</h2>
  <table>
    <thead><tr>
      <th>Symbol</th><th>52W High</th><th>Current</th><th>Drop from High</th>
    </tr></thead>
    <tbody>{watchlist_rows}</tbody>
  </table>
</body>
</html>"""

    return html, 200, {"Content-Type": "text/html"}


def run_server() -> None:
    """Start the Flask server. Called from main.py in a background thread."""
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Postback server starting on port {port}...")
    # use_reloader=False is required when running inside a thread
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
