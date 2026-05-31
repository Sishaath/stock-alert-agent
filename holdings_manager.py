"""
holdings_manager.py — Reads and updates holdings.json.

holdings.json is auto-updated by the postback server every time you
complete a trade on Zerodha. You never need to edit it manually.

Format:
    [
      {"symbol": "HDFCBANK", "quantity": 10, "average_price": 1650.00},
      {"symbol": "INFY",     "quantity": 5,  "average_price": 1420.00}
    ]
"""

import json
import logging
import threading
from config import HOLDINGS_FILE

logger = logging.getLogger(__name__)

# Thread lock — postback server and scheduler both read/write holdings.json
_lock = threading.Lock()


def load_holdings() -> list[dict]:
    """Load current holdings from holdings.json."""
    with _lock:
        try:
            with open(HOLDINGS_FILE, "r") as f:
                holdings = json.load(f)

            validated = []
            for h in holdings:
                symbol    = str(h.get("symbol", "")).upper().strip()
                qty       = float(h.get("quantity", 0))
                avg_price = float(h.get("average_price", 0))
                if symbol and qty > 0 and avg_price > 0:
                    validated.append({
                        "symbol":        symbol,
                        "quantity":      qty,
                        "average_price": round(avg_price, 2),
                    })
            return validated

        except FileNotFoundError:
            logger.warning(f"{HOLDINGS_FILE} not found — starting with empty holdings.")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {HOLDINGS_FILE}: {e}")
            return []


def _save_holdings(holdings: list[dict]) -> None:
    """Write holdings to disk. Must be called inside _lock."""
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(holdings, f, indent=2)


def process_trade(
    symbol: str,
    transaction_type: str,
    quantity: float,
    average_price: float,
) -> None:
    """
    Update holdings.json based on a completed trade from Zerodha postback.

    BUY  → adds stock or recalculates weighted average price
    SELL → reduces quantity or removes stock if fully sold

    Args:
        symbol:           NSE ticker, e.g. "RELIANCE"
        transaction_type: "BUY" or "SELL"
        quantity:         Number of shares traded
        average_price:    Execution price per share
    """
    symbol = symbol.upper().strip()

    with _lock:
        try:
            with open(HOLDINGS_FILE, "r") as f:
                holdings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            holdings = []

        if transaction_type == "BUY":
            _process_buy(holdings, symbol, quantity, average_price)
        elif transaction_type == "SELL":
            _process_sell(holdings, symbol, quantity)
        else:
            logger.warning(f"Unknown transaction type: {transaction_type}")
            return

        _save_holdings(holdings)
        logger.info(
            f"Holdings updated: {transaction_type} {quantity} {symbol} "
            f"@ ₹{average_price:.2f}"
        )


def _process_buy(
    holdings: list[dict],
    symbol: str,
    qty: float,
    price: float,
) -> None:
    """Add or update a holding after a BUY order."""
    for h in holdings:
        if h["symbol"] == symbol:
            # Already holding this stock — recalculate weighted average price
            old_qty   = h["quantity"]
            old_avg   = h["average_price"]
            new_qty   = old_qty + qty
            new_avg   = (old_qty * old_avg + qty * price) / new_qty
            h["quantity"]      = round(new_qty, 4)
            h["average_price"] = round(new_avg, 2)
            return

    # New stock not previously held
    holdings.append({
        "symbol":        symbol,
        "quantity":      round(qty, 4),
        "average_price": round(price, 2),
    })


def _process_sell(holdings: list[dict], symbol: str, qty: float) -> None:
    """Reduce or remove a holding after a SELL order."""
    for i, h in enumerate(holdings):
        if h["symbol"] == symbol:
            new_qty = h["quantity"] - qty
            if new_qty <= 0.001:
                holdings.pop(i)   # Fully sold — remove
                logger.info(f"Fully sold {symbol} — removed from holdings.")
            else:
                h["quantity"] = round(new_qty, 4)  # Partially sold
            return

    logger.warning(f"SELL received for {symbol} but it's not in holdings — ignoring.")
