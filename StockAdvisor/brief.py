"""
brief.py -- the morning brief. Composed once per weekday morning, kept as
an append-only history so today's brief can show deltas vs yesterday's
(equity change, leaders that moved in/out of the momentum top ranks, and
trades closed since).
"""
from datetime import datetime, timedelta, timezone

import config
import data
import db
import risk
import strategy


def _next_rebalance_date(today) -> str:
    days_ahead = (config.REBALANCE_WEEKDAY - today.weekday()) % 7
    if days_ahead == 0:
        # today IS rebalance day -- next one is next week
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).date().isoformat()


def _current_ranking():
    dials = db.get_all_dials()
    price_data = data.get_daily_history_multi(config.UNIVERSE, lookback_days=800)
    spy_df = data.get_daily_history(config.BENCHMARK, lookback_days=800)
    return strategy.build_ranking(price_data, spy_df, dials["trend_filter_len"])


def compute_equity_snapshot():
    positions = db.get_positions()
    cash = db.get_meta("cash", 0.0)
    prices = data.get_latest_prices([p["symbol"] for p in positions]) if positions else {}
    invested = 0.0
    holdings = []
    for p in positions:
        price = prices.get(p["symbol"]) or p["avg_cost"]
        market_value = p["shares"] * price
        invested += market_value
        unrealized_pnl = market_value - (p["shares"] * p["avg_cost"])
        unrealized_pnl_pct = (price / p["avg_cost"] - 1.0) * 100 if p["avg_cost"] else 0.0
        holdings.append({
            "symbol": p["symbol"], "shares": round(p["shares"], 4), "avg_cost": round(p["avg_cost"], 2),
            "price": round(price, 2), "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2), "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        })
    equity = cash + invested
    for h in holdings:
        h["weight_pct"] = round((h["market_value"] / equity * 100) if equity else 0.0, 2)
    return {"cash": round(cash, 2), "invested": round(invested, 2), "equity": round(equity, 2), "holdings": holdings}


def generate_brief() -> dict:
    today = datetime.now(timezone.utc)
    spy_state = risk.get_spy_state()
    ranking = _current_ranking()
    leaders = [{"symbol": r.symbol, "rank": r.rank, "momentum_pct": round((r.momentum or 0) * 100, 2),
                "qualifies": r.qualifies} for r in ranking[:5]]
    snapshot = compute_equity_snapshot()

    closed_since_yesterday = []
    conn = db.get_conn()
    yesterday_iso = (today - timedelta(days=1)).date().isoformat()
    for t in db.get_closed_trades(limit=50):
        if t["closed_at"][:10] >= yesterday_iso:
            closed_since_yesterday.append({
                "symbol": t["symbol"], "pnl_pct": round(t["pnl_pct"], 2),
                "pnl_dollars": round(t["pnl_dollars"], 2), "exit_reason": t["exit_reason"],
            })

    prev_brief = db.get_latest_brief()
    deltas = {"equity_change": None, "equity_change_pct": None, "leaders_in": [], "leaders_out": [],
              "trades_closed_since": closed_since_yesterday}
    if prev_brief:
        prev_equity = prev_brief["content"].get("equity_snapshot", {}).get("equity")
        if prev_equity:
            deltas["equity_change"] = round(snapshot["equity"] - prev_equity, 2)
            deltas["equity_change_pct"] = round((snapshot["equity"] / prev_equity - 1.0) * 100, 3)
        prev_leader_syms = {l["symbol"] for l in prev_brief["content"].get("momentum_leaders", [])}
        cur_leader_syms = {l["symbol"] for l in leaders}
        deltas["leaders_in"] = sorted(cur_leader_syms - prev_leader_syms)
        deltas["leaders_out"] = sorted(prev_leader_syms - cur_leader_syms)

    regime = "RISK-OFF (halted, flattened to cash)" if db.get_meta("halted", False) else (
        "CAUTIOUS (new buys paused)" if db.get_meta("pause_buys", False) else (
            "RISK-ON (SPY above its 200-day average)" if spy_state.above_200dma else
            "NEUTRAL (SPY below its 200-day average, but no brake triggered)"))

    content = {
        "date": today.date().isoformat(),
        "regime": regime,
        "spy": {"price": round(spy_state.price, 2), "day_return_pct": round(spy_state.day_return * 100, 2),
                "sma200": round(spy_state.sma200, 2) if spy_state.sma200 else None,
                "above_200dma": spy_state.above_200dma},
        "equity_snapshot": snapshot,
        "momentum_leaders": leaders,
        "next_rebalance": _next_rebalance_date(today),
        "deltas_vs_yesterday": deltas,
    }
    db.save_brief(content["date"], content)
    return content


def maybe_generate_daily_brief():
    """Idempotent: only actually composes a new brief once per weekday, the
    first time the worker ticks that day."""
    today = datetime.now(timezone.utc)
    if today.weekday() >= 5:
        return None  # weekend, markets closed -- no brief
    last_date = db.get_meta("last_brief_date")
    today_str = today.date().isoformat()
    if last_date == today_str:
        return None
    brief = generate_brief()
    db.set_meta("last_brief_date", today_str)
    db.log_event("INFO", f"Morning brief composed for {today_str}.")
    return brief
