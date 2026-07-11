"""Shadow-SPY comparison: the strategy vs "the same dollars in SPY, same windows".

Fairness rule (agreed): SPY only competes while a position is actually open.
Every fill snapshots SPY (`trades.spy_entry`), every close snapshots it again
(`trades.spy_exit`); an open trade's shadow marks against the current SPY quote.
Cash periods accrue nothing on either side — this isolates *selection skill*,
deliberately excluding the cash-allocation effect (the backtester's SPY
buy-and-hold column remains the harsher whole-strategy benchmark).

Honest limits: snapshots are price-only quotes (no dividend adjustment), which
understates SPY by roughly 0.1%/month of holding — negligible per trade.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db


def summary(
    conn: sqlite3.Connection,
    spy_now: float | None,
    prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Aggregate + per-position comparison. `prices` marks open positions
    (symbol -> latest price, defaulting to entry = flat); `spy_now` marks their
    shadows — without it the open side is listed but excluded from totals."""
    prices = prices or {}

    def _rows(status: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM trades WHERE status=? AND spy_entry IS NOT NULL "
            "AND spy_entry > 0 ORDER BY id",
            (status,),
        ).fetchall()

    strat_pnl = spy_pnl = notional = 0.0
    n_beat = 0
    closed_rows: list[dict[str, Any]] = []
    for t in _rows("closed"):
        if not t["spy_exit"]:
            continue
        cost = t["size"] * t["entry_price"]
        spy_ret = t["spy_exit"] / t["spy_entry"] - 1.0
        pnl_pct = t["pnl_pct"] or 0.0
        notional += cost
        strat_pnl += t["pnl"] or 0.0
        spy_pnl += cost * spy_ret
        if pnl_pct > spy_ret:
            n_beat += 1
        closed_rows.append({
            "symbol": t["symbol"],
            "exit_ts": t["exit_ts"],
            "exit_reason": t["exit_reason"],
            "pnl_pct": round(pnl_pct, 4),
            "spy_pct": round(spy_ret, 4),
            "alpha_pct": round(pnl_pct - spy_ret, 4),
        })

    open_rows: list[dict[str, Any]] = []
    open_strat = open_spy = open_notional = 0.0
    for t in _rows("open"):
        cost = t["size"] * t["entry_price"]
        px = prices.get(t["symbol"], t["entry_price"])
        pnl_pct = px / t["entry_price"] - 1.0 if t["entry_price"] else 0.0
        spy_ret = (spy_now / t["spy_entry"] - 1.0) if spy_now else None
        if spy_ret is not None:
            open_notional += cost
            open_strat += cost * pnl_pct
            open_spy += cost * spy_ret
        open_rows.append({
            "symbol": t["symbol"],
            "entry_ts": t["entry_ts"],
            "pnl_pct": round(pnl_pct, 4),
            "spy_pct": round(spy_ret, 4) if spy_ret is not None else None,
            "alpha_pct": round(pnl_pct - spy_ret, 4) if spy_ret is not None else None,
        })

    skipped = conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE spy_entry IS NULL "
        "OR (status='closed' AND spy_exit IS NULL)"
    ).fetchone()["c"]

    total_notional = notional + open_notional
    total_strat = strat_pnl + open_strat
    total_spy = spy_pnl + open_spy
    pct = lambda pnl, base: round(pnl / base, 4) if base else 0.0  # noqa: E731
    return {
        "closed": {
            "n": len(closed_rows),
            "n_beat": n_beat,
            "strategy_pnl": round(strat_pnl, 2),
            "spy_pnl": round(spy_pnl, 2),
            "strategy_pct": pct(strat_pnl, notional),
            "spy_pct": pct(spy_pnl, notional),
            "rows": closed_rows[-10:],
        },
        "open": {
            "strategy_pnl": round(open_strat, 2),
            "spy_pnl": round(open_spy, 2),
            "rows": open_rows,
        },
        "total": {
            "strategy_pnl": round(total_strat, 2),
            "spy_pnl": round(total_spy, 2),
            "strategy_pct": pct(total_strat, total_notional),
            "spy_pct": pct(total_spy, total_notional),
            "alpha_pct": pct(total_strat, total_notional) - pct(total_spy, total_notional),
        },
        "skipped": skipped,
    }


def backfill(conn: sqlite3.Connection | None = None) -> int:
    """Fill missing SPY snapshots on legacy trades from daily adjusted closes.

    Idempotent (only touches NULL columns). Uses the close on/just before each
    trade's entry/exit date — intraday precision is lost for these old rows,
    acceptable for a one-time backfill. Returns the number of trades updated.
    """
    import pandas as pd

    from .adapters import equities

    own = conn is None
    if own:
        db.init_db()
        conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, entry_ts, exit_ts, status, spy_entry, spy_exit FROM trades "
            "WHERE spy_entry IS NULL OR (status='closed' AND spy_exit IS NULL)"
        ).fetchall()
        if not rows:
            return 0
        spy = equities.fetch_history(["SPY"], period="2y")["SPY"].dropna()
        updated = 0
        for t in rows:
            # int(): whole seconds — fractional epochs can't convert losslessly
            # to the index's datetime resolution
            entry = t["spy_entry"] or spy.asof(pd.Timestamp(int(t["entry_ts"]), unit="s"))
            exit_ = t["spy_exit"]
            if t["status"] == "closed" and not exit_ and t["exit_ts"]:
                exit_ = spy.asof(pd.Timestamp(int(t["exit_ts"]), unit="s"))
            conn.execute(
                "UPDATE trades SET spy_entry=?, spy_exit=? WHERE id=?",
                (
                    float(entry) if pd.notna(entry) else None,
                    float(exit_) if exit_ is not None and pd.notna(exit_) else None,
                    t["id"],
                ),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"backfilled {backfill()} trade(s)")
