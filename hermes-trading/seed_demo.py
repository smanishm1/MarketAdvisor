"""Seed demo *closed* trades so the reflection has something to react to.

The reflection holds (proposes nothing) until there are at least 5 closed trades
(MIN_SAMPLE_TRADES; override with `min_sample_trades:` in goal.yaml). This inserts a handful
of small losing closed trades — which pushes realised return below target — so clicking
**Reflect now** produces a real strategy-change proposal (a card in "Pending strategy changes"
+ a green toast).

    .venv/Scripts/python seed_demo.py          # insert demo closed trades
    .venv/Scripts/python seed_demo.py --clear  # remove them again

All rows are tagged exit_reason='demo_seed' so --clear removes exactly these and
nothing real. Paper-only; touches only state/trading.db.

WARNING: this writes into the SAME database the app uses. Until you --clear them, the fake
trades count in profit, equity, the score and the drift report. So it refuses to run:
  * if demo trades are already there (it would double them), or
  * if the database already holds real closed trades or open positions
    (pass --force only if you really mean it, and run --clear afterwards).
"""
from __future__ import annotations

import argparse
import sys

from hermes_trading import db
from hermes_trading.config import load_strategy

TAG = "demo_seed"  # marker so these rows are easy to identify and remove


def _count(conn, where: str, params: tuple = ()) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM trades WHERE {where}", params).fetchone()["c"])


def clear(conn) -> None:
    n = conn.execute("DELETE FROM trades WHERE exit_reason=?", (TAG,)).rowcount
    # The auto-reflection trigger counts closed trades since the last reflection. If a reflection
    # ran while the demo rows were there, that counter now sits above the real count and would
    # delay the next one. Pull it back down so removing the demo data leaves no trace.
    closed = _count(conn, "status='closed'")
    last = int(db.get_meta(conn, "last_reflection_count", "0") or "0")
    if last > closed:
        db.set_meta(conn, "last_reflection_count", str(closed))
    conn.commit()
    print(f"removed {n} demo trade(s)")


def seed(conn, force: bool = False) -> bool:
    """Insert the demo trades. Returns False (and changes nothing) if it refuses."""
    already = _count(conn, "exit_reason=?", (TAG,))
    if already:
        print(f"{already} demo trade(s) are already in the database - not adding more.")
        print("Run with --clear first if you want a fresh set.")
        return False
    real_closed = _count(conn, "status='closed' AND (exit_reason IS NULL OR exit_reason<>?)", (TAG,))
    open_now = _count(conn, "status='open'")
    if (real_closed or open_now) and not force:
        print(f"REFUSING: this database already has {real_closed} real closed trade(s) and "
              f"{open_now} open position(s).")
        print("Demo trades would be mixed into your real profit, equity and score.")
        print("Use a fresh install for demos, or add --force and then run --clear straight after.")
        return False

    strat = load_strategy()
    ver = str(strat.get("version", "01"))
    universe = strat.get("universe") or ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY"]
    now = db.now()
    day = 86_400.0

    rows = []
    for i in range(6):  # 6 > MIN_SAMPLE_TRADES (5)
        sym = universe[i % len(universe)]
        entry, exitp, size = 100.0, 98.0, 10.0          # -2% each, small loser
        pnl = (exitp - entry) * size                     # -20.0
        pnl_pct = (exitp - entry) / entry                # -0.02
        entry_ts = now - (12 - i) * day
        exit_ts = now - (6 - i) * day
        rows.append(
            (sym, "buy", entry_ts, entry, size, 95.0, 110.0, ver,
             "closed", exit_ts, exitp, TAG, pnl, pnl_pct)
        )

    conn.executemany(
        "INSERT INTO trades(symbol, side, entry_ts, entry_price, size, stop_price, "
        "target_price, strategy_version, status, exit_ts, exit_price, exit_reason, "
        "pnl, pnl_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    print(f"seeded {len(rows)} demo closed trades (tag={TAG!r}), about -$120 in total")
    print("Now: open the dashboard, set Brain = 'fallback', click 'Reflect now'.")
    print("These are FAKE trades in the real database: run with --clear when you're done.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="seed/clear demo closed trades")
    ap.add_argument("--clear", action="store_true", help="remove the demo trades")
    ap.add_argument("--force", action="store_true",
                    help="seed even though the database already has real trades (then run --clear)")
    args = ap.parse_args()
    db.init_db()
    conn = db.connect()
    try:
        if args.clear:
            clear(conn)
            return 0
        return 0 if seed(conn, force=args.force) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
