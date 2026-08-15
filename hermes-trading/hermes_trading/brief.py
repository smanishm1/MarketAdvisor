"""Morning market brief: a once-a-day pre-open snapshot for the dashboard.

Composed deterministically from data the worker already has (no API calls, no
cost): where SPY sits vs its trend, how the universe ranks, how holdings are
doing, what's next in line, what the risk brakes say, and what changed since
yesterday (closed trades, reflection decisions, pending approvals).

The worker generates one automatically on its first tick after BRIEF_HOUR each
day; the dashboard's "Brief now" button regenerates on demand. One row per date
in the `briefs` table (regenerating overwrites the same date).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

from . import approval, db, memory, paper_broker, srsr

# generate the daily brief on the first tick at/after this local hour ("morning")
BRIEF_HOUR = 8


def _fmt_pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def compose(conn: sqlite3.Connection, cfg: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Build the brief from prefetched daily data ({sym: {closes, last, ...}})."""
    universe = list(cfg.get("universe", []))
    benchmark = cfg.get("benchmark", "SPY")
    stocks = set(cfg.get("stocks") or [])
    top_n = int(cfg.get("hold_top_n", 3))
    exit_rank = int(cfg.get("exit_rank_n", top_n + 1))

    prices = {s: data[s]["closes"] for s in universe if s in data}
    bench = data[benchmark]["closes"]
    decision = srsr.evaluate(prices, bench, cfg)
    ranked = sorted(decision.scores, key=lambda s: decision.scores[s], reverse=True)
    rank_of = {s: i + 1 for i, s in enumerate(ranked)}

    # --- market ---------------------------------------------------------------
    spy_last = bench[-1]
    spy_day = (bench[-1] / bench[-2] - 1.0) if len(bench) >= 2 and bench[-2] else None
    trend_n = int(cfg.get("trend_sma_days", 200))
    spy_sma = srsr.sma(bench, trend_n)
    spy_above = spy_sma is not None and spy_last > spy_sma
    market_lines = [
        f"{benchmark} ${spy_last:,.2f} ({_fmt_pct(spy_day)} on the day), "
        f"{'above' if spy_above else 'BELOW'} its {trend_n}-day trend"
        + (f" by {_fmt_pct(spy_last / spy_sma - 1.0)}" if spy_sma else "") + ".",
        f"{benchmark} momentum score {_fmt_pct(decision.benchmark_score)} — "
        f"{len(decision.eligible)} of {len(universe)} names clear the dual-momentum gate.",
    ]

    # --- rankings ---------------------------------------------------------------
    def tag(s: str) -> str:
        return f"{s}*" if s in stocks else s

    rank_lines = [
        f"#{i + 1} {tag(s)} {_fmt_pct(decision.scores[s])}"
        + ("" if s in decision.eligible else " (filtered out)")
        for i, s in enumerate(ranked[:max(top_n + 2, 6)])
    ]
    if stocks:
        rank_lines.append("* = individual stock (tighter cap, wider stop, limited slots)")

    # --- holdings ---------------------------------------------------------------
    open_pos = paper_broker.open_positions(conn)
    held = [p["symbol"] for p in open_pos]
    holding_lines: list[str] = []
    for p in open_pos:
        sym = p["symbol"]
        last = data.get(sym, {}).get("last", p["entry_price"])
        unreal = (last / p["entry_price"] - 1.0) if p["entry_price"] else 0.0
        r = rank_of.get(sym)
        near_exit = r is not None and r > exit_rank - 1
        note = " — near the exit band" if near_exit else ""
        if sym not in decision.eligible:
            note = " — FAILED a filter (exit due)"
        holding_lines.append(
            f"{tag(sym)} {_fmt_pct(unreal)} since entry · rank #{r or '—'} of "
            f"{len(ranked)} · stop ${p['stop_price']:,.2f}{note}"
        )
    if not holding_lines:
        holding_lines.append(f"No open positions — {top_n} slots in cash.")

    # --- watchlist ----------------------------------------------------------------
    next_up = [s for s in decision.targets if s not in held]
    watch_lines = (
        [f"Next in line at the rebalance: {', '.join(tag(s) for s in next_up)}."]
        if next_up
        else ["Holdings already match the target book — no buys expected."]
    )
    today = dt.date.today()
    next_reb = today + dt.timedelta(days=(4 - today.weekday()) % 7)
    watch_lines.append(
        "Rebalance due today (Friday)." if next_reb == today
        else f"Next rebalance: Friday {next_reb.isoformat()}."
    )

    # --- risk ---------------------------------------------------------------------
    risk_lines: list[str] = []
    if db.get_meta(conn, "risk_off", "0") == "1":
        risk_lines.append("⛔ RISK-OFF: " + (db.get_meta(conn, "risk_off_reason", "") or ""))
    if db.get_meta(conn, "buys_halted", "0") == "1":
        risk_lines.append("⚠ Buys paused: " + (db.get_meta(conn, "buys_halted_reason", "") or ""))
    if not risk_lines:
        risk_lines.append("No brakes active — normal operation.")

    # --- recap (what changed since yesterday) ---------------------------------------
    recap_lines: list[str] = []
    cutoff = db.now() - 86400
    opened = conn.execute(
        "SELECT symbol, entry_price FROM trades WHERE entry_ts >= ? ORDER BY entry_ts",
        (cutoff,),
    ).fetchall()
    for t in opened:
        recap_lines.append(f"Opened {tag(t['symbol'])} @ ${t['entry_price']:,.2f}.")
    closed = conn.execute(
        "SELECT symbol, pnl_pct, exit_reason FROM trades "
        "WHERE status='closed' AND exit_ts >= ? ORDER BY exit_ts",
        (cutoff,),
    ).fetchall()
    for t in closed:
        recap_lines.append(
            f"Closed {t['symbol']} {_fmt_pct(t['pnl_pct'])} ({t['exit_reason']})."
        )
    n_pt = len(approval.list_pending_trades(conn))
    n_ps = len(approval.list_pending_strategy(conn))
    if n_pt or n_ps:
        recap_lines.append(
            f"Awaiting your approval: {n_pt} trade(s), {n_ps} strategy change(s)."
        )
    note = db.get_meta(conn, "last_reflection_note", "") or ""
    if note:
        recap_lines.append(f"Last reflection: {note}")
    vp = [v for v in memory.version_performance(conn) if v["version"] == cfg.get("version")]
    if vp:
        v = vp[0]
        recap_lines.append(
            f"Strategy v{v['version']} so far: {v['n_trades']} closed trades, "
            f"{v['win_rate']:.0%} winners, ${v['total_pnl']:,.2f} realised."
        )
    if not recap_lines:
        recap_lines.append("Quiet 24h — no closed trades, nothing pending.")

    invested = f"{len(held)}/{top_n} slots filled"
    headline = (
        f"{'Risk-on' if spy_above else 'Risk-off tape'} — {benchmark} "
        f"{'above' if spy_above else 'below'} trend, "
        f"{len(decision.eligible)}/{len(universe)} names eligible, {invested}."
    )

    return {
        "date": today.isoformat(),
        "ts": db.now(),
        "headline": headline,
        "sections": [
            {"title": "Market", "lines": market_lines},
            {"title": "Rankings (momentum)", "lines": rank_lines},
            {"title": "Holdings", "lines": holding_lines},
            {"title": "Watch", "lines": watch_lines},
            {"title": "Risk", "lines": risk_lines},
            {"title": "Since yesterday", "lines": recap_lines},
        ],
    }


