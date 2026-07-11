"""Score realised trades against the goal. Returns a float in [-1, +1]."""
from __future__ import annotations

import math
from typing import Any

from .paper_broker import start_equity


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def realised_return(trades: list[dict[str, Any]]) -> float:
    """Cumulative realised PnL as a fraction of starting equity."""
    total = sum(t.get("pnl") or 0.0 for t in trades if t.get("status") == "closed")
    e0 = start_equity()
    return total / e0 if e0 else 0.0


def max_drawdown(equity_curve: list[float]) -> float:
    """Largest peak-to-trough drop as a positive fraction (0.08 == 8%)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def sharpe(trades: list[dict[str, Any]]) -> float:
    """Naive per-trade Sharpe from realised pnl_pct (mean / std)."""
    rets = [t["pnl_pct"] for t in trades if t.get("status") == "closed" and t.get("pnl_pct") is not None]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    return mean / std if std else 0.0


def score(
    trades: list[dict[str, Any]],
    goal: dict[str, Any],
    equity_curve: list[float] | None = None,
) -> dict[str, Any]:
    """Composite score in [-1, +1] plus the components that produced it."""
    ret = realised_return(trades)
    mdd = max_drawdown(equity_curve or [])
    shp = sharpe(trades)

    target = goal.get("target_return_30d", 0.05) or 0.05
    max_dd = goal.get("max_drawdown", 0.08) or 0.08
    min_shp = goal.get("min_sharpe", 1.2) or 1.2
    floor = goal.get("failure_below", -0.04)

    return_score = _clip(ret / target) if target else 0.0
    dd_score = _clip(1.0 - (mdd / max_dd)) if max_dd else 0.0   # 1 when no dd, ->neg when over
    sharpe_score = _clip(shp / min_shp) if min_shp else 0.0

    composite = _clip(0.5 * return_score + 0.3 * dd_score + 0.2 * sharpe_score)
    if ret < floor:
        composite = _clip(min(composite, -0.8))  # steeply negative below the floor

    return {
        "composite": round(composite, 4),
        "realised_return": round(ret, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(shp, 4),
        "return_score": round(return_score, 4),
        "dd_score": round(dd_score, 4),
        "sharpe_score": round(sharpe_score, 4),
    }
