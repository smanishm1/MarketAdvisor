"""Execution realism: trading costs, interest on cash, and the risk-free rate.

One home for the assumptions that turn *gross* paper numbers into *net* ones,
shared by the backtester and the live paper book so they can never disagree:

  - ``cost_bps_per_side``: bid/offer half-spread + slippage, charged on EVERY buy
    and sell (buys fill that much higher, sells that much lower). Weekly
    rebalancing makes this matter — ignoring it overstates net performance.
  - ``cash_rate``: idle cash earns the 13-week T-bill yield (``^IRX``, time-varying:
    ~0% in 2020-21, ~5% in 2023-24). Ignoring it understates performance for a
    book that sits partly in cash by design.
  - ``risk_free``: Sharpe is computed on returns in EXCESS of that T-bill rate.

Configured in ``config/goal.yaml`` under ``execution:``. ``cash_rate`` and
``risk_free`` accept ``tbill`` (historical ^IRX), a fixed annual decimal such as
``0.04``, or ``0`` / ``none`` to switch it off.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

import numpy as np
import pandas as pd

from . import db

DEFAULTS: dict[str, Any] = {
    "cost_bps_per_side": 5.0,
    "cash_rate": "tbill",
    "risk_free": "tbill",
}
TRADING_DAYS = 252

_TBILL_TTL = 12 * 3600.0
_tbill: pd.Series | None = None      # annual decimal yield, indexed by date
_tbill_ts = 0.0
_tbill_failed_ts = 0.0


# ---- configuration ---------------------------------------------------------


def settings(goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """The execution block from goal.yaml, with defaults filled in."""
    if goal is None:
        from .config import load_goal
        goal = load_goal()
    out = dict(DEFAULTS)
    out.update(goal.get("execution") or {})
    return out


def gross() -> dict[str, Any]:
    """Frictionless assumptions (the old model) — for before/after comparisons."""
    return {"cost_bps_per_side": 0.0, "cash_rate": 0.0, "risk_free": 0.0}


def cost_rate(exec_cfg: dict[str, Any] | None = None) -> float:
    """Per-side trading cost as a decimal (5 bps -> 0.0005)."""
    cfg = exec_cfg or settings()
    return max(0.0, float(cfg.get("cost_bps_per_side", 0.0))) / 10_000.0


# ---- T-bill (^IRX) -----------------------------------------------------------


def _tbill_annual() -> pd.Series | None:
    """13-week T-bill yield history as an annual decimal series (cached, soft-fail)."""
    global _tbill, _tbill_ts, _tbill_failed_ts
    now = time.time()
    if _tbill is not None and now - _tbill_ts < _TBILL_TTL:
        return _tbill
    if now - _tbill_failed_ts < 3600.0:          # don't hammer a failing fetch
        return _tbill
    try:
        import yfinance as yf

        raw = yf.download("^IRX", period="max", interval="1d",
                          progress=False, auto_adjust=False)
        col = raw["Close"]
        s = (col.iloc[:, 0] if isinstance(col, pd.DataFrame) else col).dropna()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        _tbill = (s / 100.0).astype(float)
        _tbill_ts = now
    except Exception:  # noqa: BLE001 — callers fall back to 0 and report it
        _tbill_failed_ts = now
    return _tbill


def _is_off(kind: Any) -> bool:
    return kind is None or (isinstance(kind, str) and kind.strip().lower() in ("0", "none", "off", ""))


def rate_source(kind: Any) -> str:
    """Human label for where a rate came from (shown next to backtest results)."""
    if _is_off(kind):
        return "none"
    if isinstance(kind, (int, float)) and not isinstance(kind, bool):
        return f"fixed {float(kind):.2%}" if float(kind) else "none"
    return "13-week T-bill (^IRX)" if _tbill_annual() is not None else "unavailable (0%)"


def daily_rates(kind: Any, index: pd.DatetimeIndex) -> np.ndarray:
    """Per-bar daily decimal rate aligned to `index` (compounds to the annual rate)."""
    n = len(index)
    if _is_off(kind):
        return np.zeros(n)
    if isinstance(kind, (int, float)) and not isinstance(kind, bool):
        return np.full(n, (1.0 + float(kind)) ** (1.0 / TRADING_DAYS) - 1.0)
    s = _tbill_annual()
    if s is None:
        return np.zeros(n)
    idx = pd.DatetimeIndex(index).tz_localize(None).normalize()
    aligned = s.reindex(s.index.union(idx)).ffill().reindex(idx).fillna(0.0)
    return ((1.0 + aligned.to_numpy()) ** (1.0 / TRADING_DAYS) - 1.0).clip(min=0.0)


def latest_annual(kind: Any) -> float:
    """Current annual rate (live accrual and live Sharpe)."""
    if _is_off(kind):
        return 0.0
    if isinstance(kind, (int, float)) and not isinstance(kind, bool):
        return float(kind)
    s = _tbill_annual()
    return float(s.iloc[-1]) if s is not None and len(s) else 0.0


# ---- live paper book: interest on idle cash -----------------------------------


def accrued_interest(conn) -> float:
    """Total interest the live paper book's cash has earned so far."""
    try:
        return float(db.get_meta(conn, "cash_interest", "0") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def accrue_cash_interest(conn, cash: float, today: dt.date | None = None) -> float:
    """Credit interest on `cash` for the calendar days since the last accrual.

    Called once per worker tick; only credits when the date has moved on, so it
    runs at most once a day. The first call just starts the clock. Returns the
    amount credited (0.0 most ticks). Caller commits.
    """
    today = today or dt.date.today()
    last = db.get_meta(conn, "cash_interest_date", None)
    if last is None:
        db.set_meta(conn, "cash_interest_date", today.isoformat())
        return 0.0
    days = (today - dt.date.fromisoformat(last)).days
    if days <= 0:
        return 0.0
    amount = 0.0
    if cash > 0:
        annual = latest_annual(settings()["cash_rate"])
        amount = cash * ((1.0 + annual) ** (days / 365.0) - 1.0)
        db.set_meta(conn, "cash_interest", f"{accrued_interest(conn) + amount:.6f}")
    db.set_meta(conn, "cash_interest_date", today.isoformat())
    return amount
