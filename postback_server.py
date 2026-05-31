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

    # If cache is empty (e.g. weekend or fresh boot), fetch live from Kite
    if not quotes and all_symbols:
        import kite_client
        quotes = kite_client.get_quotes(all_symbols)
        # Kite quote lacks 52-week high — fill it from historical data
        for sym, q in quotes.items():
            high52, _ = kite_client.get_daily_stats(q.get("instrument_token"))
            q["week_high_52"] = high52
        updated_at = ""  # shown as live fetch in subtitle

    def initials(s):
        return s[:2].upper()

    # Build holdings cards
    holdings_rows = ""
    total_invested = 0
    total_current  = 0
    for h in holdings:
        sym      = h["symbol"]
        qty      = h["quantity"]
        avg      = h["average_price"]
        cur      = quotes.get(sym, {}).get("last_price", 0)
        invested = qty * avg
        current  = qty * cur if cur else 0
        pnl      = current - invested
        pnl_pct  = ((cur - avg) / avg * 100) if avg and cur else 0
        up       = pnl >= 0
        cls      = "up" if up else "down"
        arrow    = "▲" if up else "▼"
        total_invested += invested
        total_current  += current if cur else invested
        cur_str  = f"₹{cur:,.2f}" if cur else "—"
        holdings_rows += f"""
        <tr>
          <td><div class="sym"><span class="avatar">{initials(sym)}</span><span class="sym-name">{sym}</span></div></td>
          <td class="num">{qty:.0f}</td>
          <td class="num">₹{avg:,.2f}</td>
          <td class="num">{cur_str}</td>
          <td class="num"><span class="pill {cls}">{arrow} {abs(pnl_pct):.1f}%</span></td>
          <td class="num {cls}">{'+' if up else '−'}₹{abs(pnl):,.0f}</td>
        </tr>"""

    total_pnl     = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0
    total_up      = total_pnl >= 0
    total_cls     = "up" if total_up else "down"

    # Build watchlist cards
    watchlist_rows = ""
    near_count = 0
    for sym in watchlist:
        q        = quotes.get(sym, {})
        cur      = q.get("last_price", 0)
        high_52w = q.get("week_high_52", 0)
        drop_pct = ((high_52w - cur) / high_52w * 100) if high_52w and cur else 0
        hot      = drop_pct >= 20
        if hot:
            near_count += 1
        cls      = "down" if hot else "muted"
        cur_str  = f"₹{cur:,.2f}" if cur else "—"
        h52_str  = f"₹{high_52w:,.2f}" if high_52w else "—"
        flag     = '<span class="hot">BUY ZONE</span>' if hot else ''
        watchlist_rows += f"""
        <tr>
          <td><div class="sym"><span class="avatar wl">{initials(sym)}</span><span class="sym-name">{sym}</span>{flag}</div></td>
          <td class="num">{h52_str}</td>
          <td class="num">{cur_str}</td>
          <td class="num"><span class="pill {cls}">▼ {drop_pct:.1f}%</span></td>
        </tr>"""

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_str   = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")
    if updated_at:
        try:
            dt        = datetime.fromisoformat(updated_at).astimezone(ZoneInfo("Asia/Kolkata"))
            price_str = f"Prices as of {dt.strftime('%I:%M %p IST')}"
        except Exception:
            price_str = "Live from Kite"
    else:
        price_str = "Live from Kite"

    pnl_sign = "+" if total_up else "−"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portfolio · Stock Alerts</title>
  <meta http-equiv="refresh" content="300">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    :root{{
      --bg:#0a0e1a; --panel:rgba(255,255,255,.04); --border:rgba(255,255,255,.08);
      --text:#e8ecf5; --muted:#7c89a5; --up:#22e08a; --down:#ff5c7c; --accent:#6366f1;
    }}
    html{{-webkit-text-size-adjust:100%}}
    body{{
      background:var(--bg); color:var(--text);
      font-family:'Inter',-apple-system,sans-serif; min-height:100vh;
      padding:28px 20px 60px; position:relative; overflow-x:hidden;
    }}
    body::before{{
      content:""; position:fixed; top:-40%; left:-10%; width:60%; height:80%;
      background:radial-gradient(circle, rgba(99,102,241,.18), transparent 70%);
      filter:blur(40px); z-index:-1;
    }}
    body::after{{
      content:""; position:fixed; bottom:-40%; right:-10%; width:60%; height:80%;
      background:radial-gradient(circle, rgba(34,224,138,.10), transparent 70%);
      filter:blur(40px); z-index:-1;
    }}
    .wrap{{max-width:1080px;margin:0 auto}}
    .top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}}
    .brand{{display:flex;align-items:center;gap:12px}}
    .logo{{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--accent),#22e08a);display:flex;align-items:center;justify-content:center;font-size:20px}}
    .brand h1{{font-size:19px;font-weight:700;letter-spacing:-.02em}}
    .brand p{{font-size:12px;color:var(--muted);font-weight:500}}
    .live{{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);background:var(--panel);border:1px solid var(--border);padding:7px 13px;border-radius:99px}}
    .dot{{width:7px;height:7px;border-radius:50%;background:var(--up);box-shadow:0 0 0 0 rgba(34,224,138,.6);animation:pulse 2s infinite}}
    @keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(34,224,138,.5)}}70%{{box-shadow:0 0 0 8px rgba(34,224,138,0)}}100%{{box-shadow:0 0 0 0 rgba(34,224,138,0)}}}}

    .hero{{
      background:linear-gradient(135deg, rgba(99,102,241,.12), rgba(34,224,138,.04));
      border:1px solid var(--border); border-radius:22px; padding:28px 30px; margin-bottom:18px;
      backdrop-filter:blur(20px);
    }}
    .hero .lbl{{font-size:13px;color:var(--muted);font-weight:500;margin-bottom:8px}}
    .hero .big{{font-size:42px;font-weight:800;letter-spacing:-.03em;line-height:1}}
    .hero .chg{{display:inline-flex;align-items:center;gap:6px;margin-top:12px;font-size:15px;font-weight:600;padding:5px 12px;border-radius:99px}}
    .chg.up{{background:rgba(34,224,138,.14);color:var(--up)}}
    .chg.down{{background:rgba(255,92,124,.14);color:var(--down)}}

    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:32px}}
    .stat{{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px 20px;backdrop-filter:blur(20px)}}
    .stat .lbl{{font-size:12px;color:var(--muted);font-weight:500;margin-bottom:7px}}
    .stat .val{{font-size:21px;font-weight:700;letter-spacing:-.02em}}

    .sec{{display:flex;align-items:center;gap:10px;margin:0 4px 14px}}
    .sec h2{{font-size:14px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}}
    .sec .count{{font-size:12px;font-weight:600;color:var(--accent);background:rgba(99,102,241,.14);padding:2px 9px;border-radius:99px}}

    .panel{{background:var(--panel);border:1px solid var(--border);border-radius:18px;overflow:hidden;margin-bottom:32px;backdrop-filter:blur(20px)}}
    table{{width:100%;border-collapse:collapse}}
    th{{text-align:right;padding:14px 18px;font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}}
    th:first-child{{text-align:left}}
    td{{padding:15px 18px;font-size:14px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}}
    tr:last-child td{{border-bottom:none}}
    tbody tr{{transition:background .15s}}
    tbody tr:hover{{background:rgba(255,255,255,.03)}}
    .num{{text-align:right;font-variant-numeric:tabular-nums;font-weight:500}}
    .sym{{display:flex;align-items:center;gap:11px}}
    .avatar{{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff;flex-shrink:0}}
    .avatar.wl{{background:linear-gradient(135deg,#0ea5e9,#22d3ee)}}
    .sym-name{{font-weight:600;letter-spacing:-.01em}}
    .pill{{display:inline-block;padding:4px 10px;border-radius:8px;font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}}
    .pill.up{{background:rgba(34,224,138,.14);color:var(--up)}}
    .pill.down{{background:rgba(255,92,124,.14);color:var(--down)}}
    .pill.muted{{background:rgba(124,137,165,.12);color:var(--muted)}}
    .up{{color:var(--up)}} .down{{color:var(--down)}}
    .hot{{margin-left:8px;font-size:10px;font-weight:700;letter-spacing:.05em;background:rgba(34,224,138,.16);color:var(--up);padding:3px 7px;border-radius:6px}}
    .foot{{text-align:center;color:var(--muted);font-size:12px;margin-top:8px}}

    @media(max-width:640px){{
      body{{padding:20px 14px 40px}}
      .hero .big{{font-size:34px}}
      .stats{{grid-template-columns:1fr;gap:10px}}
      th:nth-child(2),td:nth-child(2){{display:none}}
      .avatar{{width:30px;height:30px}}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="brand">
        <div class="logo">📈</div>
        <div>
          <h1>Portfolio</h1>
          <p>{price_str}</p>
        </div>
      </div>
      <div class="live"><span class="dot"></span> Live · {now_str}</div>
    </div>

    <div class="hero">
      <div class="lbl">Current Value</div>
      <div class="big">₹{total_current:,.0f}</div>
      <div class="chg {total_cls}">{'▲' if total_up else '▼'} {pnl_sign}₹{abs(total_pnl):,.0f} ({abs(total_pnl_pct):.2f}%)</div>
    </div>

    <div class="stats">
      <div class="stat"><div class="lbl">Invested</div><div class="val">₹{total_invested:,.0f}</div></div>
      <div class="stat"><div class="lbl">Holdings</div><div class="val">{len(holdings)}</div></div>
      <div class="stat"><div class="lbl">Watchlist</div><div class="val">{len(watchlist)}</div></div>
    </div>

    <div class="sec"><h2>Holdings</h2><span class="count">{len(holdings)}</span></div>
    <div class="panel">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>Return</th><th>P&amp;L</th>
        </tr></thead>
        <tbody>{holdings_rows}</tbody>
      </table>
    </div>

    <div class="sec"><h2>Watchlist</h2>{f'<span class="count">{near_count} in buy zone</span>' if near_count else ''}</div>
    <div class="panel">
      <table>
        <thead><tr>
          <th>Symbol</th><th>52W High</th><th>LTP</th><th>From High</th>
        </tr></thead>
        <tbody>{watchlist_rows}</tbody>
      </table>
    </div>

    <p class="foot">Auto-refreshes every 5 minutes · Alerts via ntfy.sh</p>
  </div>
</body>
</html>"""

    return html, 200, {"Content-Type": "text/html"}


def run_server() -> None:
    """Start the Flask server. Called from main.py in a background thread."""
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Postback server starting on port {port}...")
    # use_reloader=False is required when running inside a thread
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
