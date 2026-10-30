"""Deterministic fake price history for offline backtest tests (no network, no Yahoo).

Eleven fake "sector" series plus SPY, ~7 years of business days, with slowly changing
trends so the rotation strategy has something to rank. Same seed -> same numbers on every
machine (uses numpy's legacy RandomState, which is stable across versions).
"""
import numpy as np
import pandas as pd

SECTORS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def synthetic_close(seed: int = 7, days: int = 1800, crash_at: int | None = None,
                    crash_size: float = -0.09) -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    dates = pd.bdate_range("2012-01-02", periods=days)
    block = 120
    n_blocks = days // block + 1
    cols = {}
    sector_rets = []
    for _ in SECTORS:
        mu = np.repeat(rs.uniform(-0.0004, 0.0009, n_blocks), block)[:days]
        r = mu + rs.normal(0.0, 0.011, days)
        sector_rets.append(r)
    spy_r = np.mean(sector_rets, axis=0) + rs.normal(0.0, 0.002, days)
    sector_rets = [r.copy() for r in sector_rets]
    if crash_at is not None:
        spy_r[crash_at] = crash_size
        for r in sector_rets:
            r[crash_at] = crash_size * 1.1
    for sym, r in zip(SECTORS, sector_rets):
        cols[sym] = 100.0 * np.cumprod(1.0 + r)
    cols["SPY"] = 100.0 * np.cumprod(1.0 + spy_r)
    return pd.DataFrame(cols, index=dates)


def base_cfg(**over):
    cfg = {
        "universe": list(SECTORS), "benchmark": "SPY",
        "momentum_lookbacks_days": [63, 126], "trend_sma_days": 200,
        "hold_top_n": 3, "exit_rank_n": 4, "max_positions": 3,
        "sizing": "equal_weight", "position_notional_cap_pct": 35,
        "catastrophe_stop_pct": 15, "stocks": [],
    }
    cfg.update(over)
    return cfg


EXEC = {"cost_bps_per_side": 5.0, "cash_rate": 0.02, "risk_free": 0.02}   # fixed: no T-bill download
