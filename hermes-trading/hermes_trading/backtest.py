"""Backtester for the SRSR rotation strategy.

Replays the EXACT same signal logic (hermes_trading.srsr) over historical daily
data so a parameter choice can be judged on years of evidence instead of a handful
of live paper trades. Cash when names don't qualify, daily catastrophe stop,
weekly rebalance.

Results are NET of execution frictions (see hermes_trading.execution): a per-side
trading cost on every buy and sell, T-bill interest on idle cash, and a Sharpe
ratio on returns in excess of the T-bill rate. Pass ``exec_cfg=execution.gross()``
(CLI: ``--gross``) to reproduce the old frictionless numbers.

    python -m hermes_trading.backtest --years 20
    python -m hermes_trading.backtest --gross

Honest health warning: tuning dials until this looks great is curve-fitting. Prefer
robust round numbers, out-of-sample checks, and the per-regime table (a strategy
built to protect on the downside must be judged in downturns, not just in bull runs).
Past performance ≠ future results.
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from . import execution as ex
from . import paper_broker, srsr
from .adapters import equities
from .config import load_strategy, load_strategy_file
from .paper_broker import start_equity
from .paths import PRESETS_DIR

console = Console()

DEFAULT_YEARS = 20

# Named market regimes (SPY peak/trough dates) so every comparison shows how a
# configuration behaves in crashes, rebounds and chop — not only in one bull run.
# A window is reported only when the backtest fully covers its start.
REGIMES: list[tuple[str, str, str | None]] = [
    ("2008 GFC crash",       "2007-10-09", "2009-03-09"),
    ("2009 rebound",         "2009-03-09", "2009-12-31"),
    ("2011 debt scare",      "2011-04-29", "2011-10-03"),
    ("2015-16 chop",         "2015-05-21", "2016-02-11"),
    ("2018 Q4 selloff",      "2018-09-20", "2018-12-24"),
    ("2020 COVID crash",     "2020-02-19", "2020-03-23"),
    ("2020 rebound",         "2020-03-23", "2020-08-31"),
    ("2022 bear market",     "2022-01-03", "2022-10-12"),
    ("2023-26 bull run",     "2023-01-03", None),
]


def _stats(equity: pd.Series, cash_frac: list[float], rf: pd.Series | None = None) -> dict[str, Any]:
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0
    excess = rets - rf.reindex(rets.index).fillna(0.0) if rf is not None else rets
    sd = excess.std()
    sharpe = (excess.mean() / sd * np.sqrt(ex.TRADING_DAYS)) if sd else 0.0
    raw_sd = rets.std()
    sharpe_no_rf = (rets.mean() / raw_sd * np.sqrt(ex.TRADING_DAYS)) if raw_sd else 0.0
    roll_max = equity.cummax()
    max_dd = ((equity - roll_max) / roll_max).min()
    rf_avg = float((1.0 + rf.mean()) ** ex.TRADING_DAYS - 1.0) if rf is not None and len(rf) else 0.0
    return {
        "total_return": float(total),
        "cagr": float(cagr),
        "sharpe": float(sharpe),               # excess over the risk-free rate
        "sharpe_no_rf": float(sharpe_no_rf),   # the old definition, for reference
        "max_drawdown": float(max_dd),
        "pct_cash": float(np.mean(cash_frac)) if cash_frac else 0.0,
        "rf_annual_avg": rf_avg,
    }


def _load_history(symbols: list[str], years: int):
    close = equities.fetch_history(symbols, period="max")
    cutoff = close.index[-1] - pd.DateOffset(years=years)
    return close[close.index >= cutoff]


def _window(s: pd.Series, start: str, end: str | None) -> pd.Series:
    w = s[s.index >= pd.Timestamp(start)]
    return w[w.index <= pd.Timestamp(end)] if end else w


def regime_table(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return / max drawdown per named regime for a simulate() result vs SPY."""
    strat = pd.Series(dict(result["equity_curve"]))
    strat.index = pd.DatetimeIndex(strat.index)
    spy = pd.Series(dict(result["benchmark_curve"]))
    spy.index = pd.DatetimeIndex(spy.index)
    out: list[dict[str, Any]] = []
    for name, start, end in REGIMES:
        if strat.empty or pd.Timestamp(start) < strat.index[0]:
            continue
        w, b = _window(strat, start, end), _window(spy, start, end)
        if len(w) < 5:
            continue
        dd = lambda x: float(((x - x.cummax()) / x.cummax()).min())  # noqa: E731
        out.append({
            "regime": name,
            "start": str(w.index[0].date()), "end": str(w.index[-1].date()),
            "return": float(w.iloc[-1] / w.iloc[0] - 1.0),
            "max_drawdown": dd(w),
            "spy_return": float(b.iloc[-1] / b.iloc[0] - 1.0),
            "spy_max_drawdown": dd(b),
        })
    return out


