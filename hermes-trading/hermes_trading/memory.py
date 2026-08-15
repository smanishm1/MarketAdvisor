"""Strategy memory: context that persists from trade to trade and version to version.

Three sources, all already on disk, pulled together in one place so the reflection
brain, the morning brief, and the dashboard journal all see the same history:

  - trades.context        — WHY each position was opened (rank, score, prior exit
                            of the same symbol), written at proposal time and
                            carried onto the filled trade.
  - pending_strategy      — the lineage of applied/rejected strategy changes.
  - hypotheses.jsonl      — every reflection decision, including holds.

Read-only over the DB; safe to call from the worker, the dashboard, or reflect.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .paths import HYPOTHESES_FILE


def trade_context(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    """Context for a NEW proposal of `symbol`: its most recent closed trade.

    This is the trade-to-trade thread: a buy card can say "last time we held this
    we exited at rank_drop for -2.1%" instead of proposing from a blank slate.
    """
    row = conn.execute(
        "SELECT exit_ts, exit_price, exit_reason, pnl_pct, strategy_version "
        "FROM trades WHERE symbol=? AND status='closed' ORDER BY exit_ts DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if not row:
        return None
    return {
        "exit_ts": row["exit_ts"],
        "exit_reason": row["exit_reason"],
        "pnl_pct": row["pnl_pct"],
        "strategy_version": row["strategy_version"],
    }


def version_performance(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Closed-trade stats grouped by the strategy version that opened each trade."""
    rows = conn.execute(
        "SELECT strategy_version AS version, COUNT(*) AS n_trades, "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "ROUND(SUM(pnl), 2) AS total_pnl, ROUND(AVG(pnl_pct), 4) AS avg_pnl_pct "
        "FROM trades WHERE status='closed' GROUP BY strategy_version ORDER BY version"
    ).fetchall()
    out = db.rows_to_dicts(rows)
    for r in out:
        r["win_rate"] = round(r["wins"] / r["n_trades"], 2) if r["n_trades"] else 0.0
    return out


def strategy_lineage(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    """Applied AND rejected strategy changes, oldest first — the version history."""
    rows = conn.execute(
        "SELECT proposed_ts, resolved_ts, source, from_version, to_version, "
        "variable, old_value, new_value, rationale, status FROM pending_strategy "
        "WHERE status IN ('applied','rejected') ORDER BY proposed_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return list(reversed(db.rows_to_dicts(rows)))


def reflection_history(limit: int = 10) -> list[dict[str, Any]]:
    """Last N reflection decisions (including holds) from the hypotheses log."""
    if not HYPOTHESES_FILE.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in HYPOTHESES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-limit:]


def recent_trades_with_context(
    conn: sqlite3.Connection, limit: int = 15
) -> list[dict[str, Any]]:
    """Recent closed trades with their decoded entry context, oldest first."""
    rows = conn.execute(
        "SELECT symbol, entry_ts, exit_ts, pnl_pct, exit_reason, strategy_version, "
        "context FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in reversed(rows):
        t = dict(r)
        try:
            t["context"] = json.loads(t["context"]) if t["context"] else None
        except json.JSONDecodeError:
            t["context"] = None
        out.append(t)
    return out


def build(conn: sqlite3.Connection) -> dict[str, Any]:
    """The full memory bundle — one shape shared by reflect, brief, and dashboard."""
    return {
        "version_performance": version_performance(conn),
        "strategy_lineage": strategy_lineage(conn),
        "reflections": reflection_history(),
        "trades": recent_trades_with_context(conn),
    }
