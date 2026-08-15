"""Seed demo *closed* trades so the reflection has something to react to.

The reflection holds (proposes nothing) until there are at least 5 closed trades
(MIN_SAMPLE_TRADES). This inserts a handful of small losing closed trades — which
pushes realised return below target — so clicking **Reflect now** produces a real
strategy-change proposal (a card in "Pending strategy changes" + a green toast).

    .venv/Scripts/python seed_demo.py          # insert demo closed trades
    .venv/Scripts/python seed_demo.py --clear  # remove them again

All rows are tagged exit_reason='demo_seed' so --clear removes exactly these and
nothing real. Paper-only; touches only state/trading.db.
"""
from __future__ import annotations

import argparse

from hermes_trading import db
from hermes_trading.config import load_strategy

TAG = "demo_seed"  # marker so these rows are easy to identify and remove


def clear(conn) -> None:
    n = conn.execute("DELETE FROM trades WHERE exit_reason=?", (TAG,)).rowcount
    conn.commit()
    print(f"removed {n} demo trade(s)")


def seed(conn) -> None:
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
    print(f"seeded {len(rows)} demo closed trades (tag={TAG!r})")
    print("Now: open the dashboard, set Brain = 'fallback', click 'Reflect now'.")
    print("Run with --clear to remove them when you're done.")


def main() -> None:
    ap = argparse.ArgumentParser(description="seed/clear demo closed trades")
    ap.add_argument("--clear", action="store_true", help="remove the demo trades")
    args = ap.parse_args()
    db.init_db()
    conn = db.connect()
    try:
        clear(conn) if args.clear else seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
