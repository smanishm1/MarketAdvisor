"""
worker.py -- the long-running loop. This process is the ONLY writer that
ever fills a trade, applies a dial change, or advances strategy state. The
dashboard (app.py) only ever flips a pending_approvals row between PENDING
and APPROVED/REJECTED; everything else happens here.

Every tick (default every ~20s, config.INTRADAY_RISK_POLL_SEC):
  1. Reconcile human decisions (fill approved buys, apply approved dial
     changes, note rejected ones).
  2. Intraday risk overlay: live prices for held names + SPY -> catastrophe
     stops and circuit breakers, so they react in minutes.
  3. Once per calendar day: daily-close-based decisions -- rank the
     universe, check exits (RANK_DROP / BELOW_TREND), and on the weekly
     rebalance day (Friday) only, PROPOSE new buys into the approval queue.
     Also runs the reflection cycle (if enough trades have closed) and
     composes the morning brief.

Nothing in this file ever fills a BUY without a prior human approval. Only
protective exits (stops, breakers) are automatic, exactly as required.
"""
import sys
import time
import traceback
from datetime import datetime, timezone

import brief
import config
import data
import db
import reflection
import risk
import strategy


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)  # ASCII only, no unicode -- safe for Windows cp1252 console


# ---------------------------------------------------------------- reconcile
def reconcile_approvals():
    """The ONLY place a human Approve click turns into a real (paper)
    action. Runs every tick so approvals get picked up within ~20s."""
    approvals = db.get_pending_approvals(status="APPROVED")
    for a in approvals:
        try:
            if a["kind"] == "BUY":
                _fill_approved_buy(a)
            elif a["kind"] == "DIAL_CHANGE":
                _apply_approved_dial_change(a)
        except Exception as e:
            log(f"ERROR reconciling approval #{a['id']}: {e}")
            db.log_event("ERROR", f"Failed to reconcile approval #{a['id']}: {e}")

    rejected_dials = db.get_pending_approvals(status="REJECTED")
    for a in rejected_dials:
        if a["kind"] == "DIAL_CHANGE" and a.get("dial_name"):
            db.update_reflection_outcome(a["id"], "rejected")


def _fill_approved_buy(approval):
    symbol = approval["symbol"]
    existing = db.get_position(symbol)
    if existing:
        log(f"Skipping approval #{approval['id']} for {symbol}: already holding this position.")
        db.mark_approval_status(approval["id"], "FILLED")
        return
    price = data.get_latest_price(symbol)
    if not price or price <= 0:
        log(f"Could not get a live price for {symbol}; leaving approval #{approval['id']} APPROVED for next tick.")
        return
    cash = db.get_meta("cash", 0.0)
    # Re-priced at approval time, preserving the DOLLAR AMOUNT budgeted, capped
    # by whatever cash is actually available right now (no margin, ever).
    dollars = min(approval["proposed_dollars"], cash)
    if dollars < 1.0:
        log(f"Not enough cash to fill approval #{approval['id']} for {symbol}; skipping this cycle.")
        db.mark_approval_status(approval["id"], "EXPIRED")
        db.log_event("WARN", f"Approval #{approval['id']} ({symbol}) expired: insufficient cash at fill time.")
        return
    shares = dollars / price
    db.set_meta("cash", cash - shares * price)
    db.upsert_position(symbol, shares, price, dollars, approval["rank"], db.now_iso())
    db.mark_approval_status(approval["id"], "FILLED")
    log(f"FILLED BUY: {symbol} {shares:.4f} sh @ {price:.2f} (${shares*price:.2f}) -- approval #{approval['id']}")
    db.log_event("INFO", f"Filled approved buy: {symbol} {shares:.4f} sh @ {price:.2f}")


def _apply_approved_dial_change(approval):
    db.set_dial(approval["dial_name"], approval["dial_new_value"], updated_by="human_approved")
    db.mark_approval_status(approval["id"], "APPLIED")
    db.update_reflection_outcome(approval["id"], "approved")
    log(f"APPLIED DIAL CHANGE: {approval['dial_name']} {approval['dial_old_value']} -> {approval['dial_new_value']} (approval #{approval['id']})")
    db.log_event("INFO", f"Applied human-approved dial change: {approval['dial_name']} -> {approval['dial_new_value']}")


