"""Offline tests for the backtest timing options (see docs/BACKTEST-PARITY.md).

No network: fake prices, fixed (non-T-bill) rates. Run:
    .venv\\Scripts\\python tests\\test_backtest_parity.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from synthetic_prices import EXEC, SECTORS, base_cfg, synthetic_close  # noqa: E402
from hermes_trading import backtest as bt  # noqa: E402

FAILS = []
NO_FRICTION = {"cost_bps_per_side": 0.0, "cash_rate": 0.0, "risk_free": 0.0}


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def close_to(a, b, tol=1e-3):
    return abs(a - b) <= tol * max(1.0, abs(b))


def scenario(days, xlk=None, spy=None):
    """Flat 100 everywhere except XLK / SPY paths you pass in (arrays of length `days`)."""
    idx = pd.bdate_range("2012-01-02", periods=days)
    df = pd.DataFrame({s: np.full(days, 100.0) for s in SECTORS + ["SPY"]}, index=idx)
    if xlk is not None:
        df["XLK"] = xlk
    if spy is not None:
        df["SPY"] = spy
    return df


# ---- 1. default options reproduce the ORIGINAL backtest, number for number ---------------------
print("1. defaults reproduce the original backtest (numbers computed with the pre-change code)")
close = synthetic_close()
GOLDEN = {
    "A equal weight": (base_cfg(), {}, dict(final=19860.2101, trades=91, stops=0, trims=0, costs=394.57, dd=-0.128802, sharpe=0.9555)),
    "B rank+trailing+trim": (base_cfg(sizing="rank_weight", trailing_stop=True, trim_to_cap=True, trim_band_pct=5), {},
                             dict(final=16938.8902, trades=91, stops=0, trims=3, costs=266.85, dd=-0.075336, sharpe=0.826572)),
    "C daily exits (old flag)": (base_cfg(), {"fast_exits": True},
                                 dict(final=16539.2193, trades=117, stops=0, trims=0, costs=457.57, dd=-0.120001, sharpe=0.676032)),
    "D tight stop": (base_cfg(catastrophe_stop_pct=4), {},
                     dict(final=18642.4615, trades=105, stops=26, costs=445.27, dd=-0.132171, sharpe=0.854866)),
}
for name, (cfg, kw, g) in GOLDEN.items():
    r = bt.simulate(cfg, close, exec_cfg=EXEC, **kw)
    s = r["strategy"]
    ok(close_to(r["final_equity"], g["final"]), f"{name}: final equity {r['final_equity']:.2f} == {g['final']}")
    ok(s["n_trades"] == g["trades"] and s["n_stop_exits"] == g["stops"], f"{name}: {s['n_trades']} trades, {s['n_stop_exits']} stop exits")
    ok(close_to(s["costs_paid"], g["costs"]) and close_to(s["max_drawdown"], g["dd"], 1e-4)
       and close_to(s["sharpe"], g["sharpe"], 1e-4), f"{name}: costs, max drawdown, Sharpe unchanged")
ok(bt.simulate(base_cfg(), close, exec_cfg=EXEC)["brake_log"] == [], "no brakes by default -> empty brake log")

# ---- 2. which bars count as the weekly rebalance ----------------------------------------------
print("2. rebalance day")
dates = pd.bdate_range("2012-01-02", periods=900)
mon = sorted(bt.rebalance_bars(dates, 205, 900, "monday"))
fri = sorted(bt.rebalance_bars(dates, 205, 900, "friday"))
ok(all(dates[i].weekday() == 0 for i in mon[1:]), "monday mode: every rebalance (after the first bar) is a Monday")
ok(all(dates[i].weekday() == 4 for i in fri[1:]), "friday mode: every rebalance (after the first bar) is a Friday")
ok(mon[0] == 205 and fri[0] == 205, "the first bar is a rebalance in both modes (the live worker does that too)")
ok(abs(len(mon) - len(fri)) <= 1 and len(mon) > 100, f"about one rebalance a week in both ({len(mon)} / {len(fri)})")
try:
    bt.rebalance_bars(dates, 205, 900, "tuesday")
    ok(False, "unknown rebalance day should be refused")
except ValueError:
    ok(True, "unknown rebalance day is refused")
try:
    bt.simulate(base_cfg(), close, exec_cfg=EXEC, lag=-1)
    ok(False, "negative lag should be refused")
except ValueError:
    ok(True, "negative lag is refused")

# ---- 3. fill timing: lag=1 buys at the NEXT bar's close ---------------------------------------
print("3. fill next bar")
days = 420
xlk = 100.0 * 1.001 ** np.arange(days)             # the only name that qualifies
sc = scenario(days, xlk=xlk)
first = 205                                          # warm-up bars for a 200-day trend filter
r0 = bt.simulate(base_cfg(), sc, exec_cfg=NO_FRICTION)["final_equity"]
r1 = bt.simulate(base_cfg(), sc, exec_cfg=NO_FRICTION, lag=1)["final_equity"]
shares0 = (10000 / 3) / xlk[first]
shares1 = (10000 / 3) / xlk[first + 1]
exp0 = 10000 - shares0 * xlk[first] + shares0 * xlk[-1]
exp1 = 10000 - shares1 * xlk[first + 1] + shares1 * xlk[-1]
ok(close_to(r0, exp0, 1e-6), f"lag 0 buys at the signal bar's close (final {r0:.2f} == {exp0:.2f})")
ok(close_to(r1, exp1, 1e-6), f"lag 1 buys at the next bar's close (final {r1:.2f} == {exp1:.2f})")
ok(r1 < r0, "in a steadily rising market, filling a day late earns slightly less")

# ---- 4. daily exits leave the day the signal breaks, not at the next rebalance ----------------
print("4. daily exits")
t = np.arange(days)
xlk2 = 100.0 * 1.001 ** t
drop_start = 302                                     # then it falls 1.5% a day; the signal breaks on a
                                                     # Wednesday, so the weekly run must wait until Monday
xlk2[drop_start:] = xlk2[drop_start - 1] * 0.985 ** np.arange(1, days - drop_start + 1)
sc2 = scenario(days, xlk=xlk2)
cfg_nostop = base_cfg(catastrophe_stop_pct=99)       # isolate the rotation exit from the stop
weekly = bt.simulate(cfg_nostop, sc2, exec_cfg=NO_FRICTION)
daily = bt.simulate(cfg_nostop, sc2, exec_cfg=NO_FRICTION, fast_exits=True)
ok(weekly["strategy"]["n_trades"] == daily["strategy"]["n_trades"] == 1, "one entry in both runs")
ok(daily["final_equity"] > weekly["final_equity"],
   f"daily exits get out earlier in a falling market ({daily['final_equity']:.0f} > {weekly['final_equity']:.0f})")

# ---- 5. safety brakes ---------------------------------------------------------------------------
print("5. safety brakes")
days = 700
crash = 400
spy = np.full(days, 100.0)
spy[crash:] = 91.0 * 1.002 ** np.arange(days - crash)   # -9% day, then a slow recovery
xlk3 = 100.0 * 1.001 ** np.arange(days)
xlk3[crash:] = xlk3[crash - 1] * 0.90 * 1.001 ** np.arange(days - crash)
sc3 = scenario(days, xlk=xlk3, spy=spy)
brakes = bt.brakes_from_goal({"black_swan": {}})
on = bt.simulate(base_cfg(), sc3, exec_cfg=NO_FRICTION, brakes=brakes)
off = bt.simulate(base_cfg(), sc3, exec_cfg=NO_FRICTION)
log = on["brake_log"]
crash_date = str(sc3.index[crash].date())
ok(bool(log) and log[0]["event"] == "risk_off" and log[0]["date"] == crash_date,
   f"a -9% SPY day flips to risk-off on that day ({log[0]['date'] if log else 'no event'})")
ok(log and log[0]["positions_flattened"] == 1 and on["strategy"]["n_brake_flattened"] == 1, "the open position is sold to cash")
ok(off["strategy"]["n_brake_flattened"] == 0 and off["brake_log"] == [], "without brakes nothing is flattened")
cleared = [e for e in log if e["event"] == "risk_off_cleared"]
ok(bool(cleared), "risk-off clears once SPY is back above its 200-day average")
if log and cleared:
    curve = pd.Series(dict(on["equity_curve"]))
    between = curve[(curve.index > crash_date) & (curve.index < cleared[0]["date"])]
    ok(len(between) > 5 and between.nunique() == 1,
       f"no buying while risk-off: equity is flat in cash for {len(between)} bars")
ok(on["strategy"]["pct_days_risk_off"] > 0.05, "risk-off days are counted")

spy2 = np.full(days, 100.0)
spy2[500:] = 94.5                                      # a -5.5% day: early warning, not a crash
sc4 = scenario(days, xlk=100.0 * 1.001 ** np.arange(days), spy=spy2)
early = bt.simulate(base_cfg(), sc4, exec_cfg=NO_FRICTION, brakes=brakes)
ev = [e["event"] for e in early["brake_log"]]
ok(ev == ["buys_paused", "buys_resumed"], f"a -5.5% day only pauses buys for a day ({ev})")
ok(early["strategy"]["n_brake_flattened"] == 0, "an early-warning day sells nothing")

# ---- 6. the "live-like" bundle ------------------------------------------------------------------
print("6. live-like bundle")
live = bt.live_like_options({"black_swan": {"crash_day_pct": -0.06}})
ok(live["rebalance_day"] == "friday" and live["lag"] == 1 and live["fast_exits"] is True, "Friday, 1-bar lag, daily exits")
ok(live["brakes"]["crash_day_pct"] == -0.06 and live["brakes"]["early_warn_day_pct"] == -0.05
   and live["brakes"]["max_portfolio_drawdown_pct"] == 0.20 and live["brakes"]["flatten_on_trigger"] is True,
   "brake limits come from goal.yaml with the live app's defaults")
r = bt.simulate(base_cfg(), close, exec_cfg=EXEC, **live)
ok(r["assumptions"]["timing"] == {"rebalance_day": "friday", "lag_bars": 1, "daily_exits": True,
                                  "daily_buys": False, "safety_brakes": True}, "the result says which timing it used")

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