def run_backtest(cfg: dict[str, Any], years: int = DEFAULT_YEARS,
                 exec_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    close = _load_history(list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")], years)
    return simulate(cfg, close, exec_cfg=exec_cfg)


def compare(
    current_cfg: dict[str, Any], proposed_cfg: dict[str, Any],
    years: int = DEFAULT_YEARS, test_frac: float = 0.30,
    exec_cfg: dict[str, Any] | None = None,
    baseline_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backtest current vs proposed — full period, a held-out out-of-sample (last
    `test_frac`) window, and per-regime. The OOS column is the trustworthy one.
    With `baseline_cfg` (the original settings) it is reported alongside, so every
    proposal also shows how far the book has drifted from where it started."""
    exec_cfg = ex.settings() if exec_cfg is None else exec_cfg
    cfgs = [current_cfg, proposed_cfg] + ([baseline_cfg] if baseline_cfg else [])
    syms = list(dict.fromkeys(
        [s for c in cfgs for s in c.get("universe", [])] + [current_cfg.get("benchmark", "SPY")]
    ))
    close = _load_history(syms, years)
    dates = close.index
    split = dates[int(len(dates) * (1 - test_frac))]

    def run(cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        full = simulate(cfg, close, exec_cfg=exec_cfg)
        oos = simulate(cfg, close, start_date=split, exec_cfg=exec_cfg)["strategy"]
        return full, oos

    cur, cur_oos = run(current_cfg)
    prop, prop_oos = run(proposed_cfg)
    out = {
        "years": years,
        "start": cur["start"], "end": cur["end"],
        "benchmark": cur["benchmark"],
        "current": cur["strategy"],
        "proposed": prop["strategy"],
        "oos_start": str(split.date()),
        "current_oos": cur_oos,
        "proposed_oos": prop_oos,
        "regimes": {"current": regime_table(cur), "proposed": regime_table(prop)},
        "assumptions": cur["assumptions"],
    }
    if baseline_cfg:
        base, base_oos = run(baseline_cfg)
        out["baseline"] = base["strategy"]
        out["baseline_oos"] = base_oos
        out["regimes"]["baseline"] = regime_table(base)
    return out


def simulate(cfg: dict[str, Any], close, start_date=None, end_date=None,
             fast_exits: bool = False, fast_buys: bool = False,
             exec_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Backtest cfg over close. `start_date`/`end_date` window the *recorded* portfolio
    (bars before start_date are still used for indicator warmup) — used for train/test splits.
    `fast_exits` checks rotation exits daily; `fast_buys` opens new positions daily. Default
    is "exit fast (daily), add slow (weekly)". `exec_cfg` sets the frictions (defaults to
    goal.yaml's `execution:` block; ``execution.gross()`` = the old frictionless model).
    """
    exec_cfg = ex.settings() if exec_cfg is None else exec_cfg
    cost = ex.cost_rate(exec_cfg)
    universe = list(cfg.get("universe", []))
    benchmark = cfg.get("benchmark", "SPY")
    max_pos = int(cfg.get("max_positions", 3))
    sizing = str(cfg.get("sizing", "equal_weight")).strip().lower()
    vol_lb = int(cfg.get("vol_lookback_days", 63))
    rank_power = float(cfg.get("rank_power", 1.0))
    # trailing_stop: the catastrophe stop ratchets up to X% below the highest close since
    # entry (never down); X% below entry is the floor. Off = fixed stop set at purchase.
    trailing = bool(cfg.get("trailing_stop", False))
    # trim_to_cap: at each rebalance, a winner whose weight has drifted above its cap +
    # trim_band_pct is sold back down to the cap (ETF cap = trim_weight_pct or the entry cap;
    # stocks use the stock cap). Proceeds go to cash for new buys. Off = winners run.
    trim = bool(cfg.get("trim_to_cap", False))
    trim_band = float(cfg.get("trim_band_pct", 5.0)) / 100.0

    def trim_cap(sym: str) -> float:
        if srsr.is_stock(cfg, sym):
            return srsr.cap_pct(cfg, sym) / 100.0
        return float(cfg.get("trim_weight_pct", srsr.cap_pct(cfg, sym))) / 100.0
    warmup = max(int(cfg.get("trend_sma_days", 200)), max(cfg.get("momentum_lookbacks_days", [63, 126]))) + 5

    if len(close) < warmup + 30:
        raise equities.SchemaError(f"not enough history ({len(close)} bars) for backtest")

    dates = close.index
    cash_r = ex.daily_rates(exec_cfg.get("cash_rate"), dates)
    rf_r = ex.daily_rates(exec_cfg.get("risk_free"), dates)
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
    n_stop_exits = 0
    n_trims = 0
    costs_paid = 0.0
    interest = 0.0
    traded = 0.0
    n_held: list[int] = []        # positions held at each close
    eff_n: list[float] = []       # effective number of holdings, 1/sum(w^2), when invested
    top_w: list[float] = []       # largest position as a share of equity, when invested
    prev_week: tuple[int, int] | None = None

    def sell(sym: str, px: float) -> None:
        nonlocal cash, costs_paid, traded
        gross_value = positions[sym]["shares"] * px
        cash += gross_value * (1.0 - cost)       # sells fill below the quote
        costs_paid += gross_value * cost
        traded += gross_value
        del positions[sym]

    for i in range(begin, stop):
        d = dates[i]
        row = close.iloc[i]

        # 0. idle cash earns the risk-free rate overnight
        if i > begin and cash > 0:
            earned = cash * cash_r[i]
            cash += earned
            interest += earned

        # 1. daily catastrophe stops (then ratchet trailing stops up off today's close)
        for sym in list(positions):
            px = row.get(sym)
            if px is None or pd.isna(px):
                continue
            if px <= positions[sym]["stop"]:
                sell(sym, px)
                n_stop_exits += 1
            elif trailing and px > positions[sym]["peak"]:
                positions[sym]["peak"] = px
                positions[sym]["stop"] = max(
                    positions[sym]["stop"], px * (1 - srsr.stop_pct(cfg, sym) / 100.0)
                )

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
                    sell(sym, px)

            if trim and is_rebalance:
                eq_now = cash + sum(
                    positions[s]["shares"] * row[s] for s in positions if not pd.isna(row.get(s))
                )
                for sym in list(positions):
                    px = row.get(sym)
                    if px is None or pd.isna(px) or eq_now <= 0:
                        continue
                    val = positions[sym]["shares"] * px
                    cap_w = trim_cap(sym)
                    if val / eq_now > cap_w + trim_band:
                        gross_value = val - cap_w * eq_now          # sell the excess back to the cap
                        positions[sym]["shares"] -= gross_value / px
                        cash += gross_value * (1.0 - cost)
                        costs_paid += gross_value * cost
                        traded += gross_value
                        n_trims += 1

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
                    px_fill = px * (1.0 + cost)     # buys fill above the quote
                    shares = paper_broker.weighted_size(
                        sizing, weights, sym, equity, px_fill, max_pos,
                        srsr.cap_pct(cfg, sym),   # per-symbol: tighter for single stocks
                    )
                    shares = min(shares, cash / px_fill)  # never go negative
                    if shares <= 0:
                        continue
                    cash -= shares * px_fill
                    costs_paid += shares * px * cost
                    traded += shares * px
                    positions[sym] = {
                        "shares": shares,
                        "stop": px * (1 - srsr.stop_pct(cfg, sym) / 100.0),
                        "peak": px,
                    }
                    n_trades += 1

        vals = [positions[s]["shares"] * row[s] for s in positions if not pd.isna(row.get(s))]
        n_held.append(len(vals))
        if vals:
            tot = sum(vals)
            eff_n.append(1.0 / sum((v / tot) ** 2 for v in vals))
            top_w.append(max(vals) / equity if equity else 0.0)

        curve_dates.append(d)
        curve_equity.append(equity)
        cash_frac.append((cash / equity) if equity else 1.0)

    if not curve_equity:
        raise equities.SchemaError("empty backtest window")
    idx = pd.DatetimeIndex(curve_dates)
    equity_s = pd.Series(curve_equity, index=idx)
    rf_s = pd.Series(rf_r[begin:stop], index=idx)
    strat_stats = _stats(equity_s, cash_frac, rf_s)
    years = max((idx[-1] - idx[0]).days / 365.25, 1e-9)
    avg_equity = float(equity_s.mean()) or e0
    strat_stats.update({
        "n_trades": n_trades,
        "n_stop_exits": n_stop_exits,
        "n_trims": n_trims,
        "costs_paid": round(costs_paid, 2),
        "interest_earned": round(interest, 2),
        "turnover_annual": traded / avg_equity / years,   # traded notional / avg equity / yr
        "avg_positions": float(np.mean(n_held)) if n_held else 0.0,
        "avg_effective_n": float(np.mean(eff_n)) if eff_n else 0.0,
        "avg_top_weight": float(np.mean(top_w)) if top_w else 0.0,
    })

    # SPY buy & hold over the same window (adjusted closes include dividends)
    spy = close[benchmark].reindex(idx).ffill()
    spy_equity = e0 * (spy / spy.iloc[0])
    spy_stats = _stats(spy_equity, [0.0], rf_s)

    return {
        "start": str(idx[0].date()),
        "end": str(idx[-1].date()),
        "final_equity": float(equity_s.iloc[-1]),
        "strategy": strat_stats,
        "benchmark": spy_stats,
        "equity_curve": [(str(d.date()), round(v, 2)) for d, v in equity_s.items()],
        "benchmark_curve": [(str(d.date()), round(v, 2)) for d, v in spy_equity.items()],
        "assumptions": {
            "cost_bps_per_side": round(cost * 10_000, 2),
            "cash_rate": ex.rate_source(exec_cfg.get("cash_rate")),
            "risk_free": ex.rate_source(exec_cfg.get("risk_free")),
        },
    }


def _print_report(r: dict[str, Any]) -> None:
    s, b, a = r["strategy"], r["benchmark"], r["assumptions"]
    console.print(
        f"\n[bold]SRSR backtest[/]  {r['start']} -> {r['end']}  "
        f"(start ${start_equity():,.0f} -> end ${r['final_equity']:,.0f})\n"
        f"[dim]net of {a['cost_bps_per_side']:g} bps/side costs · cash earns {a['cash_rate']} · "
        f"Sharpe vs {a['risk_free']}[/]\n"
    )
    t = Table(show_header=True, header_style="bold")
    t.add_column("metric"); t.add_column("SRSR", justify="right"); t.add_column("SPY buy&hold", justify="right")
    t.add_row("Total return", f"{s['total_return']:+.1%}", f"{b['total_return']:+.1%}")
    t.add_row("CAGR", f"{s['cagr']:+.1%}", f"{b['cagr']:+.1%}")
    t.add_row("Max drawdown", f"{s['max_drawdown']:.1%}", f"{b['max_drawdown']:.1%}")
    t.add_row("Sharpe (excess of T-bill)", f"{s['sharpe']:.2f}", f"{b['sharpe']:.2f}")
    t.add_row("Avg % in cash", f"{s['pct_cash']:.0%}", "0%")
    t.add_row("Trades", str(s["n_trades"]), "1")
    t.add_row("Trading costs paid", f"${s['costs_paid']:,.0f}", "—")
    t.add_row("Interest earned on cash", f"${s['interest_earned']:,.0f}", "—")
    t.add_row("Turnover (x equity / yr)", f"{s['turnover_annual']:.1f}x", "—")
    console.print(t)
    rt = Table(show_header=True, header_style="bold", title="By market regime")
    for col in ("regime", "SRSR return", "SRSR max DD", "SPY return", "SPY max DD"):
        rt.add_column(col, justify="left" if col == "regime" else "right")
    for g in regime_table(r):
        rt.add_row(g["regime"], f"{g['return']:+.1%}", f"{g['max_drawdown']:.1%}",
                   f"{g['spy_return']:+.1%}", f"{g['spy_max_drawdown']:.1%}")
    console.print(rt)
    console.print(
        "\n[dim]Reminder: a pretty backtest can be curve-fit. Check out-of-sample "
        "and the regime table before trusting any dial change.[/]\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SRSR backtester")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"lookback window (default {DEFAULT_YEARS})")
    parser.add_argument("--preset", help="named preset in config/presets/ (e.g. etf, leaders)")
    parser.add_argument("--config", help="path to a strategy yaml to backtest")
    parser.add_argument("--gross", action="store_true",
                        help="frictionless (no costs, no cash interest, raw Sharpe) — the old model")
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
    _print_report(run_backtest(cfg, args.years, ex.gross() if args.gross else None))


if __name__ == "__main__":
    main()
