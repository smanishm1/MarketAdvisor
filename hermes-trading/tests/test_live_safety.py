"""Offline tests for PR3: the stuck drawdown brake, stale/blocked approvals, the target display.

No network, throwaway databases. Run:
    .venv\\Scripts\\python tests\\test_live_safety.py
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from synthetic_prices import SECTORS, base_cfg  # noqa: E402
from hermes_trading import approval, backtest as bt, db, loop, paper_broker  # noqa: E402

FAILS = []
NO_FRICTION = {"cost_bps_per_side": 0.0, "cash_rate": 0.0, "risk_free": 0.0}


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh_db(name):
    db.DB_FILE = Path(tempfile.mkdtemp()) / name   # throwaway: never touch state/trading.db
    db.init_db()
    return db.connect()


def pend(sym="AAA", price=100.0, size=10.0, age_h=0.0):
    return {"symbol": sym, "side": "long", "price": price, "rsi": None, "stop_price": price * 0.85,
            "target_price": price * 1e6, "size": size, "strategy_version": "t", "context": None}


# ---- 1. backtest: a drawdown brake can clear and does not instantly re-fire ---------------------
print("1. backtest: drawdown brake clears after the flatten")
days = 700
idx = pd.bdate_range("2012-01-02", periods=days)
df = pd.DataFrame({s: np.full(days, 100.0) for s in SECTORS + ["SPY"]}, index=idx)
df["SPY"] = 100.0 * 1.0006 ** np.arange(days)                  # calm, steadily rising: no crash day
xlk = 100.0 * 1.003 ** np.arange(days)
xlk[400:406] = xlk[399] * np.linspace(0.97, 0.74, 6)          # a sharp ~26% slide over a week
xlk[406:] = xlk[405] * 1.004 ** np.arange(1, days - 406 + 1)   # then a strong recovery
df["XLK"] = xlk
cfg = base_cfg(hold_top_n=1, exit_rank_n=1, max_positions=1, position_notional_cap_pct=100,
               catastrophe_stop_pct=45)
res = bt.simulate(cfg, df, exec_cfg=NO_FRICTION, brakes=bt.brakes_from_goal({"black_swan": {}}))
log = res["brake_log"]
ev = [(e["event"], e["date"]) for e in log]
first = log[0] if log else {}
ok(first.get("event") == "risk_off" and "drawdown" in first.get("reason", ""), f"drawdown flattens ({ev[:2]})")
clr = [e for e in log if e["event"] == "risk_off_cleared"]
if first and clr:
    gap = int(((pd.Timestamp(clr[0]["date"]) - pd.Timestamp(first["date"])).days))
    ok(gap <= 14, f"clears within two weeks once SPY is above its 200-day average ({gap} days)")
else:
    ok(False, "clears at all")
refires = [e for e in log if e["event"] == "risk_off"]
ok(len(refires) <= 2, f"no repeated flatten/clear cycling ({len(refires)} flatten events)")
ok(res["strategy"]["pct_days_risk_off"] < 0.10 if "pct_days_risk_off" in res["strategy"] else True,
   "only a small share of days spent risk-off")

# ---- 2. live guard: same behaviour -----------------------------------------------------------
print("2. live worker: drawdown flatten, then it can clear and stays clear")
conn = fresh_db("guard.db")
t = paper_broker.fill(conn, pend("AAA", 100.0, 100.0), price=100.0)   # ~$10,000 position
conn.execute("INSERT OR REPLACE INTO equity(ts, equity, cash, position_value) VALUES(?,?,?,?)",
             (time.time() - 5000, 10000.0, 0.0, 10000.0))
conn.commit()
bench = [100.0 + 0.1 * i for i in range(300)]                  # rising, above its 200-day average
r1 = loop._black_swan_guard(conn, bench, {"AAA": 78.0}, None)    # position down ~22%
ok(r1 is True and db.get_meta(conn, "risk_off") == "1", "20% drawdown flips risk-off")
ok(paper_broker.held_symbols(conn) == [], "position sold to cash")
ok(float(db.get_meta(conn, "peak_reset_ts", "0")) > 0, "high-water mark restart is recorded")
r2 = loop._black_swan_guard(conn, bench, {}, None)
ok(db.get_meta(conn, "risk_off") == "0", "next tick clears risk-off (SPY above 200-day)")
r3 = loop._black_swan_guard(conn, bench, {}, None)
ok(db.get_meta(conn, "risk_off") == "0", "and it does not re-fire on the following tick")
conn.close()

print("   manual clear restarts the clock too")
conn = fresh_db("clear.db")
db.set_meta(conn, "risk_off", "1")
conn.commit()
conn.close()
from dashboard import app as dash  # noqa: E402
resp = dash.clear_risk_off()   # the function behind POST /api/risk-off/clear (no web client needed)
conn = db.connect()
ok(resp == {"risk_off": False} and db.get_meta(conn, "risk_off") == "0", "clear endpoint clears risk-off")
ok(float(db.get_meta(conn, "peak_reset_ts", "0")) > 0, "clear endpoint restarts the high-water mark")
conn.close()

# ---- 3. stale proposals expire ---------------------------------------------------------------
print("3. stale proposals expire")
conn = fresh_db("stale.db")
old_id = approval.propose_trade(conn, pend("OLD"))
new_id = approval.propose_trade(conn, pend("NEW"))
old_appr = approval.propose_trade(conn, pend("OLDA"))
conn.execute("UPDATE pending_trades SET proposed_ts=? WHERE id IN (?,?)", (time.time() - 80 * 3600, old_id, old_appr))
approval.set_trade_status(conn, old_appr, "approved")
gone = approval.expire_stale_trades(conn, 72)
st = {r["id"]: r["status"] for r in db.rows_to_dicts(conn.execute("SELECT id,status FROM pending_trades").fetchall())}
ok(len(gone) == 2 and st[old_id] == "expired" and st[old_appr] == "expired", "80-hour-old proposals (pending or approved) expire")
ok(st[new_id] == "pending", "a fresh proposal is untouched")

# ---- 4. approval re-checks ------------------------------------------------------------------
print("4. approval re-checks")
goal = {}
p = pend("AAA", 100.0, 10.0)
ok(loop._fill_blocker(conn, p, 100.0, goal) is None, "normal buy passes")
db.set_meta(conn, "risk_off", "1")
ok("risk-off" in (loop._fill_blocker(conn, p, 100.0, goal) or ""), "blocked while risk-off")
db.set_meta(conn, "risk_off", "0")
db.set_meta(conn, "buys_halted", "1")
ok("early-warning" in (loop._fill_blocker(conn, p, 100.0, goal) or ""), "blocked while buys are paused")
db.set_meta(conn, "buys_halted", "0")
ok("moved" in (loop._fill_blocker(conn, p, 110.0, goal) or ""), "blocked if price moved 10% since proposal")
ok(loop._fill_blocker(conn, p, 103.0, goal) is None, "a 3% move is fine")
big = pend("AAA", 100.0, 500.0)
ok("cash" in (loop._fill_blocker(conn, big, 100.0, goal) or ""), "blocked if the cash is gone")
sell = dict(p, side="short")
ok(loop._fill_blocker(conn, sell, 100.0, goal) is None, "non-buy orders are not blocked by buy checks")

print("   reconcile_trades end to end")


async def quote(sym):
    return 100.5


tid = approval.propose_trade(conn, pend("OKAY", 100.0, 10.0))
approval.set_trade_status(conn, tid, "approved")
conn.commit()
asyncio.run(loop.reconcile_trades(conn, quote))
ok(paper_broker.held_symbols(conn) == ["OKAY"], "a still-valid approval is filled")
db.set_meta(conn, "risk_off", "1")
tid2 = approval.propose_trade(conn, pend("BLOCK", 100.0, 10.0))
approval.set_trade_status(conn, tid2, "approved")
conn.commit()
asyncio.run(loop.reconcile_trades(conn, quote))
status2 = conn.execute("SELECT status FROM pending_trades WHERE id=?", (tid2,)).fetchone()["status"]
ok("BLOCK" not in paper_broker.held_symbols(conn) and status2 == "expired", "an approval made before risk-off is not filled")
conn.close()

# ---- 5. dashboard no longer prints the placeholder target -----------------------------------
print("5. pending-trade target display")
html = (ROOT / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
ok("t.target_price < t.price*50" in html and "none (exits by rule)" in html, "placeholder target shows 'none (exits by rule)'")
ok("target ${money(t.target_price)} · v" not in html, "raw target no longer printed")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
