"""
market_data.py — Thin wrapper that exposes market data via kite_client.
"""
import kite_client


def get_quotes(symbols: list[str]) -> dict:
    return kite_client.get_quotes(symbols)


def get_30day_avg_volume(instrument_token: int) -> float | None:
    return kite_client.get_30day_avg_volume(instrument_token)
