"""SQLite state shared by the worker and the dashboard.

WAL mode lets the worker (writer) and dashboard (reader + status writer) operate
concurrently. The worker is the only process that *fills* trades and *applies*
strategy changes; the dashboard only flips pending rows to approved/rejected.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from .paths import DB_FILE, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    proposed_ts     REAL NOT NULL,
    price           REAL NOT NULL,
    rsi             REAL,
    stop_price      REAL NOT NULL,
    target_price    REAL NOT NULL,
    size            REAL NOT NULL,
    strategy_version TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|filled
    resolved_ts     REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    entry_ts         REAL NOT NULL,
    entry_price      REAL NOT NULL,
    size             REAL NOT NULL,
    stop_price       REAL NOT NULL,
    target_price     REAL NOT NULL,
    strategy_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',     -- open|closed
    exit_ts          REAL,
    exit_price       REAL,
    exit_reason      TEXT,
    pnl              REAL,
    pnl_pct          REAL
);

CREATE TABLE IF NOT EXISTS equity (
    ts              REAL PRIMARY KEY,
    equity          REAL NOT NULL,
    cash            REAL NOT NULL,
    position_value  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_strategy (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_ts   REAL NOT NULL,
    source        TEXT NOT NULL,            -- fallback|hermes
    from_version  TEXT NOT NULL,
    to_version    TEXT NOT NULL,
    variable      TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    rationale     TEXT,
    proposed_yaml TEXT NOT NULL,            -- full candidate strategy.yaml
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|applied
    resolved_ts   REAL,
    backtest_json TEXT                       -- cached current-vs-proposed backtest comparison
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
    date  TEXT PRIMARY KEY,   -- ISO date the brief is FOR (one per day, regenerable)
    ts    REAL NOT NULL,      -- when it was generated
    json  TEXT NOT NULL       -- composed brief (see hermes_trading.brief)
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # lightweight migrations for DBs created before a column existed
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(pending_strategy)").fetchall()]
        if "backtest_json" not in cols:
            conn.execute("ALTER TABLE pending_strategy ADD COLUMN backtest_json TEXT")
        for table in ("pending_trades", "trades"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "context" not in cols:
                # entry context (rank, score, prior exit of the same symbol, …) —
                # carried from the proposal onto the filled trade so every trade
                # remembers WHY it was opened. JSON; see hermes_trading.memory.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN context TEXT")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "spy_entry" not in cols:
            # shadow-SPY snapshots: SPY's price at fill and at close, so each trade
            # can be compared against "the same dollars in SPY over the same window"
            # — only while the position is open. See hermes_trading.spy_compare.
            conn.execute("ALTER TABLE trades ADD COLUMN spy_entry REAL")
            conn.execute("ALTER TABLE trades ADD COLUMN spy_exit REAL")
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def now() -> float:
    return time.time()
