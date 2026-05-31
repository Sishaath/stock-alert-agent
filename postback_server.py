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


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — Railway uses this to verify the service is running."""
    return jsonify({"status": "running"}), 200


@app.route("/holdings", methods=["GET"])
def view_holdings():
    """
    Quick view of current holdings — open in browser to check.
    https://<your-railway-app>.up.railway.app/holdings
    """
    from holdings_manager import load_holdings
    return jsonify(load_holdings()), 200


def run_server() -> None:
    """Start the Flask server. Called from main.py in a background thread."""
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Postback server starting on port {port}...")
    # use_reloader=False is required when running inside a thread
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