# -------------------------------------------------------------- risk / exits
def compute_intraday_equity():
    positions = db.get_positions()
    cash = db.get_meta("cash", 0.0)
    invested = 0.0
    for p in positions:
        price = data.get_latest_price(p["symbol"]) or p["avg_cost"]
        invested += p["shares"] * price
    return cash + invested


def _close_position(symbol, exit_price, exit_reason):
    pos = db.get_position(symbol)
    if not pos:
        return
    proceeds = pos["shares"] * exit_price
    cash = db.get_meta("cash", 0.0)
    db.set_meta("cash", cash + proceeds)
    pnl_dollars = proceeds - pos["shares"] * pos["avg_cost"]
    pnl_pct = (exit_price / pos["avg_cost"] - 1.0) * 100 if pos["avg_cost"] else 0.0
    db.record_closed_trade(symbol, pos["shares"], pos["avg_cost"], pos["opened_at"],
                            exit_price, db.now_iso(), pnl_dollars, pnl_pct, exit_reason)
    db.delete_position(symbol)
    log(f"AUTO-EXIT {exit_reason}: sold {symbol} {pos['shares']:.4f} sh @ {exit_price:.2f}, "
        f"P/L ${pnl_dollars:.2f} ({pnl_pct:.1f}%)")
    db.log_event("INFO" if pnl_dollars >= 0 else "WARN",
                 f"Closed {symbol} via {exit_reason}: P/L ${pnl_dollars:.2f} ({pnl_pct:.1f}%)")


def intraday_risk_tick():
    equity = compute_intraday_equity()
    state = risk.evaluate_breakers(equity)
    if state["flatten_required"]:
        flatten_to_cash("BLACK_SWAN_FLATTEN")
        return state
    dials = db.get_all_dials()
    hits = risk.check_catastrophe_stops_intraday(dials["catastrophe_stop_pct"])
    for hit in hits:
        _close_position(hit.symbol, hit.current_price, "CATASTROPHE_STOP")
    if hits:
        reflection.run_reflection_cycle()
    return state


def flatten_to_cash(reason):
    positions = db.get_positions()
    if not positions:
        return
    log(f"FLATTENING TO CASH ({reason}) -- selling all {len(positions)} position(s).")
    for p in positions:
        price = data.get_latest_price(p["symbol"]) or p["avg_cost"]
        _close_position(p["symbol"], price, reason)
    reflection.run_reflection_cycle()


# ------------------------------------------------------------- daily cycle
def daily_exits_tick():
    """Close-based exits: RANK_DROP and BELOW_TREND. Checked every day the
    worker runs (exit fast)."""
    dials = db.get_all_dials()
    positions = db.get_positions()
    if not positions:
        return
    price_data = data.get_daily_history_multi(config.UNIVERSE, lookback_days=800)
    spy_df = data.get_daily_history(config.BENCHMARK, lookback_days=800)
    ranking = strategy.build_ranking(price_data, spy_df, dials["trend_filter_len"])
    ranking_by_symbol = {r.symbol: r for r in ranking}
    entry_prices = {p["symbol"]: p["avg_cost"] for p in positions}
    current_prices = {p["symbol"]: ranking_by_symbol[p["symbol"]].price for p in positions
                       if p["symbol"] in ranking_by_symbol}
    signals = strategy.check_exits([p["symbol"] for p in positions], ranking_by_symbol,
                                    entry_prices, current_prices, dials["exit_rank_hysteresis"],
                                    dials["catastrophe_stop_pct"])
    for sig in signals:
        _close_position(sig.symbol, current_prices[sig.symbol], sig.reason)
    if signals:
        reflection.run_reflection_cycle()


