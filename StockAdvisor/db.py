"""
db.py -- SQLite persistence layer (WAL mode).

Design rule that matters most in this file: the WORKER is the only writer
that ever fills a trade, applies a dial change, or writes strategy state.
The dashboard process only ever flips a pending_approvals row from PENDING
to APPROVED or REJECTED -- it never touches positions, trades, cash, or
dials directly. That separation is what makes "every trade is human
approved, but only the worker executes" actually true in code, not just in
spirit.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS dials (
    name TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL  -- 'default' | 'reflection' | 'human_edit'
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    shares REAL NOT NULL,
    avg_cost REAL NOT NULL,
    budgeted_dollars REAL NOT NULL,
    entry_rank INTEGER,
    opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    shares REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    exit_price REAL NOT NULL,
    exit_date TEXT NOT NULL,
    pnl_dollars REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    closed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- 'BUY' | 'DIAL_CHANGE'
    symbol TEXT,
    proposed_dollars REAL,
    rank INTEGER,
    dial_name TEXT,
    dial_old_value REAL,
    dial_new_value REAL,
    rationale TEXT,
    backtest_json TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED | EXPIRED
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS reflection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    closed_trades_considered INTEGER NOT NULL,
    decision TEXT NOT NULL,      -- 'HOLD' | 'PROPOSE'
    dial_name TEXT,
    old_value REAL,
    new_value REAL,
    rationale TEXT NOT NULL,
    mode TEXT NOT NULL,          -- 'fallback' | 'llm'
    approval_id INTEGER,
    outcome TEXT                 -- 'approved' | 'rejected' | 'pending' | NULL (for HOLD)
);

CREATE TABLE IF NOT EXISTS equity_history (
    date TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    invested REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    date TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    content_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,   -- INFO | WARN | ERROR
    message TEXT NOT NULL
);
"""


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """Simple transaction context manager."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    import os
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    # seed dials if empty
    existing = {r["name"] for r in conn.execute("SELECT name FROM dials")}
    for name, val in config.DEFAULT_DIALS.items():
        if name not in existing:
            conn.execute(
                "INSERT INTO dials (name, value, updated_at, updated_by) VALUES (?,?,?,?)",
                (name, val, now_iso(), "default"),
            )
    # seed cash / meta
    if get_meta("cash") is None:
        set_meta("cash", config.PAPER_STARTING_CASH)
    if get_meta("halted") is None:
        set_meta("halted", False)
    if get_meta("pause_buys") is None:
        set_meta("pause_buys", False)
    if get_meta("worker_status") is None:
        set_meta("worker_status", "starting")
    return conn


# ---------------------------------------------------------------------- meta
def get_meta(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_meta(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


# --------------------------------------------------------------------- dials
def get_dial(name):
    conn = get_conn()
    row = conn.execute("SELECT value FROM dials WHERE name=?", (name,)).fetchone()
    if row is None:
        return config.DEFAULT_DIALS[name]
    return row["value"]


def get_all_dials():
    conn = get_conn()
    return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM dials")}


def set_dial(name, value, updated_by="reflection"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO dials (name, value, updated_at, updated_by) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (name, value, now_iso(), updated_by),
    )


# ----------------------------------------------------------------- positions
def get_positions():
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM positions ORDER BY symbol")]


def get_position(symbol):
    conn = get_conn()
    row = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def upsert_position(symbol, shares, avg_cost, budgeted_dollars, entry_rank, opened_at):
    conn = get_conn()
    conn.execute(
        "INSERT INTO positions (symbol, shares, avg_cost, budgeted_dollars, entry_rank, opened_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET shares=excluded.shares, "
        "avg_cost=excluded.avg_cost, budgeted_dollars=excluded.budgeted_dollars",
        (symbol, shares, avg_cost, budgeted_dollars, entry_rank, opened_at),
    )


def delete_position(symbol):
    conn = get_conn()
    conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


# -------------------------------------------------------------------- trades
def record_closed_trade(symbol, shares, entry_price, entry_date, exit_price, exit_date,
                         pnl_dollars, pnl_pct, exit_reason):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trades (symbol, shares, entry_price, entry_date, exit_price, exit_date, "
        "pnl_dollars, pnl_pct, exit_reason, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (symbol, shares, entry_price, entry_date, exit_price, exit_date,
         pnl_dollars, pnl_pct, exit_reason, now_iso()),
    )


def get_closed_trades(limit=200):
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]


def count_closed_trades_since(after_trade_id):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) c FROM trades WHERE id > ?", (after_trade_id,)).fetchone()
    return row["c"]


def max_trade_id():
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(MAX(id),0) m FROM trades").fetchone()
    return row["m"]


# ------------------------------------------------------------ approvals queue
def create_pending_approval(kind, symbol=None, proposed_dollars=None, rank=None,
                             dial_name=None, dial_old_value=None, dial_new_value=None,
                             rationale="", backtest_json=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO pending_approvals (kind, symbol, proposed_dollars, rank, dial_name, "
        "dial_old_value, dial_new_value, rationale, backtest_json, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'PENDING')",
        (kind, symbol, proposed_dollars, rank, dial_name, dial_old_value, dial_new_value,
         rationale, backtest_json, now_iso()),
    )
    return cur.lastrowid


def get_pending_approvals(status="PENDING"):
    conn = get_conn()
    if status is None:
        rows = conn.execute("SELECT * FROM pending_approvals ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pending_approvals WHERE status=? ORDER BY id ASC", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_approval(approval_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM pending_approvals WHERE id=?", (approval_id,)).fetchone()
    return dict(row) if row else None


def set_approval_decision(approval_id, decision):
    """decision: 'APPROVED' or 'REJECTED'. Called ONLY by the dashboard.
    This never fills a trade or applies a dial -- it just flips the flag;
    the worker's reconcile step does the actual work on its next loop."""
    conn = get_conn()
    conn.execute(
        "UPDATE pending_approvals SET status=?, decided_at=? WHERE id=? AND status='PENDING'",
        (decision, now_iso(), approval_id),
    )


