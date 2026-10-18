"""Offline tests for PR9: the minimum-trades check in the reflection, and the safer seed_demo.py.

Throwaway databases and files; no network, nothing under state/ is touched. Run:
    .venv\\Scripts\\python tests\\test_reflection_guard.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seed_demo  # noqa: E402
from hermes_trading import db, reflect  # noqa: E402

FAILS = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh_db(name):
    db.DB_FILE = Path(tempfile.mkdtemp()) / name   # throwaway: never touch state/trading.db
    db.init_db()
    return db.connect()


def add_trade(conn, status="closed", reason="rotation_exit", n=1):
    for i in range(n):
        conn.execute(
            "INSERT INTO trades(symbol, side, entry_ts, entry_price, size, stop_price, target_price, "
            "strategy_version, status, exit_ts, exit_price, exit_reason, pnl, pnl_pct) "
            "VALUES('XLK','long',?,100,1,85,1e8,'01',?,?,?,?,?,?)",
            (1000.0 + i, status, 2000.0 + i if status == "closed" else None,
             101.0 if status == "closed" else None, reason if status == "closed" else None,
             1.0 if status == "closed" else None, 0.01 if status == "closed" else None),
        )
    conn.commit()


def count(conn, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) AS c FROM trades WHERE {where}").fetchone()["c"]


# ---- 1. seed_demo on a fresh database ----------------------------------------------------------
print("1. seed_demo: fresh database")
conn = fresh_db("fresh.db")
ok(seed_demo.seed(conn) is True and count(conn, "exit_reason='demo_seed'") == 6, "seeds exactly 6 tagged demo trades")
ok(seed_demo.seed(conn) is False and count(conn) == 6, "running it again refuses and does not double them")
seed_demo.clear(conn)
ok(count(conn) == 0, "--clear removes them all")
ok(seed_demo.seed(conn) is True and count(conn) == 6, "and a fresh seed works again after clearing")
conn.close()

# ---- 2. guards ------------------------------------------------------------------------------
print("2. seed_demo: will not mix fake trades into a real account")
conn = fresh_db("real.db")
add_trade(conn, "closed", "rotation_exit", n=2)
ok(seed_demo.seed(conn) is False and count(conn, "exit_reason='demo_seed'") == 0, "refuses when real closed trades exist")
ok(seed_demo.seed(conn, force=True) is True and count(conn, "exit_reason='demo_seed'") == 6, "--force overrides")
seed_demo.clear(conn)
ok(count(conn) == 2 and count(conn, "exit_reason='rotation_exit'") == 2, "--clear removes only the demo rows, real trades are untouched")
conn.close()

conn = fresh_db("open.db")
add_trade(conn, "open")
ok(seed_demo.seed(conn) is False and count(conn, "exit_reason='demo_seed'") == 0, "refuses when a position is open")
conn.close()

# ---- 3. clear resets the reflection counter -------------------------------------------------
print("3. --clear puts the reflection counter back")
conn = fresh_db("counter.db")
add_trade(conn, "closed", "rotation_exit", n=3)
seed_demo.seed(conn, force=True)                      # 3 real + 6 demo = 9 closed
db.set_meta(conn, "last_reflection_count", "9")       # a reflection ran while the demo rows were there
conn.commit()
seed_demo.clear(conn)
ok(db.get_meta(conn, "last_reflection_count") == "3", "counter drops from 9 to the real count (3)")
db.set_meta(conn, "last_reflection_count", "1")
conn.commit()
seed_demo.clear(conn)
ok(db.get_meta(conn, "last_reflection_count") == "1", "a counter already at or below the real count is left alone")
conn.close()

# ---- 4. the minimum-trades check in the reflection ----------------------------------------------
print("4. reflection holds until enough trades have closed")
reflect.HYPOTHESES_FILE = Path(tempfile.mkdtemp()) / "hypotheses.jsonl"   # keep the audit log out of state/
reflect.score = lambda *a, **k: {"composite": 0.0, "realised_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}


BASE_GOAL = {"asset": "US sector ETFs", "target_return_30d": 0.015, "max_drawdown": 0.15, "min_sharpe": 1.0,
             "failure_below": -0.10, "reflection_every": 8, "one_variable_only": True, "black_swan": {}}


def reflect_with(goal, n_trades):
    c = fresh_db("reflect.db")
    add_trade(c, "closed", "rotation_exit", n=n_trades)
    c.close()
    full = dict(BASE_GOAL)
    full.update(goal)
    reflect.load_goal = lambda: dict(full)
    return reflect.propose_once("fallback")


ok(reflect.MIN_SAMPLE_TRADES == 5, "the built-in minimum is 5 (it was 0)")
r = reflect_with({}, 0)
ok(r and r.get("hold") and "too few" in r["rationale"], "no trades -> holds (this used to propose a change from nothing)")
r = reflect_with({}, 4)
ok(r and r.get("hold") and "need 5" in r["rationale"], "4 trades -> still holds, and says how many are needed")
r = reflect_with({}, 5)
ok(not (r and r.get("hold") and "too few" in r.get("rationale", "")), "5 trades -> no longer held for lack of data")
r = reflect_with({"min_sample_trades": 10}, 7)
ok(r and r.get("hold") and "need 10" in r["rationale"], "goal.yaml min_sample_trades can raise the bar")
r = reflect_with({"min_sample_trades": 0}, 0)
ok(not (r and r.get("hold") and "too few" in r.get("rationale", "")), "min_sample_trades: 0 switches the check off (old behaviour stays available)")

print("5. the example config documents the setting")
import yaml  # noqa: E402
ex = yaml.safe_load((ROOT / "config" / "goal.example.yaml").read_text(encoding="utf-8"))
ok(ex.get("min_sample_trades") == reflect.MIN_SAMPLE_TRADES, "goal.example.yaml has min_sample_trades matching the built-in default")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
