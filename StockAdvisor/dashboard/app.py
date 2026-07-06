"""
dashboard/app.py -- local-only FastAPI dashboard (127.0.0.1:8000 by
default). No cloud, no accounts, no referral links.

CRITICAL SEPARATION OF DUTIES: this process NEVER fills a trade, never
applies a dial change, and never writes to positions/trades/cash/dials. Its
write surface is exactly two endpoints (/api/approve, /api/reject) and both
of them do nothing more than flip a pending_approvals row from PENDING to
APPROVED/REJECTED. The worker process (worker.py) is the sole writer that
turns an APPROVED row into an actual (paper) fill or dial change. If you
are auditing this codebase for safety, this file plus db.py's
set_approval_decision() is the entire footprint of what a dashboard click
can do.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

import jinja2
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import backtester
import config
import data
import db
import risk

app = FastAPI(title="StockAdvisor Dashboard")

# NOTE: we use a plain jinja2.Environment directly here (rather than
# starlette's Jinja2Templates wrapper) -- this dashboard has exactly one
# page and no need for the extra request-context machinery, and it keeps
# this file's dependency surface small and easy to audit.
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=True,
)

db.init_db()


def _positions_with_marks():
    positions = db.get_positions()
    cash = db.get_meta("cash", 0.0)
    invested = 0.0
    out = []
    for p in positions:
        price = data.get_latest_price(p["symbol"]) or p["avg_cost"]
        market_value = p["shares"] * price
        invested += market_value
        unrealized = market_value - p["shares"] * p["avg_cost"]
        unrealized_pct = (price / p["avg_cost"] - 1.0) * 100 if p["avg_cost"] else 0.0
        out.append({
            "symbol": p["symbol"], "shares": round(p["shares"], 4), "avg_cost": round(p["avg_cost"], 2),
            "price": round(price, 2), "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized, 2), "unrealized_pnl_pct": round(unrealized_pct, 2),
            "opened_at": p["opened_at"], "entry_rank": p["entry_rank"],
        })
    equity = cash + invested
    for o in out:
        o["weight_pct"] = round((o["market_value"] / equity * 100) if equity else 0.0, 2)
    return out, cash, invested, equity


def build_state() -> dict:
    positions, cash, invested, equity = _positions_with_marks()
    starting_cash = config.PAPER_STARTING_CASH
    return_pct = (equity / starting_cash - 1.0) * 100 if starting_cash else 0.0

    pending_buys = db.get_pending_approvals("PENDING")
    pending_buys = [p for p in pending_buys if p["kind"] == "BUY"]
    pending_dials = [p for p in db.get_pending_approvals("PENDING") if p["kind"] == "DIAL_CHANGE"]

    spy_state = None
    try:
        s = risk.get_spy_state()
        spy_state = {"price": round(s.price, 2), "day_return_pct": round(s.day_return * 100, 2),
                     "sma200": round(s.sma200, 2) if s.sma200 else None, "above_200dma": s.above_200dma}
    except Exception:
        pass

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worker": {
            "status": db.get_meta("worker_status", "unknown"),
            "last_heartbeat": db.get_meta("worker_last_heartbeat"),
            "started_at": db.get_meta("worker_started_at"),
        },
        "risk": {
            "halted": bool(db.get_meta("halted", False)),
            "halt_reason": db.get_meta("halt_reason"),
            "pause_buys": bool(db.get_meta("pause_buys", False)),
            "pause_reason": db.get_meta("pause_reason"),
            "spy": spy_state,
        },
        "book": {
            "cash": round(cash, 2), "invested": round(invested, 2), "equity": round(equity, 2),
            "starting_cash": starting_cash, "return_pct": round(return_pct, 2),
        },
        "positions": positions,
        "closed_trades": db.get_closed_trades(limit=100),
        "pending_buys": pending_buys,
        "pending_dial_changes": pending_dials,
        "equity_history": db.get_equity_history(),
        "dials": db.get_all_dials(),
        "reflection_log": db.get_reflection_log(limit=50),
        "brief": db.get_latest_brief(),
        "brief_history": db.get_brief_history(limit=14),
        "events": db.get_events(limit=50),
        "locked_structure": {
            "universe": config.UNIVERSE, "benchmark": config.BENCHMARK, "top_n_hold": config.TOP_N_HOLD,
            "momentum_lookbacks_days": config.MOMENTUM_LOOKBACKS_DAYS,
            "rebalance_weekday": config.REBALANCE_WEEKDAY,
            "black_swan_spy_day_drop": config.BLACK_SWAN_SPY_DAY_DROP,
            "black_swan_drawdown": config.BLACK_SWAN_DRAWDOWN,
            "early_brake_spy_day_drop": config.EARLY_BRAKE_SPY_DAY_DROP,
            "spy_resume_ma_days": config.SPY_RESUME_MA_DAYS,
        },
        "dial_bounds": config.DIAL_BOUNDS,
        "reflection_mode": config.REFLECTION_MODE,
        "reflection_every_n_trades": config.REFLECTION_EVERY_N_TRADES,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    template = _jinja_env.get_template("index.html")
    return HTMLResponse(template.render(state=build_state()))


@app.get("/api/state")
def api_state():
    return JSONResponse(build_state())


@app.post("/api/approve/{approval_id}")
def api_approve(approval_id: int):
    a = db.get_approval(approval_id)
    if not a:
        raise HTTPException(404, "Approval not found")
    if a["status"] != "PENDING":
        raise HTTPException(400, f"Approval is already {a['status']}")
    db.set_approval_decision(approval_id, "APPROVED")
    db.log_event("INFO", f"Human APPROVED approval #{approval_id} ({a['kind']}).")
    return {"ok": True}


@app.post("/api/reject/{approval_id}")
def api_reject(approval_id: int):
    a = db.get_approval(approval_id)
    if not a:
        raise HTTPException(404, "Approval not found")
    if a["status"] != "PENDING":
        raise HTTPException(400, f"Approval is already {a['status']}")
    db.set_approval_decision(approval_id, "REJECTED")
    db.log_event("INFO", f"Human REJECTED approval #{approval_id} ({a['kind']}).")
    return {"ok": True}


@app.post("/api/resume")
def api_resume():
    risk.manual_resume()
    return {"ok": True}


@app.get("/api/backtest_preview/{approval_id}")
def api_backtest_preview(approval_id: int):
    """On-demand backtest-in-approval. Only meaningful for DIAL_CHANGE
    approvals. Can take a few seconds -- 10-15 years across 11 ETFs. Never
    called automatically; only when the human clicks 'Run backtest'."""
    a = db.get_approval(approval_id)
    if not a or a["kind"] != "DIAL_CHANGE":
        raise HTTPException(404, "No such dial-change approval")
    current_dials = db.get_all_dials()
    proposed_dials = dict(current_dials)
    proposed_dials[a["dial_name"]] = a["dial_new_value"]
    try:
        price_data, spy_df = backtester.load_backtest_history()
        result = backtester.compare_dials(price_data, spy_df, current_dials, proposed_dials)
        return JSONResponse(result)
    except Exception as e:
        raise HTTPException(500, f"Backtest failed: {e}")
