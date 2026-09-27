"""FastAPI dashboard: live state + the human approval queue.

Run:  uvicorn dashboard.app:app --port 8000
The dashboard only flips pending rows to approved/rejected; the worker reconciles
them on its next tick (it is the sole filler of trades / applier of strategy).
"""
from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from hermes_trading import approval, brief, db, drift, memory, paper_broker, review, settings, spy_compare, wheel
from hermes_trading import execution as ex
from hermes_trading.config import load_goal, load_strategy
from hermes_trading.loop import RISK_CHECK_SECONDS, TICK_SECONDS
from hermes_trading.paths import HEARTBEAT_FILE, STATE_DIR, ensure_dirs
from hermes_trading.reflect import propose_once
from hermes_trading.score import score

app = FastAPI(title="hermes-trading dashboard")

# guards a manual "Reflect now" so double-clicks can't spawn two at once
_reflect_lock = threading.Lock()
_reflecting = False

# guards a manual "Brief now" (the generate fetches market data — takes seconds)
_brief_lock = threading.Lock()
_briefing = False

# tracks which pending-strategy proposals are currently being backtested
_bt_lock = threading.Lock()
_bt_running: set[int] = set()

STATIC_DIR = (STATE_DIR.parent / "dashboard" / "static")


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    db.init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _heartbeat() -> dict[str, Any]:
    if HEARTBEAT_FILE.exists():
        try:
            return json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _last_price(conn) -> float | None:
    hb = _heartbeat()
    if hb.get("last"):
        return float(hb["last"])
    row = conn.execute(
        "SELECT entry_price FROM trades ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return float(row["entry_price"]) if row else None


@app.get("/api/state")
def state() -> JSONResponse:
    conn = db.connect()
    try:
        goal = load_goal()
        strat = load_strategy()
        hb = _heartbeat()
        hb_prices = hb.get("prices") or {}
        single = hb.get("last")
        is_rotation = strat.get("type") == "relative_strength_rotation"

        # mark each position against ITS OWN latest price (never a single shared price)
        open_pos = paper_broker.open_positions(conn)
        for p in open_pos:
            sym = p["symbol"]
            if sym in hb_prices:
                px = float(hb_prices[sym])
            elif not is_rotation and single is not None:
                px = float(single)            # single-symbol RSI strategy
            else:
                px = p["entry_price"]         # no live price yet -> mark flat, don't mismark
            p["last"] = px
            p["value"] = round(p["size"] * px, 2)          # market value of the position
            p["unrealised"] = round(p["size"] * (px - p["entry_price"]), 2)
            p["unrealised_pct"] = round((px - p["entry_price"]) / p["entry_price"], 4) if p["entry_price"] else 0.0

        price = float(single) if single is not None else None

        # decode entry context so the approval card can show rank / prior exit
        pending_trades = approval.list_pending_trades(conn)
        for t in pending_trades:
            try:
                t["context"] = json.loads(t["context"]) if t.get("context") else None
            except (json.JSONDecodeError, TypeError):
                t["context"] = None

        # strategy proposals: attach the auto-backtest + whether approval is allowed yet
        pending_strategy = approval.list_pending_strategy(conn)
        for s in pending_strategy:
            s["backtest"] = review.backtest_of(s)
            s["bt_status"] = review.bt_status(s)
            s["stale"] = review.is_stale(s, strat)
            s["can_approve"], s["approve_block"] = review.can_approve(s, strat)
            try:
                s["changes"] = json.loads(s["changes_json"]) if s.get("changes_json") else None
            except (json.JSONDecodeError, TypeError):
                s["changes"] = None

        closed = db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT 50"
            ).fetchall()
        )
        equity_rows = db.rows_to_dicts(
            conn.execute(
                "SELECT ts, equity FROM "
                "(SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 2000) ORDER BY ts"
            ).fetchall()
        )
        curve = [r["equity"] for r in equity_rows]
        scored = score(closed, goal, curve)

        # live equity consistent with the per-symbol marks above
        price_map = {p["symbol"]: p["last"] for p in open_pos}
        equity, cash, invested = paper_broker.equity_now_multi(conn, price_map)
        for p in open_pos:
            p["weight"] = round(p["value"] / equity, 4) if equity else 0.0

        # reflection countdown
        every = int(goal.get("reflection_every", 5))
        closed_total = conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status='closed'"
        ).fetchone()["c"]
        since_last = max(0, closed_total - int(db.get_meta(conn, "last_reflection_count", "0") or "0"))

        return JSONResponse(
            {
                "heartbeat": hb,
                "goal": goal,
                "strategy": strat,
                "price": price,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "invested": round(invested, 2),
                "start_equity": paper_broker.start_equity(),
                "score": scored,
                "open_positions": open_pos,
                "closed_trades": closed,
                "pending_trades": pending_trades,
                "pending_strategy": pending_strategy,
                "drift": drift.report(conn, strat),
                "options": wheel.state(conn),
                "cash_interest": round(ex.accrued_interest(conn), 2),
                "brief": brief.latest(conn),
                "briefing": _briefing,
                # heartbeat "last" is the benchmark price in rotation mode — the
                # live mark for open trades' shadow-SPY windows
                "spy_compare": spy_compare.summary(
                    conn, price if is_rotation else None, price_map
                ),
                "memory": {
                    "version_performance": memory.version_performance(conn),
                    "strategy_lineage": memory.strategy_lineage(conn),
                    "reflections": memory.reflection_history(6),
                },
                "equity_curve": [
                    {"ts": r["ts"], "equity": round(r["equity"], 2)} for r in equity_rows
                ],
                "settings": {
                    "auto_reflect": settings.auto_reflect(conn),
                    "reflect_mode": settings.reflect_mode(conn),
                    "fast_exits": settings.fast_exits(conn),
                },
                "reflecting": _reflecting,
                "backtesting": sorted(_bt_running),
                "reflection": {
                    "every": every,
                    "since_last": since_last,
                    "remaining": max(0, every - since_last),
                    "last_note": db.get_meta(conn, "last_reflection_note", "") or "",
                    "last_ts": float(db.get_meta(conn, "last_reflection_ts", "0") or "0"),
                },
                "risk_off": {
                    "active": db.get_meta(conn, "risk_off", "0") == "1",
                    "reason": db.get_meta(conn, "risk_off_reason", "") or "",
                },
                "buys_halted": {
                    "active": db.get_meta(conn, "buys_halted", "0") == "1",
                    "reason": db.get_meta(conn, "buys_halted_reason", "") or "",
                },
                "cadence": {
                    "tick_seconds": TICK_SECONDS,
                    "risk_check_seconds": RISK_CHECK_SECONDS,
                },
            }
        )
    finally:
        conn.close()


