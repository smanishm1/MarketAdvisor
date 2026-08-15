"""Backtester for the SRSR rotation strategy.

Replays the EXACT same signal logic (hermes_trading.srsr) over historical daily
data so a parameter choice can be judged on years of evidence instead of a handful
of live paper trades. Equal-weight, cash when sectors don't qualify, daily
catastrophe stop, weekly rebalance.

    python -m hermes_trading.backtest --years 15

Honest health warning: tuning dials until this looks great is curve-fitting. Prefer
robust round numbers and out-of-sample checks. Past performance ≠ future results.
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from . import srsr
from .adapters import equities
from .config import load_strategy, load_strategy_file
from . import paper_broker
from .paper_broker import start_equity
from .paths import PRESETS_DIR

console = Console()


def _stats(equity: pd.Series, cash_frac: list[float]) -> dict[str, Any]:
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0
    sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() else 0.0
    roll_max = equity.cummax()
    max_dd = ((equity - roll_max) / roll_max).min()
    return {
        "total_return": total,
        "cagr": cagr,
        "sharpe": float(sharpe),
        "max_drawdown": float(max_dd),
        "pct_cash": float(np.mean(cash_frac)) if cash_frac else 0.0,
    }


def _load_history(symbols: list[str], years: int):
    close = equities.fetch_history(symbols, period="max")
    cutoff = close.index[-1] - pd.DateOffset(years=years)
    return close[close.index >= cutoff]


def run_backtest(cfg: dict[str, Any], years: int = 15) -> dict[str, Any]:
    close = _load_history(list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")], years)
    return simulate(cfg, close)


def compare(
    current_cfg: dict[str, Any], proposed_cfg: dict[str, Any],
    years: int = 10, test_frac: float = 0.30,
) -> dict[str, Any]:
    """Backtest current vs proposed (one dial changed) — full period AND a held-out
    out-of-sample (last `test_frac`) window. The OOS column is the trustworthy one."""
    syms = list(dict.fromkeys(
        list(current_cfg.get("universe", []))
        + [current_cfg.get("benchmark", "SPY")]
        + list(proposed_cfg.get("universe", []))
    ))
    close = _load_history(syms, years)
    cur = simulate(current_cfg, close)
    prop = simulate(proposed_cfg, close)

    dates = close.index
    split = dates[int(len(dates) * (1 - test_frac))]
    cur_oos = simulate(current_cfg, close, start_date=split)["strategy"]
    prop_oos = simulate(proposed_cfg, close, start_date=split)["strategy"]
    return {
        "years": years,
        "start": cur["start"], "end": cur["end"],
        "benchmark": cur["benchmark"],
        "current": cur["strategy"],
        "proposed": prop["strategy"],
        "oos_start": str(split.date()),
        "current_oos": cur_oos,
        "proposed_oos": prop_oos,
    }


def simulate(cfg: dict[str, Any], close, start_date=None, end_date=None,
             fast_exits: bool = False, fast_buys: bool = False) -> dict[str, Any]:
    """Backtest cfg over close. `start_date`/`end_date` window the *recorded* portfolio
    (bars before start_date are still used for indicator warmup) — used for train/test splits.
    `fast_exits` checks rotation exits daily; `fast_buys` opens new positions daily. Default
    is "exit fast (daily), add slow (weekly)".
    """
    universe = list(cfg.get("universe", []))
    benchmark = cfg.get("benchmark", "SPY")
    max_pos = int(cfg.get("max_positions", 3))
    sizing = str(cfg.get("sizing", "equal_weight")).strip().lower()
    vol_lb = int(cfg.get("vol_lookback_days", 63))
    rank_power = float(cfg.get("rank_power", 1.0))
    warmup = max(int(cfg.get("trend_sma_days", 200)), max(cfg.get("momentum_lookbacks_days", [63, 126]))) + 5

    if len(close) < warmup + 30:
        raise equities.SchemaError(f"not enough history ({len(close)} bars) for backtest")

    dates = close.index
    begin = warmup
    if start_date is not None:
        begin = max(warmup, int(dates.searchsorted(pd.Timestamp(start_date))))
    stop = len(dates)
    if end_date is not None:
        stop = min(stop, int(dates.searchsorted(pd.Timestamp(end_date))))

    e0 = start_equity()
    cash = e0
    positions: dict[str, dict[str, float]] = {}   # sym -> {shares, stop}
    curve_dates: list[Any] = []
    curve_equity: list[float] = []
    cash_frac: list[float] = []
    n_trades = 0
    prev_week: tuple[int, int] | None = None

    for i in range(begin, stop):
        d = dates[i]
        row = close.iloc[i]

        # 1. daily catastrophe stops
        for sym in list(positions):
            px = row.get(sym)
            if px is None or pd.isna(px):
                continue
            if px <= positions[sym]["stop"]:
                cash += positions[sym]["shares"] * px
                del positions[sym]

        # 2. mark equity
        held_value = sum(
            positions[s]["shares"] * row[s] for s in positions if not pd.isna(row.get(s))
        )
        equity = cash + held_value

        # 3. decisions — exits weekly OR daily (fast_exits); buys weekly OR daily (fast_buys)
        wk = d.isocalendar()[:2]
        is_rebalance = (wk != prev_week)
        prev_week = wk
        if is_rebalance or fast_exits or fast_buys:
            hist = close.iloc[: i + 1]
            prices = {s: hist[s].dropna().tolist() for s in universe}
            bench = hist[benchmark].dropna().tolist()
            decision = srsr.evaluate(prices, bench, cfg)
            buys, sells = srsr.actions(decision, list(positions), cfg)

            if is_rebalance or fast_exits:
                for sym, _reason in sells:
                    px = row.get(sym)
                    if px is None or pd.isna(px):
                        continue
                    cash += positions[sym]["shares"] * px
                    del positions[sym]

            if is_rebalance or fast_buys:
                equity = cash + sum(
                    positions[s]["shares"] * row[s] for s in positions if not pd.isna(row.get(s))
                )
                # weight new buys within the intended basket (keeps + buys)
                basket = list(positions) + [s for s in buys if s not in positions]
                vols = (
                    {s: v for s in basket
                     if (v := srsr.volatility(prices.get(s, []), vol_lb)) is not None}
                    if sizing == "inverse_vol" else None
                )
                weights = paper_broker.sizing_weights(
                    sizing, basket, scores=decision.scores, vols=vols, rank_power=rank_power
                )
                for sym in buys:
                    if len(positions) >= max_pos or sym in positions:
                        continue
                    px = row.get(sym)
                    if px is None or pd.isna(px) or px <= 0:
                        continue
                    shares = paper_broker.weighted_size(
                        sizing, weights, sym, equity, px, max_pos,
                        srsr.cap_pct(cfg, sym),   # per-symbol: tighter for single stocks
                    )
                    shares = min(shares, cash / px)  # never go negative
                    if shares <= 0:
                        continue
                    cash -= shares * px
                    positions[sym] = {
                        "shares": shares,
                        "stop": px * (1 - srsr.stop_pct(cfg, sym) / 100.0),
                    }
                    n_trades += 1

        curve_dates.append(d)
        curve_equity.append(equity)
        cash_frac.append((cash / equity) if equity else 1.0)

    if not curve_equity:
        raise equities.SchemaError("empty backtest window")
    equity_s = pd.Series(curve_equity, index=pd.DatetimeIndex(curve_dates))
    strat_stats = _stats(equity_s, cash_frac)
    strat_stats["n_trades"] = n_trades

    # SPY buy & hold over the same window
    spy = close[benchmark].reindex(equity_s.index).ffill()
    spy_equity = e0 * (spy / spy.iloc[0])
    spy_stats = _stats(spy_equity, [0.0])

    return {
        "start": str(equity_s.index[0].date()),
        "end": str(equity_s.index[-1].date()),
        "final_equity": float(equity_s.iloc[-1]),
        "strategy": strat_stats,
        "benchmark": spy_stats,
        "equity_curve": [(str(d.date()), round(v, 2)) for d, v in equity_s.items()],
    }


def _print_report(r: dict[str, Any]) -> None:
    s, b = r["strategy"], r["benchmark"]
    console.print(
        f"\n[bold]SRSR backtest[/]  {r['start']} -> {r['end']}  "
        f"(start ${start_equity():,.0f} -> end ${r['final_equity']:,.0f})\n"
    )
    t = Table(show_header=True, header_style="bold")
    t.add_column("metric"); t.add_column("SRSR", justify="right"); t.add_column("SPY buy&hold", justify="right")
    t.add_row("Total return", f"{s['total_return']:+.1%}", f"{b['total_return']:+.1%}")
    t.add_row("CAGR", f"{s['cagr']:+.1%}", f"{b['cagr']:+.1%}")
    t.add_row("Max drawdown", f"{s['max_drawdown']:.1%}", f"{b['max_drawdown']:.1%}")
    t.add_row("Sharpe", f"{s['sharpe']:.2f}", f"{b['sharpe']:.2f}")
    t.add_row("Avg % in cash", f"{s['pct_cash']:.0%}", "0%")
    t.add_row("Trades", str(s["n_trades"]), "1")
    console.print(t)
    console.print(
        "\n[dim]Reminder: a pretty backtest can be curve-fit. Check out-of-sample "
        "before trusting any dial change.[/]\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SRSR backtester")
    parser.add_argument("--years", type=int, default=15, help="lookback window (default 15)")
    parser.add_argument("--preset", help="named preset in config/presets/ (e.g. etf, leaders)")
    parser.add_argument("--config", help="path to a strategy yaml to backtest")
    args = parser.parse_args()

    if args.config:
        cfg = load_strategy_file(args.config)
    elif args.preset:
        path = PRESETS_DIR / f"{args.preset}.yaml"
        if not path.exists():
            console.print(f"[red]no preset '{args.preset}' in {PRESETS_DIR}[/]")
            return
        cfg = load_strategy_file(path)
    else:
        cfg = load_strategy()

    if cfg.get("type") != "relative_strength_rotation":
        console.print("[red]not a relative_strength_rotation config (backtester is rotation-only).[/]")
        return
    console.print(f"[dim]strategy: {cfg.get('name', 'active strategy.yaml')}[/]")
    _print_report(run_backtest(cfg, args.years))


if __name__ == "__main__":
    main()
