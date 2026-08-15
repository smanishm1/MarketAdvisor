"""Equity/ETF data adapter via yfinance (free, no key).

Daily adjusted closes for a list of symbols. Synchronous (yfinance is sync); the
loop calls it via asyncio.to_thread. Used by both the live rotation engine and the
backtester.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1


class SchemaError(RuntimeError):
    """Raised when upstream data is missing or too short to act on."""


def fetch_history(symbols: list[str], period: str = "2y"):
    """Return a DataFrame of daily adjusted closes (index=date, columns=symbols)."""
    import pandas as pd
    import yfinance as yf

    raw = yf.download(
        symbols,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw is None or raw.empty:
        raise SchemaError("yfinance returned no data")

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            raise SchemaError("no Close field in yfinance response")
        close = raw["Close"].copy()
    else:  # single symbol -> flat columns
        if "Close" not in raw.columns:
            raise SchemaError("no Close field in yfinance response")
        close = raw[["Close"]].copy()
        close.columns = [symbols[0] if isinstance(symbols, list) else symbols]

    return close.dropna(how="all")


def fetch_quotes(symbols: list[str]) -> dict[str, float]:
    """Current-ish intraday price per symbol (last 1-minute bar).

    For the intraday RISK check only (stops + black-swan), never for ranking.
    Returns {} on failure or when the market is closed — caller falls back to the
    daily close, so this degrades gracefully.
    """
    import logging

    import pandas as pd
    import yfinance as yf

    # When the market is closed there are no 1m bars, and yfinance logs a noisy
    # "possibly delisted; no price data found" error per symbol. That's expected
    # here (this fetch is best-effort), so mute yfinance's logger for the call.
    yf_log = logging.getLogger("yfinance")
    prev_level = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        raw = yf.download(
            symbols, period="1d", interval="1m",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception:  # noqa: BLE001 — intraday is best-effort
        return {}
    finally:
        yf_log.setLevel(prev_level)
    if raw is None or raw.empty:
        return {}
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            return {}
        close = raw["Close"]
    else:
        if "Close" not in raw.columns:
            return {}
        close = raw[["Close"]]
        close.columns = [symbols[0] if isinstance(symbols, list) else symbols]

    out: dict[str, float] = {}
    for s in (symbols if isinstance(symbols, list) else [symbols]):
        if s in close.columns:
            ser = close[s].dropna()
            if len(ser):
                out[s] = float(ser.iloc[-1])
    return out


def fetch_daily(
    symbols: list[str], period: str = "2y", min_bars: int = 210
) -> dict[str, dict[str, Any]]:
    """Per-symbol {schema_version, closes:[...], last, ts}. Raises on short/missing data."""
    close = fetch_history(symbols, period)
    out: dict[str, dict[str, Any]] = {}
    for s in symbols:
        if s not in close.columns:
            raise SchemaError(f"equities adapter: '{s}' missing from response")
        ser = close[s].dropna()
        if len(ser) < min_bars:
            raise SchemaError(f"equities adapter: '{s}' only {len(ser)} bars (<{min_bars})")
        out[s] = {
            "schema_version": SCHEMA_VERSION,
            "symbol": s,
            "closes": [float(x) for x in ser.tolist()],
            "last": float(ser.iloc[-1]),
            "ts": int(ser.index[-1].timestamp()),
        }
    return out
