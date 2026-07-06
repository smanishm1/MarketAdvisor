"""
data.py -- all yfinance access goes through this module. Free data only,
no API key required.

Two kinds of reads:
- Daily closes (get_daily_history): used for ALL decisions -- ranking,
  moving averages, rebalance, stops computed off closes. Cached to disk
  (CSV) per symbol so repeated calls in the same day don't hammer Yahoo.
- Live/latest price (get_latest_price): used ONLY for the intraday risk
  overlay (stop-loss / circuit-breaker checks + dashboard display). Never
  used for ranking or rebalance decisions -- those are close-only by design
  (see strategy.py).
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import config

CACHE_DIR = config.BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_last_fetch_ts = {}
_MIN_SECONDS_BETWEEN_FETCH = 5  # simple politeness throttle per symbol


def _cache_path(symbol):
    return CACHE_DIR / f"{symbol}.csv"


def get_daily_history(symbol, lookback_days=800, force_refresh=False):
    """Return a DataFrame indexed by date with a 'Close' column, daily bars,
    covering at least `lookback_days` calendar days back. Uses an on-disk
    cache and only re-fetches from Yahoo if the cache is missing or stale
    (older than today, in terms of the last row's date)."""
    path = _cache_path(symbol)
    today = datetime.now(timezone.utc).date()
    df = None
    if path.exists() and not force_refresh:
        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        except Exception:
            df = None
    needs_fetch = force_refresh or df is None
    if df is not None and not df.empty:
        last_date = df.index[-1].date()
        if last_date < today - timedelta(days=1):
            needs_fetch = True
    if needs_fetch:
        start = today - timedelta(days=lookback_days + 30)
        raw = yf.download(symbol, start=start.isoformat(), progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            if df is not None:
                return df
            raise RuntimeError(f"No data returned for {symbol} from yfinance")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw[["Close"]].copy()
        raw.index.name = "Date"
        raw.to_csv(path)
        df = raw
    cutoff = pd.Timestamp(today - timedelta(days=lookback_days))
    return df[df.index >= cutoff]


def get_daily_history_multi(symbols, lookback_days=800):
    return {s: get_daily_history(s, lookback_days) for s in symbols}


def get_latest_price(symbol):
    """Best-effort 'live' price for the intraday risk overlay. Falls back to
    the last daily close if a live quote isn't available (e.g. market
    closed, or Yahoo hiccup)."""
    throttle_key = symbol
    now = time.time()
    last = _last_fetch_ts.get(throttle_key, 0)
    try:
        tkr = yf.Ticker(symbol)
        fast = tkr.fast_info
        price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
        if price:
            _last_fetch_ts[throttle_key] = now
            return float(price)
    except Exception:
        pass
    # fallback to last close in cache
    try:
        df = get_daily_history(symbol, lookback_days=10)
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def get_latest_prices(symbols):
    return {s: get_latest_price(s) for s in symbols}


def get_daily_return(symbol):
    """Most recent daily % return based on the last two closes (used for
    the black-swan / early-brake checks on SPY)."""
    df = get_daily_history(symbol, lookback_days=10)
    if len(df) < 2:
        return 0.0
    prev, last = df["Close"].iloc[-2], df["Close"].iloc[-1]
    return float((last - prev) / prev)