def mark_approval_status(approval_id, status):
    """Used by the WORKER to move an approval from APPROVED -> FILLED-equivalent
    bookkeeping is implicit (approved rows that have been acted on are left as
    APPROVED; we don't need a separate FILLED state since positions/trades are
    the source of truth). Used also to EXPIRE stale buy proposals."""
    conn = get_conn()
    conn.execute("UPDATE pending_approvals SET status=? WHERE id=?", (status, approval_id))


def total_pending_buy_dollars():
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(proposed_dollars),0) s FROM pending_approvals "
        "WHERE kind='BUY' AND status='PENDING'"
    ).fetchone()
    return row["s"]


# ---------------------------------------------------------------- reflection
def log_reflection(closed_trades_considered, decision, rationale, mode,
                    dial_name=None, old_value=None, new_value=None,
                    approval_id=None, outcome=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO reflection_log (created_at, closed_trades_considered, decision, "
        "dial_name, old_value, new_value, rationale, mode, approval_id, outcome) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), closed_trades_considered, decision, dial_name, old_value, new_value,
         rationale, mode, approval_id, outcome),
    )


def get_reflection_log(limit=100):
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM reflection_log ORDER BY id DESC LIMIT ?", (limit,))]


def update_reflection_outcome(approval_id, outcome):
    conn = get_conn()
    conn.execute(
        "UPDATE reflection_log SET outcome=? WHERE approval_id=?", (outcome, approval_id)
    )


def was_dial_change_rejected(dial_name, new_value, tolerance=1e-9):
    """Hard rule: never re-propose a change the human already rejected."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT dial_new_value FROM pending_approvals WHERE kind='DIAL_CHANGE' "
        "AND dial_name=? AND status='REJECTED'", (dial_name,)
    ).fetchall()
    return any(abs(r["dial_new_value"] - new_value) < tolerance for r in rows)


# ------------------------------------------------------------------ equity
def record_equity(date, equity, cash, invested):
    conn = get_conn()
    conn.execute(
        "INSERT INTO equity_history (date, equity, cash, invested) VALUES (?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET equity=excluded.equity, cash=excluded.cash, "
        "invested=excluded.invested",
        (date, equity, cash, invested),
    )


def get_equity_history(limit=5000):
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM equity_history ORDER BY date ASC LIMIT ?", (limit,))]


def get_peak_equity():
    conn = get_conn()
    row = conn.execute("SELECT MAX(equity) m FROM equity_history").fetchone()
    return row["m"]


# ------------------------------------------------------------------- briefs
def save_brief(date, content: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO briefs (date, created_at, content_json) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET content_json=excluded.content_json",
        (date, now_iso(), json.dumps(content)),
    )


def get_brief(date):
    conn = get_conn()
    row = conn.execute("SELECT * FROM briefs WHERE date=?", (date,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["content"] = json.loads(d.pop("content_json"))
    return d


def get_latest_brief():
    conn = get_conn()
    row = conn.execute("SELECT * FROM briefs ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    d["content"] = json.loads(d.pop("content_json"))
    return d


def get_brief_history(limit=30):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM briefs ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["content"] = json.loads(d.pop("content_json"))
        out.append(d)
    return out


# ------------------------------------------------------------------- events
def log_event(level, message):
    conn = get_conn()
    conn.execute(
        "INSERT INTO worker_events (created_at, level, message) VALUES (?,?,?)",
        (now_iso(), level, message),
    )
    # keep table bounded
    conn.execute(
        "DELETE FROM worker_events WHERE id NOT IN "
        "(SELECT id FROM worker_events ORDER BY id DESC LIMIT 500)"
    )


def get_events(limit=100):
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM worker_events ORDER BY id DESC LIMIT ?", (limit,))]