def store(conn: sqlite3.Connection, brief: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO briefs(date, ts, json) VALUES(?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET ts=excluded.ts, json=excluded.json",
        (brief["date"], brief["ts"], json.dumps(brief)),
    )
    db.set_meta(conn, "last_brief_date", brief["date"])
    conn.commit()


def latest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT json FROM briefs ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return json.loads(row["json"])
    except json.JSONDecodeError:
        return None


def due(conn: sqlite3.Connection) -> bool:
    """True once per day, from BRIEF_HOUR local time (the worker's auto-trigger)."""
    now = dt.datetime.now()
    if now.hour < BRIEF_HOUR:
        return False
    return db.get_meta(conn, "last_brief_date", "") != dt.date.today().isoformat()


def generate(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Fetch fresh daily data, compose, and store today's brief (on-demand path)."""
    from .adapters import equities
    from .config import load_strategy

    cfg = load_strategy()
    if cfg.get("type") != "relative_strength_rotation":
        return None
    symbols = list(cfg.get("universe", [])) + [cfg.get("benchmark", "SPY")]
    data = equities.fetch_daily(symbols, "2y")
    own = conn is None
    if own:
        conn = db.connect()
    try:
        b = compose(conn, cfg, data)
        store(conn, b)
        return b
    finally:
        if own:
            conn.close()