@app.post("/api/risk-off/clear")
def clear_risk_off() -> dict[str, bool]:
    """Manually resume trading after a black-swan risk-off."""
    conn = db.connect()
    try:
        db.set_meta(conn, "risk_off", "0")
        db.set_meta(conn, "risk_off_reason", "")
        conn.commit()
        return {"risk_off": False}
    finally:
        conn.close()


@app.post("/api/strategy/{prop_id}/backtest")
def backtest_strategy(prop_id: int) -> dict[str, str]:
    """(Re-)run a proposal's backtest now. Proposals are backtested automatically by
    the worker; this is the manual re-run (e.g. after a failed fetch)."""
    conn = db.connect()
    try:
        if not review.fetch(conn, prop_id):
            raise HTTPException(404, f"pending strategy {prop_id} not found")
    finally:
        conn.close()

    with _bt_lock:
        if prop_id in _bt_running:
            raise HTTPException(409, "backtest already running for this proposal")
        _bt_running.add(prop_id)

    def _run() -> None:
        try:
            review.run_backtest(prop_id, force=True)
        finally:
            with _bt_lock:
                _bt_running.discard(prop_id)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


_drift_lock = threading.Lock()
_drift_running = False


@app.post("/api/drift/refresh")
def drift_refresh() -> dict[str, str]:
    """Re-run the original-vs-live backtest now (normally refreshed weekly)."""
    global _drift_running
    with _drift_lock:
        if _drift_running:
            raise HTTPException(409, "drift backtest already running")
        _drift_running = True

    def _run() -> None:
        global _drift_running
        try:
            drift.refresh()
        finally:
            _drift_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.post("/api/brief-now")
def brief_now() -> dict[str, str]:
    """Regenerate today's morning brief on demand (fetches fresh market data)."""
    global _briefing
    with _brief_lock:
        if _briefing:
            raise HTTPException(409, "a brief is already being generated")
        _briefing = True

    def _run() -> None:
        global _briefing
        try:
            brief.generate()
        except Exception:  # noqa: BLE001 — surfaced by the brief simply not updating
            pass
        finally:
            _briefing = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.post("/api/reflect-now")