def weekly_rebalance_tick():
    """Propose-only. Never fills a buy. Runs once on the Friday rebalance
    day, and is skipped entirely while halted or paused."""
    if db.get_meta("halted", False):
        log("Rebalance skipped: black-swan halt is active.")
        return
    if db.get_meta("pause_buys", False):
        log("Rebalance skipped: early brake has paused new buys.")
        return
    dials = db.get_all_dials()
    price_data = data.get_daily_history_multi(config.UNIVERSE, lookback_days=800)
    spy_df = data.get_daily_history(config.BENCHMARK, lookback_days=800)
    ranking = strategy.build_ranking(price_data, spy_df, dials["trend_filter_len"])
    targets = strategy.target_portfolio(ranking, config.TOP_N_HOLD, dials["position_cap_pct"])

    positions = db.get_positions()
    held_symbols = {p["symbol"] for p in positions}
    pending = db.get_pending_approvals(status="PENDING")
    pending_symbols = {p["symbol"] for p in pending if p["kind"] == "BUY"}

    new_targets = [t for t in targets if t.symbol not in held_symbols and t.symbol not in pending_symbols]
    if not new_targets:
        log("Weekly rebalance: no new buy candidates this week.")
        return

    cash = db.get_meta("cash", 0.0)
    already_committed = db.total_pending_buy_dollars()
    available = max(0.0, cash - already_committed)
    equity_now = cash + sum(p["shares"] * (data.get_latest_price(p["symbol"]) or p["avg_cost"]) for p in positions)

    for t in new_targets:
        if available <= 1.0:
            log(f"Weekly rebalance: no cash left to propose {t.symbol} (all cash already "
                f"committed to pending/open positions). Staying in cash for this slot.")
            continue
        desired = t.target_weight * equity_now
        proposed_dollars = min(desired, available)
        if proposed_dollars < 25.0:
            continue
        approval_id = db.create_pending_approval(
            kind="BUY", symbol=t.symbol, proposed_dollars=round(proposed_dollars, 2), rank=t.rank,
            rationale=(f"Weekly rebalance ({datetime.now(timezone.utc).date().isoformat()}): "
                       f"{t.symbol} ranked #{t.rank} by momentum, above its trend filter, and "
                       f"beats SPY. Conviction-by-rank target weight {t.target_weight:.1%} of "
                       f"equity (${proposed_dollars:.2f})."),
        )
        available -= proposed_dollars
        log(f"PROPOSED BUY: {t.symbol} ${proposed_dollars:.2f} (rank {t.rank}) -- approval #{approval_id}, awaiting human approval.")
        db.log_event("INFO", f"Proposed new buy: {t.symbol} ${proposed_dollars:.2f} (approval #{approval_id})")

    db.set_meta("last_rebalance_proposed_date", datetime.now(timezone.utc).date().isoformat())


def expire_stale_pending_buys(max_age_days=3):
    """A pending BUY proposal that sits unapproved for too long is stale --
    the market has moved on. We expire it rather than silently filling it
    later at a very different price. This does not touch DIAL_CHANGE
    proposals, which have no urgency."""
    pending = db.get_pending_approvals(status="PENDING")
    now = datetime.now(timezone.utc)
    for p in pending:
        if p["kind"] != "BUY":
            continue
        created = datetime.fromisoformat(p["created_at"])
        if (now - created).days >= max_age_days:
            db.mark_approval_status(p["id"], "EXPIRED")
            log(f"Expired stale pending buy approval #{p['id']} ({p['symbol']}), unapproved for {max_age_days}+ days.")


def daily_tick():
    today_str = datetime.now(timezone.utc).date().isoformat()
    if db.get_meta("last_daily_tick_date") == today_str:
        return
    log("--- daily tick start ---")
    try:
        expire_stale_pending_buys()
        daily_exits_tick()
        now = datetime.now(timezone.utc)
        if strategy.is_rebalance_day(now) and db.get_meta("last_rebalance_proposed_date") != today_str:
            weekly_rebalance_tick()
        equity = compute_intraday_equity()
        cash = db.get_meta("cash", 0.0)
        db.record_equity(today_str, equity, cash, equity - cash)
        brief.maybe_generate_daily_brief()
    finally:
        db.set_meta("last_daily_tick_date", today_str)
        log("--- daily tick end ---")


# ------------------------------------------------------------------- main
def run_forever():
    config.startup_safety_check()
    db.init_db()
    db.set_meta("worker_status", "running")
    db.set_meta("worker_started_at", db.now_iso())
    log(f"StockAdvisor worker starting. PAPER MODE ONLY. DB: {config.DB_PATH}")
    log(f"Universe: {', '.join(config.UNIVERSE)} | Benchmark: {config.BENCHMARK}")
    log(f"Dials: {db.get_all_dials()}")

    while True:
        try:
            db.set_meta("worker_last_heartbeat", db.now_iso())
            reconcile_approvals()
            intraday_risk_tick()
            daily_tick()
        except Exception as e:
            log(f"ERROR in worker loop: {e}")
            db.log_event("ERROR", f"Worker loop exception: {e}\n{traceback.format_exc()[:1000]}")
        time.sleep(config.INTRADAY_RISK_POLL_SEC)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        print("Worker stopped by user.", flush=True)
        sys.exit(0)
