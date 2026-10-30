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
    python -m hermes_trading.backtest --live-like        # mirror the live worker's timing + brakes
    python -m hermes_trading.backtest --compare-modes    # original vs live-like, one change at a time

By default the timing is the ORIGINAL backtest model (so old numbers stay comparable): rebalance on
the first bar of the week, exits only at the rebalance, trades filled at the same close the signal
was computed on, no safety brakes. The live app does none of those four things. See
docs/BACKTEST-PARITY.md.

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
from .config import load_goal, load_strategy, load_strategy_file
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
                 exec_cfg: dict[str, Any] | None = None, **sim_opts: Any) -> dict[str, Any]:
    """Load history and simulate. `sim_opts` = timing options (see ``simulate`` / ``live_like_options``)."""
    close = _load_history(list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")], years)
    return simulate(cfg, close, exec_cfg=exec_cfg, **sim_opts)


def compare_modes(cfg: dict[str, Any], years: int = DEFAULT_YEARS,
                  exec_cfg: dict[str, Any] | None = None, progress=None) -> list[tuple[str, dict[str, Any]]]:
    """Same strategy, same data, run under the original timing, then each live-app difference
    ON ITS OWN, then all four together — so you can see which difference actually matters."""
    close = _load_history(list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")], years)
    live = live_like_options()
    variants: list[tuple[str, dict[str, Any]]] = [
        ("original timing", {}),
        ("Friday rebalance only", {"rebalance_day": "friday"}),
        ("daily exits only", {"fast_exits": True}),
        ("fill next bar only", {"lag": 1}),
        ("safety brakes only", {"brakes": live["brakes"]}),
        ("all four (live-like)", live),
    ]
    out = []
    for label, opts in variants:
        if progress:
            progress(label)
        out.append((label, simulate(cfg, close, exec_cfg=exec_cfg, **opts)))
    return out


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


REBALANCE_DAYS = ("monday", "friday")


def rebalance_bars(dates, begin: int, stop: int, rebalance_day: str = "monday") -> set[int]:
    """Indices of the bars on which the weekly rebalance happens.

    "monday" (the original backtest behaviour): the first trading bar of each ISO week.
    "friday": the last trading bar of each ISO week, which is what the live app's Friday
    rebalance does. In both modes the very first bar counts too (the live worker also
    rebalances on its first run).
    """
    if rebalance_day not in REBALANCE_DAYS:
        raise ValueError(f"rebalance_day must be one of {REBALANCE_DAYS}, got {rebalance_day!r}")
    wks = [d.isocalendar()[:2] for d in dates]
    bars: set[int] = set()
    for i in range(begin, stop):
        if i == begin:
            bars.add(i)
        elif rebalance_day == "monday":
            if wks[i] != wks[i - 1]:
                bars.add(i)
        elif i + 1 < len(dates) and wks[i + 1] != wks[i]:
            bars.add(i)
    return bars


def brakes_from_goal(goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """The live worker's safety-brake thresholds (goal.yaml `black_swan:`), same defaults."""
    if goal is None:
        goal = load_goal()
    bs = goal.get("black_swan", {}) or {}
    return {
        "early_warn_day_pct": float(bs.get("early_warn_day_pct", -0.05)),
        "crash_day_pct": float(bs.get("crash_day_pct", -0.07)),
        "max_portfolio_drawdown_pct": float(bs.get("max_portfolio_drawdown_pct", 0.20)),
        "flatten_on_trigger": bool(bs.get("flatten_on_trigger", True)),
    }


def live_like_options(goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """simulate() options that make the backtest behave like the live worker:
    Friday rebalance, exits checked daily, trades filled one bar after the decision, and
    the crash / drawdown / early-warning brakes switched on."""
    return {"rebalance_day": "friday", "lag": 1, "fast_exits": True,
            "brakes": brakes_from_goal(goal)}


def simulate(cfg: dict[str, Any], close, start_date=None, end_date=None,
             fast_exits: bool = False, fast_buys: bool = False,
             exec_cfg: dict[str, Any] | None = None,
             rebalance_day: str = "monday", lag: int = 0,
             brakes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Backtest cfg over close. `start_date`/`end_date` window the *recorded* portfolio
    (bars before start_date are still used for indicator warmup) — used for train/test splits.
    `exec_cfg` sets the frictions (defaults to goal.yaml's `execution:` block;
    ``execution.gross()`` = the old frictionless model).

    Timing options. The defaults reproduce the ORIGINAL backtest exactly (so old numbers stay
    comparable); ``live_like_options()`` gives the settings that mirror the live worker.
      fast_exits     check rotation exits every bar (live default: on). Default here: off, so
                     exits happen only at the weekly rebalance.
      fast_buys      open new positions every bar instead of weekly.
      rebalance_day  "monday" = first bar of the week (original); "friday" = last bar of the week.
      lag            bars between a decision and its fill. 0 = decide and trade at the same
                     closing price (original, slightly optimistic); 1 = trade at the next
                     bar's close, like the live app where you approve after the signal.
      brakes         None = off (original). A dict (see ``brakes_from_goal``) turns on the live
                     app's safety brakes: SPY day <= crash_day_pct or equity drawdown >=
                     max_portfolio_drawdown_pct flattens to cash and stops buying until SPY is
                     back above its 200-day average (a drawdown trigger also needs the drawdown
                     to heal); SPY day <= early_warn_day_pct pauses new buys for that day.
                     Daily bars only, so it reacts at the close, not intraday.
    """
    exec_cfg = ex.settings() if exec_cfg is None else exec_cfg
    cost = ex.cost_rate(exec_cfg)
    if lag < 0 or int(lag) != lag:
        raise ValueError(f"lag must be a whole number of bars >= 0, got {lag!r}")
    lag = int(lag)
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
    rebal_bars = rebalance_bars(dates, begin, stop, rebalance_day)

    e0 = start_equity()
    cash = e0
    equity = e0
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
    pending: list[tuple[int, dict[str, Any]]] = []   # (bar to fill on, orders) when lag > 0

    # safety brakes (only when `brakes` is given)
    spy_close = close[benchmark] if benchmark in close.columns else None
    spy_sma200 = spy_close.rolling(200).mean() if spy_close is not None else None
    risk_off = False
    buys_paused = False
    peak = e0
    brake_log: list[dict[str, Any]] = []
    n_brake_flattened = 0
    days_risk_off = 0
    days_buys_paused = 0

    def sell(sym: str, px: float) -> None:
        nonlocal cash, costs_paid, traded
        gross_value = positions[sym]["shares"] * px
        cash += gross_value * (1.0 - cost)       # sells fill below the quote
        costs_paid += gross_value * cost
        traded += gross_value
        del positions[sym]

    def execute(o: dict[str, Any], row) -> None:
        """Carry out one decision's orders (exits, trims, buys) at THIS bar's prices."""
        nonlocal cash, equity, costs_paid, traded, n_trims, n_trades
        if o["do_exit"]:
            for sym, _reason in o["sells"]:
                px = row.get(sym)
                if px is None or pd.isna(px) or sym not in positions:
                    continue
                sell(sym, px)

        if trim and o["rebalance"]:
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

        if o["do_buy"]:
            equity = cash + sum(
                positions[s]["shares"] * row[s] for s in positions if not pd.isna(row.get(s))
            )
            prices = o["prices"]
            # weight new buys within the intended basket (keeps + buys)
            basket = list(positions) + [s for s in o["buys"] if s not in positions]
            vols = (
                {s: v for s in basket
                 if (v := srsr.volatility(prices.get(s, []), vol_lb)) is not None}
                if sizing == "inverse_vol" else None
            )
            weights = paper_broker.sizing_weights(
                sizing, basket, scores=o["scores"], vols=vols, rank_power=rank_power
            )
            for sym in o["buys"]:
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

        # 2b. safety brakes (the live worker's black-swan guard), on the daily close
        if brakes is not None and spy_close is not None and i > 0:
            spy_now, spy_prev = float(spy_close.iloc[i]), float(spy_close.iloc[i - 1])
            spy_ret = (spy_now / spy_prev - 1.0) if spy_prev and not np.isnan(spy_prev) else 0.0
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak else 0.0
            crash_lim = float(brakes.get("crash_day_pct", -0.07))
            dd_lim = float(brakes.get("max_portfolio_drawdown_pct", 0.20))
            early_lim = float(brakes.get("early_warn_day_pct", -0.05))
            reason = None
            if spy_ret <= crash_lim:
                reason = f"SPY {spy_ret:+.1%} in a day"
            elif dd >= dd_lim:
                reason = f"portfolio drawdown {dd:.1%}"
            triggered = False
            if reason and not risk_off:
                risk_off = True
                triggered = True
                flattened = 0
                if brakes.get("flatten_on_trigger", True):
                    for sym in list(positions):
                        px = row.get(sym)
                        if px is None or pd.isna(px):
                            continue
                        sell(sym, px)
                        flattened += 1
                    equity = cash
                    peak = equity   # drawdown is measured from the flatten on (matches loop.py)
                n_brake_flattened += flattened
                brake_log.append({"date": str(d.date()), "event": "risk_off",
                                  "reason": reason, "positions_flattened": flattened})
            elif risk_off and not reason:
                sma200 = spy_sma200.iloc[i]
                if not np.isnan(sma200) and spy_now > sma200:
                    risk_off = False
                    brake_log.append({"date": str(d.date()), "event": "risk_off_cleared",
                                      "reason": "SPY back above its 200-day average"})
            if not triggered:
                if not risk_off and spy_ret <= early_lim:
                    if not buys_paused:
                        brake_log.append({"date": str(d.date()), "event": "buys_paused",
                                          "reason": f"SPY {spy_ret:+.1%} today"})
                    buys_paused = True
                elif buys_paused:
                    buys_paused = False
                    brake_log.append({"date": str(d.date()), "event": "buys_resumed",
                                      "reason": "SPY recovered above the warning level"})
            days_risk_off += int(risk_off)
            days_buys_paused += int(buys_paused)

        # 3a. fill any orders decided `lag` bars ago, at today's prices
        if pending:
            due = [o for (fill_bar, o) in pending if fill_bar == i]
            pending = [(fill_bar, o) for (fill_bar, o) in pending if fill_bar > i]
            for o in due:
                execute(o, row)

        # 3b. decisions — exits weekly OR daily (fast_exits); buys weekly OR daily (fast_buys)
        is_rebalance = i in rebal_bars
        if is_rebalance or fast_exits or fast_buys:
            hist = close.iloc[: i + 1]
            prices = {s: hist[s].dropna().tolist() for s in universe}
            bench = hist[benchmark].dropna().tolist()
            decision = srsr.evaluate(prices, bench, cfg)
            buys, sells = srsr.actions(decision, list(positions), cfg)
            order = {
                "rebalance": is_rebalance,
                "do_exit": is_rebalance or fast_exits,
                "do_buy": (is_rebalance or fast_buys) and not (risk_off or buys_paused),
                "buys": buys, "sells": sells,
                "scores": decision.scores, "prices": prices,
            }
            if lag == 0:
                execute(order, row)
            else:
                pending.append((i + lag, order))

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
        "n_brake_flattened": n_brake_flattened,
        "pct_days_risk_off": days_risk_off / len(curve_equity),
        "pct_days_buys_paused": days_buys_paused / len(curve_equity),
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
        "brake_log": brake_log,
        "assumptions": {
            "cost_bps_per_side": round(cost * 10_000, 2),
            "cash_rate": ex.rate_source(exec_cfg.get("cash_rate")),
            "risk_free": ex.rate_source(exec_cfg.get("risk_free")),
            "timing": {
                "rebalance_day": rebalance_day, "lag_bars": lag,
                "daily_exits": bool(fast_exits), "daily_buys": bool(fast_buys),
                "safety_brakes": brakes is not None,
            },
        },
    }


def _timing_text(t: dict[str, Any] | None) -> str:
    if not t:
        return "original"
    parts = [
        f"rebalance {t['rebalance_day']}",
        "exits daily" if t["daily_exits"] else "exits weekly",
        f"fills {t['lag_bars']} bar(s) after the signal" if t["lag_bars"] else "fills at the signal close",
        "safety brakes on" if t["safety_brakes"] else "no safety brakes",
    ]
    return ", ".join(parts)


def _print_modes(results: list[tuple[str, dict[str, Any]]]) -> None:
    base = results[0][1]["strategy"]
    spy = results[0][1]["benchmark"]
    r0 = results[0][1]
    console.print(f"\n[bold]Original timing vs the live app's timing[/]  {r0['start']} -> {r0['end']}\n")
    t = Table(show_header=True, header_style="bold")
    for col in ("variant", "CAGR", "vs original", "Max DD", "Sharpe", "Trades", "Avg % cash", "End $"):
        t.add_column(col, justify="left" if col == "variant" else "right")
    for label, r in results:
        s = r["strategy"]
        t.add_row(label, f"{s['cagr']:+.1%}", f"{(s['cagr'] - base['cagr']) * 100:+.1f} pts",
                  f"{s['max_drawdown']:.1%}", f"{s['sharpe']:.2f}", str(s["n_trades"]),
                  f"{s['pct_cash']:.0%}", f"${r['final_equity']:,.0f}")
    t.add_row("SPY buy & hold", f"{spy['cagr']:+.1%}", "", f"{spy['max_drawdown']:.1%}",
              f"{spy['sharpe']:.2f}", "1", "0%", "")
    console.print(t)
    live = results[-1][1]
    if live.get("brake_log"):
        console.print(f"\n[dim]Safety brakes fired {len(live['brake_log'])} time(s) in the live-like run; "
                      f"first few: " + "; ".join(f"{e['date']} {e['event']}" for e in live["brake_log"][:5]) + "[/]")
    console.print(
        "\n[dim]Read it as: how much of the headline result depends on the backtest being "
        "friendlier than the live app. A small gap = the old numbers were fair; a big gap = "
        "re-judge past approvals. Daily bars only, so brakes react at the close, not intraday.[/]\n"
    )


def _print_report(r: dict[str, Any]) -> None:
    s, b, a = r["strategy"], r["benchmark"], r["assumptions"]
    console.print(
        f"\n[bold]SRSR backtest[/]  {r['start']} -> {r['end']}  "
        f"(start ${start_equity():,.0f} -> end ${r['final_equity']:,.0f})\n"
        f"[dim]net of {a['cost_bps_per_side']:g} bps/side costs · cash earns {a['cash_rate']} · "
        f"Sharpe vs {a['risk_free']}[/]\n"
        f"[dim]timing: {_timing_text(a.get('timing'))}[/]\n"
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
    parser.add_argument("--live-like", action="store_true",
                        help="mirror the live worker: Friday rebalance, daily exits, fill next bar, safety brakes")
    parser.add_argument("--rebalance-day", choices=REBALANCE_DAYS,
                        help="monday (default, first bar of the week) or friday (last bar of the week)")
    parser.add_argument("--lag", type=int, help="bars between signal and fill (default 0 = same close)")
    parser.add_argument("--daily-exits", action="store_true",
                        help="check rotation exits every day, not just at the weekly rebalance")
    parser.add_argument("--brakes", action="store_true",
                        help="turn on the live crash / drawdown / early-warning brakes (goal.yaml black_swan)")
    parser.add_argument("--compare-modes", action="store_true",
                        help="run original timing, each live difference alone, and all together; print a table")
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
    exec_cfg = ex.gross() if args.gross else None
    if args.compare_modes:
        results = compare_modes(cfg, args.years, exec_cfg,
                                progress=lambda label: console.print(f"[dim]running: {label} ...[/]"))
        _print_modes(results)
        return
    opts: dict[str, Any] = live_like_options() if args.live_like else {}
    if args.rebalance_day:
        opts["rebalance_day"] = args.rebalance_day
    if args.lag is not None:
        opts["lag"] = args.lag
    if args.daily_exits:
        opts["fast_exits"] = True
    if args.brakes:
        opts["brakes"] = brakes_from_goal()
    _print_report(run_backtest(cfg, args.years, exec_cfg, **opts))


if __name__ == "__main__":
    main()