def reflect_now() -> dict[str, str]:
    """Trigger one reflection on demand (background); result lands in the queue."""
    global _reflecting
    conn = db.connect()
    try:
        mode = settings.reflect_mode(conn)
    finally:
        conn.close()

    with _reflect_lock:
        if _reflecting:
            raise HTTPException(409, "a reflection is already running")
        _reflecting = True

    def _run() -> None:
        global _reflecting
        try:
            propose_once(mode)
        except Exception:  # noqa: BLE001 — never let the thread crash silently swallow state
            pass
        finally:
            _reflecting = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "mode": mode}


class SettingsIn(BaseModel):
    auto_reflect: bool | None = None
    reflect_mode: str | None = None
    fast_exits: bool | None = None


@app.post("/api/settings")
def update_settings(payload: SettingsIn) -> dict[str, object]:
    conn = db.connect()
    try:
        if payload.auto_reflect is not None:
            settings.set_auto_reflect(conn, payload.auto_reflect)
        if payload.reflect_mode is not None:
            settings.set_reflect_mode(conn, payload.reflect_mode)
        if payload.fast_exits is not None:
            settings.set_fast_exits(conn, payload.fast_exits)
        conn.commit()
        return {
            "auto_reflect": settings.auto_reflect(conn),
            "reflect_mode": settings.reflect_mode(conn),
            "fast_exits": settings.fast_exits(conn),
        }
    finally:
        conn.close()


@app.post("/api/trades/{pending_id}/approve")
def approve_trade(pending_id: int) -> dict[str, str]:
    return _set_trade(pending_id, "approved")


@app.post("/api/trades/{pending_id}/reject")
def reject_trade(pending_id: int) -> dict[str, str]:
    return _set_trade(pending_id, "rejected")


def _set_trade(pending_id: int, status: str) -> dict[str, str]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status FROM pending_trades WHERE id=?", (pending_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"pending trade {pending_id} not found")
        if row["status"] != "pending":
            raise HTTPException(409, f"trade {pending_id} already {row['status']}")
        approval.set_trade_status(conn, pending_id, status)
        conn.commit()
        return {"id": str(pending_id), "status": status}
    finally:
        conn.close()


@app.post("/api/options/{pid}/approve")
def approve_option(pid: int) -> dict[str, str]:
    return _decide_option(pid, "approve")


@app.post("/api/options/{pid}/reject")
def reject_option(pid: int) -> dict[str, str]:
    return _decide_option(pid, "reject")


def _decide_option(pid: int, action: str) -> dict[str, str]:
    """Options stream: approve/reject a proposed option sale (the worker fills it at the
    live quote on its next tick during market hours)."""
    conn = db.connect()
    try:
        ok, msg = wheel.decide(conn, pid, action)
        if not ok:
            raise HTTPException(409, msg)
        return {"id": str(pid), "status": msg}
    finally:
        conn.close()


_screen_lock = threading.Lock()
_screen_running = False


@app.post("/api/options/screen")
def options_screen_now() -> dict[str, str]:
    """Re-run the free-cash-flow screen now (normally weekly)."""
    global _screen_running
    with _screen_lock:
        if _screen_running:
            raise HTTPException(409, "screen already running")
        _screen_running = True

    def _run() -> None:
        global _screen_running
        from hermes_trading import wheel_book, wheel_screen
        try:
            wheel_screen.refresh(wheel_book.load_config())
        finally:
            _screen_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


class DecisionIn(BaseModel):
    reason: str | None = None


@app.post("/api/strategy/{prop_id}/approve")
def approve_strategy(prop_id: int, payload: DecisionIn | None = None) -> dict[str, str]:
    return _decide_strategy(prop_id, "approve", payload)


@app.post("/api/strategy/{prop_id}/reject")
def reject_strategy(prop_id: int, payload: DecisionIn | None = None) -> dict[str, str]:
    return _decide_strategy(prop_id, "reject", payload)


def _decide_strategy(prop_id: int, action: str, payload: DecisionIn | None) -> dict[str, str]:
    """Approve/reject a strategy change. Requires a one-line reason; approval also
    requires a finished backtest against the live config (see hermes_trading.review)."""
    conn = db.connect()
    try:
        ok, msg = review.decide(conn, prop_id, action, payload.reason if payload else None, "dashboard")
        if not ok:
            code = 404 if msg == "proposal not found" else (409 if msg.startswith("already") else 400)
            raise HTTPException(code, msg)
        return {"id": str(prop_id), "status": msg}
    finally:
        conn.close()


# static assets (Chart.js shim etc., if added later)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
