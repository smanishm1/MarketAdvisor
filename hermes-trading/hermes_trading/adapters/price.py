"""Price adapter: OHLCV candles via ccxt's free public endpoints.

`fetch()` is synchronous (ccxt is sync); the loop calls it via asyncio.to_thread
so the event loop is never blocked. A schema mismatch raises SchemaError.
"""
from __future__ import annotations

import os
from typing import Any

SCHEMA_VERSION = 1


class SchemaError(RuntimeError):
    """Raised when upstream data does not match the expected schema."""


_exchange = None


def _get_exchange():
    global _exchange
    if _exchange is not None:
        return _exchange
    import ccxt

    name = os.environ.get("HERMES_TRADING_EXCHANGE", "kraken")
    klass = getattr(ccxt, name, None)
    if klass is None:
        raise SchemaError(f"Unknown ccxt exchange '{name}'")
    params: dict[str, Any] = {"enableRateLimit": True}
    key = os.environ.get("EXCHANGE_API_KEY", "").strip()
    secret = os.environ.get("EXCHANGE_API_SECRET", "").strip()
    if key and secret:
        params["apiKey"] = key
        params["secret"] = secret
    _exchange = klass(params)
    return _exchange


def fetch(symbol: str, timeframe: str = "1m", limit: int = 100) -> dict[str, Any]:
    """Return the latest candles for `symbol`.

    Shape: {schema_version, symbol, timeframe, closes:[...], last:float, ts:int}
    """
    ex = _get_exchange()
    candles = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not isinstance(candles, list) or not candles:
        raise SchemaError("price adapter: empty/invalid OHLCV response")
    # each candle: [ts, open, high, low, close, volume]
    try:
        closes = [float(c[4]) for c in candles]
        last_ts = int(candles[-1][0])
    except (IndexError, TypeError, ValueError) as exc:
        raise SchemaError(f"price adapter: malformed candle: {exc}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "closes": closes,
        "last": closes[-1],
        "ts": last_ts,
    }
