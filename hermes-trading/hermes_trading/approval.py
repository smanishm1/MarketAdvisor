"""The human-approval gate.

Worker proposes; the dashboard (a human) approves or rejects. The worker
reconciles approved rows on its next tick — it is the only writer that fills
trades or applies strategy changes.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db

# ---- trade proposals -------------------------------------------------------


def has_pending_or_open(conn: sqlite3.Connection, symbol: str) -> bool:
    """True if there is already a pending/approved proposal or an open position
    for `symbol` — so we never stack duplicate entries while one awaits a click."""
    p = conn.execute(
        "SELECT 1 FROM pending_trades WHERE symbol=? AND status IN "
        "('pending','approved') LIMIT 1",
        (symbol,),
    ).fetchone()
    if p:
        return True
    o = conn.execute(
        "SELECT 1 FROM trades WHERE symbol=? AND status='open' LIMIT 1", (symbol,)
    ).fetchone()
    return o is not None


def propose_trade(conn: sqlite3.Connection, trade: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO pending_trades(symbol, side, proposed_ts, price, rsi, "
        "stop_price, target_price, size, strategy_version, status, context) "
        "VALUES(?,?,?,?,?,?,?,?,?, 'pending', ?)",
        (
            trade["symbol"],
            trade["side"],
            db.now(),
            trade["price"],
            trade.get("rsi"),
            trade["stop_price"],
            trade["target_price"],
            trade["size"],
            trade["strategy_version"],
            trade.get("context"),
        ),
    )
    return int(cur.lastrowid)


def list_pending_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pending_trades WHERE status='pending' ORDER BY proposed_ts"
    ).fetchall()
    return db.rows_to_dicts(rows)


def list_approved_unfilled(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pending_trades WHERE status='approved' ORDER BY proposed_ts"
    ).fetchall()
    return db.rows_to_dicts(rows)


def set_trade_status(conn: sqlite3.Connection, pending_id: int, status: str) -> None:
    conn.execute(
        "UPDATE pending_trades SET status=?, resolved_ts=? WHERE id=?",
        (status, db.now(), pending_id),
    )


# ---- strategy proposals ----------------------------------------------------


def propose_strategy(conn: sqlite3.Connection, prop: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO pending_strategy(proposed_ts, source, from_version, "
        "to_version, variable, old_value, new_value, rationale, proposed_yaml, "
        "status) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
        (
            db.now(),
            prop["source"],
            prop["from_version"],
            prop["to_version"],
            prop["variable"],
            str(prop.get("old_value")),
            str(prop.get("new_value")),
            prop.get("rationale", ""),
            prop["proposed_yaml"],
        ),
    )
    return int(cur.lastrowid)


def list_pending_strategy(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pending_strategy WHERE status='pending' ORDER BY proposed_ts"
    ).fetchall()
    return db.rows_to_dicts(rows)


def list_approved_strategy(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pending_strategy WHERE status='approved' ORDER BY proposed_ts"
    ).fetchall()
    return db.rows_to_dicts(rows)


def set_strategy_status(conn: sqlite3.Connection, prop_id: int, status: str) -> None:
    conn.execute(
        "UPDATE pending_strategy SET status=?, resolved_ts=? WHERE id=?",
        (status, db.now(), prop_id),
    )
