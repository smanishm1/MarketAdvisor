"""Train/test parameter optimizer — disciplined strategy 'discovery'.

Sweeps a SMALL grid of the rotation dials over an in-sample TRAIN window, picks the best
by risk-adjusted return, then validates the winner on a held-out TEST window it never saw.
The whole point is to SHOW whether an in-sample winner survives out-of-sample (it usually
shrinks hard) — so you don't fool yourself. This is the honest opposite of random-strategy
mining: a tiny, sensible search space + a real out-of-sample test.

    python -m hermes_trading.optimize --preset etf --years 15 --test-frac 0.3

Honest caveat it bakes in: even this overfits a little (you're still picking the best of N
on the train window). Treat the OOS column as the truth, and prefer changes that beat the
current settings out-of-sample by a clear margin — not by a hair.
"""
from __future__ import annotations

import argparse
import copy
import itertools
from typing import Any

from rich.console import Console
from rich.table import Table

from .backtest import _load_history, simulate
from .config import load_strategy, load_strategy_file
from .paths import PRESETS_DIR

console = Console()


def _grid(cfg: dict[str, Any]) -> dict[str, list]:
    htn = int(cfg.get("hold_top_n", 3))
    return {
        "trend_sma_days": [100, 150, 200, 250],
        "exit_rank_n": [htn + 1, htn + 2, htn + 3],
        "catastrophe_stop_pct": [15, 20, 25],
    }


def _combos(grid: dict[str, list]):
    keys = list(grid)
    for vals in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, vals))


def optimize(cfg: dict[str, Any], years: int = 15, test_frac: float = 0.30) -> dict[str, Any]:
    close = _load_history(list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")], years)
    dates = close.index
    split_date = dates[int(len(dates) * (1 - test_frac))]
    grid = _grid(cfg)

    results: list[dict[str, Any]] = []
    for params in _combos(grid):
        v = copy.deepcopy(cfg)
        v.update(params)
        v["max_positions"] = int(v.get("hold_top_n", 3))   # keep the mirror invariant
        try:
            tr = simulate(v, close, end_date=split_date)["strategy"]
            te = simulate(v, close, start_date=split_date)["strategy"]
        except Exception:  # noqa: BLE001 — skip a combo that can't run
            continue
        results.append({"params": params, "train": tr, "test": te})
    if not results:
        raise RuntimeError("no valid parameter combinations")

    base_tr = simulate(cfg, close, end_date=split_date)
    base_te = simulate(cfg, close, start_date=split_date)
    results.sort(key=lambda r: r["train"]["sharpe"], reverse=True)
    return {
        "train_window": (base_tr["start"], base_tr["end"]),
        "test_window": (base_te["start"], base_te["end"]),
        "n_combos": len(results),
        "ranked": results,
        "baseline_test": base_te["strategy"],
        "spy_test": base_te["benchmark"],
    }


def _pstr(p: dict[str, Any]) -> str:
    return f"sma{p['trend_sma_days']} rank{p['exit_rank_n']} stop{p['catastrophe_stop_pct']}"


def _print(opt: dict[str, Any], cfg: dict[str, Any]) -> None:
    best = opt["ranked"][0]
    bt, sp = opt["baseline_test"], opt["spy_test"]
    console.print(
        f"\n[bold]Parameter optimization[/] — {cfg.get('name', 'strategy')} "
        f"({opt['n_combos']} combos)\n"
        f"  train (in-sample):     {opt['train_window'][0]} -> {opt['train_window'][1]}\n"
        f"  test (out-of-sample):  {opt['test_window'][0]} -> {opt['test_window'][1]}\n"
    )

    t = Table(show_header=True, header_style="bold", title="Top in-sample combos (ranked by train Sharpe)")
    t.add_column("#", justify="right"); t.add_column("params")
    t.add_column("train Sharpe", justify="right"); t.add_column("OOS Sharpe", justify="right")
    t.add_column("OOS CAGR", justify="right"); t.add_column("OOS maxDD", justify="right")
    for i, r in enumerate(opt["ranked"][:8], 1):
        t.add_row(
            str(i), _pstr(r["params"]),
            f"{r['train']['sharpe']:.2f}", f"{r['test']['sharpe']:.2f}",
            f"{r['test']['cagr']*100:+.1f}%", f"{r['test']['max_drawdown']*100:.1f}%",
        )
    t.add_row("—", "[dim]current settings[/]", f"[dim]{simulate_or_dash(cfg)}[/]",
              f"{bt['sharpe']:.2f}", f"{bt['cagr']*100:+.1f}%", f"{bt['max_drawdown']*100:.1f}%")
    t.add_row("—", "[dim]SPY buy & hold[/]", "—",
              f"{sp['sharpe']:.2f}", f"{sp['cagr']*100:+.1f}%", f"{sp['max_drawdown']*100:.1f}%")
    console.print(t)

    # honest verdict
    top5 = opt["ranked"][:5]
    avg_train = sum(r["train"]["sharpe"] for r in top5) / len(top5)
    avg_oos = sum(r["test"]["sharpe"] for r in top5) / len(top5)
    beat = sum(1 for r in top5 if r["test"]["sharpe"] > bt["sharpe"])
    console.print(
        f"\n[bold]Verdict[/]\n"
        f"  Best-on-train OOS Sharpe [b]{best['test']['sharpe']:.2f}[/] "
        f"vs current [b]{bt['sharpe']:.2f}[/] vs SPY [b]{sp['sharpe']:.2f}[/].\n"
        f"  Top-5 Sharpe shrank from [b]{avg_train:.2f}[/] in-sample to "
        f"[b]{avg_oos:.2f}[/] out-of-sample (that gap IS the overfitting).\n"
        f"  {beat}/5 top combos beat current settings out-of-sample.\n"
    )
    if best["test"]["sharpe"] > bt["sharpe"] and beat >= 3:
        console.print("  [green]Cautiously promising[/] — the optimized region holds up OOS. "
                      "Still validate on a different period before trusting it.")
    else:
        console.print("  [yellow]Keep current settings.[/] The in-sample winner did not clearly "
                      "beat them out-of-sample — textbook overfitting. Don't chase the train numbers.")


def simulate_or_dash(cfg: dict[str, Any]) -> str:
    return _pstr({
        "trend_sma_days": cfg.get("trend_sma_days", 200),
        "exit_rank_n": cfg.get("exit_rank_n", 4),
        "catastrophe_stop_pct": cfg.get("catastrophe_stop_pct", 15),
    })


def main() -> None:
    ap = argparse.ArgumentParser(description="train/test parameter optimizer")
    ap.add_argument("--years", type=int, default=15)
    ap.add_argument("--test-frac", type=float, default=0.30, help="held-out fraction (default 0.30)")
    ap.add_argument("--preset", help="named preset in config/presets/")
    ap.add_argument("--config", help="path to a strategy yaml")
    args = ap.parse_args()

    if args.config:
        cfg = load_strategy_file(args.config)
    elif args.preset:
        path = PRESETS_DIR / f"{args.preset}.yaml"
        if not path.exists():
            console.print(f"[red]no preset '{args.preset}'[/]")
            return
        cfg = load_strategy_file(path)
    else:
        cfg = load_strategy()
    if cfg.get("type") != "relative_strength_rotation":
        console.print("[red]optimizer is rotation-only.[/]")
        return
    _print(optimize(cfg, args.years, args.test_frac), cfg)


if __name__ == "__main__":
    main()
