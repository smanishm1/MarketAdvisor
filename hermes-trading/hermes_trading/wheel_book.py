"""The options stream's paper book — separate from the ETF book.

Unlike the ETF book (cash derived from P&L), options need an explicit cash ledger:
selling a put CREDITS the premium, its collateral (strike x 100) is RESERVED but stays
in cash (and earns interest), assignment DEBITS strike x 100 for the shares, a call
being exercised CREDITS strike x 100 for them.

    equity = cash + market value of shares held - cost to buy back open short options
    free cash = cash - reserved put collateral
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import yaml

from . import db
from . import execution as ex
from .paths import OPTIONS_FILE

CONTRACT = 100   # shares per option contract


def load_config() -> dict[str, Any]:
    with OPTIONS_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---- cash ledger ---------------------------------------------------------------------


def cash(conn: sqlite3.Connection, cfg: dict[str, Any] | None = None) -> float:
    v = db.get_meta(conn, "opt_cash", None)
    if v is None:   # first use: fund the book
        start = float((cfg or load_config()).get("start_equity", 10000))
        db.set_meta(conn, "opt_cash", f"{start:.6f}")
        return start
    return float(v)


def add_cash(conn: sqlite3.Connection, amount: float) -> float:
    new = cash(conn) + amount
    db.set_meta(conn, "opt_cash", f"{new:.6f}")
    return new


def accrue_interest(conn: sqlite3.Connection, cfg: dict[str, Any], today: dt.date | None = None) -> float:
    """Credit T-bill interest on the whole cash balance (collateral included), once a day."""
    today = today or dt.date.today()
    last = db.get_meta(conn, "opt_interest_date", None)
    db.set_meta(conn, "opt_interest_date", today.isoformat())
    if last is None:
        return 0.0
    days = (today - dt.date.fromisoformat(last)).days
    bal = cash(conn, cfg)
    if days <= 0 or bal <= 0:
        return 0.0
    amt = bal * ((1.0 + ex.latest_annual(cfg.get("cash_rate", "tbill"))) ** (days / 365.0) - 1.0)
    add_cash(conn, amt)
    db.set_meta(conn, "opt_interest_total",
                f"{float(db.get_meta(conn, 'opt_interest_total', '0') or 0) + amt:.6f}")
    return amt


# ---- positions -------------------------------------------------------------------


def open_positions(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM opt_positions WHERE status='open'"
    rows = conn.execute(q + (" AND kind=?" if kind else "") + " ORDER BY open_ts",
                        (kind,) if kind else ()).fetchall()
    return db.rows_to_dicts(rows)


def open_position(conn, kind, symbol, qty, price, *, contract=None, strike=None, expiry=None,
                  context=None) -> int:
    cur = conn.execute(
        "INSERT INTO opt_positions(kind, symbol, contract, strike, expiry, qty, open_price, open_ts, "
        "status, context) VALUES(?,?,?,?,?,?,?,?, 'open', ?)",
        (kind, symbol, contract, strike, expiry, qty, price, db.now(),
         json.dumps(context) if isinstance(context, dict) else context),
    )
    return int(cur.lastrowid)


def close_position(conn, pos: dict[str, Any], close_price: float, reason: str, pnl: float) -> None:
    conn.execute(
        "UPDATE opt_positions SET status='closed', close_price=?, close_ts=?, close_reason=?, pnl=? "
        "WHERE id=? AND status='open'",
        (close_price, db.now(), reason, pnl, pos["id"]),
    )


def slots_used(conn: sqlite3.Connection) -> set[str]:
    """Wheel slots in use: one per underlying with an open short put or shares."""
    return {p["symbol"] for p in open_positions(conn) if p["kind"] in ("short_put", "stock")}


def reserved(conn: sqlite3.Connection) -> float:
    return sum(float(p["strike"]) * CONTRACT * float(p["qty"]) for p in open_positions(conn, "short_put"))


# ---- proposals (need approval) ------------------------------------------------------


def propose(conn, action, symbol, contract, strike, expiry, qty, price, context,
            position_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO opt_pending(action, symbol, contract, strike, expiry, qty, price, position_id, "
        "proposed_ts, status, context) VALUES(?,?,?,?,?,?,?,?,?, 'pending', ?)",
        (action, symbol, contract, strike, expiry, qty, price, position_id, db.now(), json.dumps(context)),
    )
    return int(cur.lastrowid)


def pending(conn: sqlite3.Connection, status: str = "pending") -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM opt_pending WHERE status=? ORDER BY proposed_ts", (status,)).fetchall()
    out = db.rows_to_dicts(rows)
    for r in out:
        try:
            r["context"] = json.loads(r["context"]) if r.get("context") else {}
        except (json.JSONDecodeError, TypeError):
            r["context"] = {}
    return out


def set_pending_status(conn, pid: int, status: str) -> bool:
    cur = conn.execute(
        "UPDATE opt_pending SET status=?, resolved_ts=? WHERE id=? AND status IN ('pending','approved')",
        (status, db.now(), pid),
    )
    return cur.rowcount == 1


# ---- valuation --------------------------------------------------------------------


def marks(conn: sqlite3.Connection) -> dict[str, Any]:
    """Latest marks written by the worker: {'stocks': {sym: px}, 'options': {contract: mid}, 'ts': …}"""
    raw = db.get_meta(conn, "opt_marks", None)
    try:
        return json.loads(raw) if raw else {"stocks": {}, "options": {}}
    except json.JSONDecodeError:
        return {"stocks": {}, "options": {}}


def valuation(conn: sqlite3.Connection, cfg: dict[str, Any] | None = None,
              mk: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    mk = mk or marks(conn)
    bal = cash(conn, cfg)
    stock_val = short_liab = 0.0
    for p in open_positions(conn):
        if p["kind"] == "stock":
            stock_val += float(p["qty"]) * float(mk["stocks"].get(p["symbol"], p["open_price"]))
        else:   # cost to buy back the short option (mid, else last trade, else what we sold it for)
            q = mk["options"].get(p["contract"]) or {}
            px = q.get("mid") or q.get("last") or p["open_price"]
            short_liab += float(p["qty"]) * CONTRACT * float(px)
    equity = bal + stock_val - short_liab
    res = reserved(conn)
    start = float(cfg.get("start_equity", 10000))
    closed = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) s, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w "
        "FROM opt_positions WHERE status='closed' AND kind IN ('short_put','short_call')"
    ).fetchone()
    return {
        "equity": equity, "cash": bal, "reserved": res, "free_cash": bal - res,
        "stock_value": stock_val, "short_liability": short_liab,
        "start_equity": start, "return": (equity - start) / start if start else 0.0,
        "premium_trades_closed": int(closed["n"] or 0),
        "premium_realised": float(closed["s"] or 0.0),
        "win_rate": (float(closed["w"] or 0) / closed["n"]) if closed["n"] else None,
        "interest_total": float(db.get_meta(conn, "opt_interest_total", "0") or 0.0),
        "marks_ts": mk.get("ts"),
    }


def snapshot(conn: sqlite3.Connection, v: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO opt_equity(ts, equity, cash, reserved, positions_value) VALUES(?,?,?,?,?)",
        (round(db.now(), 1), v["equity"], v["cash"], v["reserved"], v["stock_value"] - v["short_liability"]),
    )
