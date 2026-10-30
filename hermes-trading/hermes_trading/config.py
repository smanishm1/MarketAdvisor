"""Load and validate goal.yaml and strategy.yaml. Read live on every tick."""
from __future__ import annotations

from typing import Any

import yaml

from .paths import GOAL_FILE, STRATEGY_FILE


# What a missing key in goal.yaml falls back to. These match config/goal.example.yaml (the ETF
# rotation). They used to be the old crypto numbers (5% / 8% drawdown / Sharpe 1.2 / -4%), which
# silently applied to anyone whose goal.yaml lacked a key. See docs/GOAL-BAR.md.
DEFAULT_GOAL: dict[str, Any] = {
    "target_return_30d": 0.015,
    "max_drawdown": 0.15,
    "min_sharpe": 1.0,
    "failure_below": -0.10,
    "reflection_every": 8,
    "one_variable_only": True,
}
DEFAULT_BLACK_SWAN: dict[str, Any] = {
    "early_warn_day_pct": -0.05,
    "crash_day_pct": -0.07,
    "max_portfolio_drawdown_pct": 0.20,
    "flatten_on_trigger": True,
}


def load_goal() -> dict[str, Any]:
    with GOAL_FILE.open(encoding="utf-8") as f:
        goal = yaml.safe_load(f) or {}
    goal.setdefault("asset", "BTC/USDT")   # only used by the legacy single-asset RSI path
    for key, value in DEFAULT_GOAL.items():
        goal.setdefault(key, value)
    bs = goal.setdefault("black_swan", {})
    for key, value in DEFAULT_BLACK_SWAN.items():
        bs.setdefault(key, value)
    return goal


def load_strategy() -> dict[str, Any]:
    return load_strategy_file(STRATEGY_FILE)


def load_strategy_file(path) -> dict[str, Any]:
    """Load + normalise any strategy yaml (active file, a preset, or a backtest config)."""
    with open(path, encoding="utf-8") as f:
        strat = yaml.safe_load(f) or {}
    # normalise: version always a zero-padded string; type drives the engine
    strat["version"] = str(strat.get("version", "01")).zfill(2)
    stype = strat.setdefault("type", "rsi")

    if stype == "relative_strength_rotation":
        strat.setdefault("universe", [])
        strat.setdefault("benchmark", "SPY")
        strat.setdefault("momentum_lookbacks_days", [63, 126])
        # optional per-horizon weights (negative = reversal); absent = equal weights
        mw = strat.get("momentum_weights")
        if mw is not None and len(mw) != len(strat["momentum_lookbacks_days"]):
            raise ValueError(
                f"momentum_weights {mw} must have one weight per lookback "
                f"{strat['momentum_lookbacks_days']}"
            )
        strat.setdefault("trend_sma_days", 200)
        strat.setdefault("hold_top_n", 3)
        strat.setdefault("exit_rank_n", 4)
        strat.setdefault("rebalance", "weekly")
        strat.setdefault("sizing", "equal_weight")
        strat.setdefault("position_notional_cap_pct", 35)
        strat.setdefault("catastrophe_stop_pct", 15)
        # single-stock risk rules (only meaningful when `stocks` names members of
        # the universe): tighter cap, wider stop, and a slot budget. Defaults keep
        # a pure-ETF config byte-for-byte equivalent to the old behaviour.
        strat.setdefault("stocks", [])
        strat["stocks"] = [s for s in strat["stocks"] if s in strat["universe"]]
        strat.setdefault("stock_notional_cap_pct", strat["position_notional_cap_pct"])
        strat.setdefault("stock_catastrophe_stop_pct", strat["catastrophe_stop_pct"])
        strat.setdefault("max_stock_positions", int(strat["hold_top_n"]))
        # hold_top_n is the single source of truth for slot count; max_positions
        # mirrors it so the two can never drift out of sync (no silent no-op changes).
        strat["max_positions"] = int(strat["hold_top_n"])
    else:  # rsi (default)
        entry = strat.setdefault("entry", {})
        entry.setdefault("indicator", "rsi")
        entry.setdefault("period", 14)
        entry.setdefault("threshold", 30)
        entry.setdefault("direction", "long")
        strat.setdefault("stop_loss_pct", 2.0)
        strat.setdefault("take_profit_pct", 4.0)
        strat.setdefault("position_size_r", 0.5)
    return strat


def dump_strategy(strat: dict[str, Any]) -> str:
    """Serialise a strategy dict back to YAML text (stable key order)."""
    return yaml.safe_dump(strat, sort_keys=False, default_flow_style=False)


def save_strategy(strat: dict[str, Any]) -> None:
    STRATEGY_FILE.write_text(dump_strategy(strat), encoding="utf-8")
