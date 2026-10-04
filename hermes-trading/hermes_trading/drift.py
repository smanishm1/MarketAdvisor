"""Drift report: how far the live strategy has moved from the ORIGINAL design.

The self-tuning loop can't change *what* the strategy is, but step by step it can
change *how cautious or aggressive* it is. This module keeps that visible:

  * ``report()`` — every tracked dial (stop loss, position caps, rolling windows,
    dropout rank, slots, …): original v01 value vs live value, which way the change
    leans (more aggressive / more cautious), and who made it — a human edit or the
    agent's reflection loop (from the approval lineage).
  * ``refresh()`` — a backtest of the original design vs the live one (net of costs,
    full + out-of-sample + regimes), cached in ``meta`` and refreshed weekly by the
    worker or whenever the live version changes. Shown on the dashboard and in the
    morning brief.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from typing import Any

from . import db
from .config import load_strategy
from .review import load_baseline

REFRESH_DAYS = 7
META_KEY = "drift_backtest"

# (key, label, which way an INCREASE leans)
DIALS: list[tuple[str, str, str | None]] = [
    ("catastrophe_stop_pct",       "Stop loss below entry, ETF %",      "aggressive"),  # wider = more loss tolerated
    ("stock_catastrophe_stop_pct", "Stop loss below entry, stock %",    "aggressive"),
    ("position_notional_cap_pct",  "Position cap, ETF %",               "aggressive"),  # bigger = more concentrated
    ("stock_notional_cap_pct",     "Position cap, stock %",             "aggressive"),
    ("exit_rank_n",                "Dropout rank (sell when below)",    "aggressive"),  # holds fading names longer
    ("trend_sma_days",             "Trend filter length (days)",        "slower"),      # slower to exit AND re-enter
    ("momentum_lookbacks_days",    "Momentum rolling windows (days)",   None),
    ("momentum_weights",           "Momentum window weights",           None),
    ("hold_top_n",                 "Number of holdings (slots)",        "cautious"),    # more names = diversified
    ("sizing",                     "Sizing model",                      None),
    ("rank_power",                 "Weight concentration (rank power)", "aggressive"),
    ("universe",                   "Universe size",                     None),
    ("stocks",                     "Single stocks eligible",            "aggressive"),
    ("max_stock_positions",        "Max single-stock slots",            "aggressive"),
    ("earnings_blackout_days",     "Earnings blackout (days)",          "cautious"),
]
_SIZING_ORDER = {"equal_weight": 0, "inverse_vol": 0, "rank_weight": 1}   # 1 = more concentrated


def _val(cfg: dict[str, Any], key: str) -> Any:
    v = cfg.get(key)
    if key in ("universe", "stocks"):
        return len(v or [])
    if key == "momentum_weights" and v is None:
        return "equal"
    if key == "earnings_blackout_days" and v is None:
        return 0
    if key in ("stock_notional_cap_pct", "stock_catastrophe_stop_pct", "max_stock_positions") \
            and not cfg.get("stocks"):
        return "n/a"
    return v


def _direction(key: str, lean: str | None, old: Any, new: Any) -> str:
    if old == new:
        return ""
    if key == "sizing":
        a, b = _SIZING_ORDER.get(str(old), 0), _SIZING_ORDER.get(str(new), 0)
        return "more concentrated" if b > a else ("less concentrated" if b < a else "changed")
    if lean is None or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        if old == "n/a" and lean:   # a feature switched on (e.g. the stock sleeve)
            return "more aggressive" if lean == "aggressive" else "more cautious"
        return "changed"
    up = new > old
    if lean == "slower":
        return "slower to react" if up else "faster to react"
    if lean == "aggressive":
        return "more aggressive" if up else "more cautious"
    return "more cautious" if up else "more aggressive"


def _agent_changes(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Dials the reflection loop (not a human edit) has changed, with the versions."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT from_version, to_version, variable, old_value, new_value, source "
        "FROM pending_strategy WHERE status='applied' ORDER BY proposed_ts"
    ).fetchall():
        if r["source"] not in ("hermes", "fallback"):
            continue
        for key in [k.strip() for k in str(r["variable"]).split(",")]:
            if r["old_value"] == r["new_value"]:
                continue   # no-op bump (e.g. 30 -> 30)
            out.setdefault(key, []).append(
                f"v{r['from_version']}→v{r['to_version']} ({r['old_value']}→{r['new_value']})"
            )
    return out


def report(conn: sqlite3.Connection, current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or load_strategy()
    base = load_baseline()
    if base is None:
        return {"available": False}
    agent = _agent_changes(conn)
    rows = []
    for key, label, lean in DIALS:
        o, n = _val(base, key), _val(current, key)
        d = _direction(key, lean, o, n)
        rows.append({
            "key": key, "label": label, "original": o, "current": n, "changed": o != n,
            "direction": d, "by_agent": agent.get(key, []),
        })
    changed = [r for r in rows if r["changed"]]
    aggr = [r for r in changed if r["direction"] in ("more aggressive", "more concentrated")]
    caut = [r for r in changed if r["direction"] == "more cautious"]
    agent_rows = [r for r in changed if r["by_agent"]]
    agent_lean = {r["direction"] for r in agent_rows}
    verdict = (
        f"{len(changed)} of {len(rows)} settings differ from the original v{base['version']}: "
        f"{len(aggr)} lean more aggressive, {len(caut)} more cautious."
    )
    if agent_rows:
        verdict += (
            f" The reflection loop itself moved {len(agent_rows)} "
            f"({', '.join(r['label'] for r in agent_rows)})"
            + (" — all toward MORE AGGRESSIVE." if agent_lean <= {"more aggressive"} else ".")
        )
    return {
        "available": True,
        "baseline_version": base["version"],
        "live_version": current["version"],
        "rows": rows,
        "n_changed": len(changed),
        "n_aggressive": len(aggr),
        "n_cautious": len(caut),
        "agent_changed": [r["key"] for r in agent_rows],
        "verdict": verdict,
        "backtest": cached(conn),
    }


# ---- original-vs-live backtest (cached, refreshed weekly) --------------------------


def cached(conn: sqlite3.Connection) -> dict[str, Any] | None:
    raw = db.get_meta(conn, META_KEY, None)
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def refresh_due(conn: sqlite3.Connection, current: dict[str, Any] | None = None) -> bool:
    c = cached(conn)
    if not c or c.get("status") == "error" and time.time() - float(c.get("ts", 0)) > 3600:
        return True
    current = current or load_strategy()
    if str(c.get("live_version")) != str(current.get("version")):
        return True
    return time.time() - float(c.get("ts", 0)) > REFRESH_DAYS * 86400


def refresh() -> dict[str, Any]:
    """Backtest original vs live and cache it. Blocking — call from a thread."""
    from .backtest import compare

    current = load_strategy()
    base = load_baseline()
    try:
        r = compare(base, current)
        out = {
            "status": "done", "ts": time.time(), "date": dt.date.today().isoformat(),
            "live_version": current["version"], "baseline_version": base["version"],
            "start": r["start"], "end": r["end"], "oos_start": r["oos_start"],
            "original": r["current"], "live": r["proposed"],
            "original_oos": r["current_oos"], "live_oos": r["proposed_oos"],
            "regimes": {"original": r["regimes"]["current"], "live": r["regimes"]["proposed"]},
            "assumptions": r["assumptions"],
        }
    except Exception as exc:  # noqa: BLE001
        out = {"status": "error", "error": str(exc)[:300], "ts": time.time(),
               "live_version": current.get("version")}
    conn = db.connect()
    try:
        db.set_meta(conn, META_KEY, json.dumps(out))
        conn.commit()
    finally:
        conn.close()
    return out


def brief_lines(conn: sqlite3.Connection) -> list[str]:
    """Compact drift summary for the morning brief."""
    rep = report(conn)
    if not rep.get("available"):
        return ["No baseline configured (config/baseline.yaml)."]
    lines = [rep["verdict"]]
    for r in rep["rows"]:
        if r["changed"]:
            who = f" [agent: {'; '.join(r['by_agent'])}]" if r["by_agent"] else ""
            lines.append(f"{r['label']}: {r['original']} → {r['current']} ({r['direction']}){who}")
    bt = rep.get("backtest")
    if bt and bt.get("status") == "done":
        o, l = bt["original_oos"], bt["live_oos"]
        lines.append(
            f"Backtest, out-of-sample since {bt['oos_start']} (net): original Sharpe {o['sharpe']:.2f} / "
            f"CAGR {o['cagr']:+.1%} / maxDD {o['max_drawdown']:.1%} vs live Sharpe {l['sharpe']:.2f} / "
            f"CAGR {l['cagr']:+.1%} / maxDD {l['max_drawdown']:.1%} (as of {bt['date']})."
        )
    return lines
