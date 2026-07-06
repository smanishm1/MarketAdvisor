"""
reflection.py -- the self-tuning loop. Reviews the last batch of closed
trades every REFLECTION_EVERY_N_TRADES trades and proposes AT MOST ONE
change to ONE of the four tunable dials. The change is queued for human
approval exactly like a trade proposal -- it is never applied automatically.

Two modes:
- "fallback" (default): deterministic, rule-based, free, no dependencies.
- "llm": optional. Shells out to a local "hermes" CLI pointed at the Claude
  API. Costs API money and requires ANTHROPIC_API_KEY. If it's unavailable
  or errors, we fail safe by falling back to the rule-based mode rather
  than blocking the reflection cycle.

Guardrails baked in here (not just documented -- enforced in code):
- HOLD is the default outcome. Every rule requires a real, named trigger to
  fire before a change is proposed.
- Below REFLECTION_EVERY_N_TRADES closed trades total, always HOLD.
- Never re-propose a dial value the human already rejected (db.was_dial_change_rejected).
- Every proposed value is clamped to config.DIAL_BOUNDS no matter which
  mode produced it.
- The structure (universe, sizing model, position count, cadence) is not
  representable here at all -- there is no code path that can touch it.
"""
import json
import subprocess
from typing import Optional

import config
import db


def _clamp(dial_name: str, value: float) -> float:
    lo, hi = config.DIAL_BOUNDS[dial_name]
    return max(lo, min(hi, value))


def _batch_stats(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    reason_counts = {}
    for t in trades:
        reason_counts[t["exit_reason"]] = reason_counts.get(t["exit_reason"], 0) + 1
    below_trend_trades = [t for t in trades if t["exit_reason"] == "BELOW_TREND"]
    total_abs_pnl = sum(abs(t["pnl_dollars"]) for t in trades) or 1.0
    max_single_share = max((abs(t["pnl_dollars"]) / total_abs_pnl for t in trades), default=0.0)
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_pnl_pct": sum(t["pnl_pct"] for t in trades) / n,
        "reason_counts": reason_counts,
        "reason_fractions": {k: v / n for k, v in reason_counts.items()},
        "below_trend_avg_pnl_pct": (sum(t["pnl_pct"] for t in below_trend_trades) / len(below_trend_trades)
                                     if below_trend_trades else None),
        "max_single_trade_pnl_share": max_single_share,
    }


def rule_based_reflection(trades: list, dials: dict, rejected_check) -> dict:
    """Deterministic fallback. Returns a dict:
    {'decision': 'HOLD', 'rationale': str} or
    {'decision': 'PROPOSE', 'dial_name': str, 'new_value': float, 'rationale': str}
    `rejected_check(dial_name, value) -> bool` lets us honor the
    never-re-propose-a-rejection rule before returning."""
    stats = _batch_stats(trades)
    n = stats.get("n", 0)
    if n < config.REFLECTION_EVERY_N_TRADES:
        return {"decision": "HOLD", "rationale": f"Only {n} closed trades in this batch; "
                                                   f"minimum sample for any change is {config.REFLECTION_EVERY_N_TRADES}. Holding."}

    rf = stats["reason_fractions"]

    candidates = []

    # 1. Stops firing very often -> consider loosening the stop.
    if rf.get("CATASTROPHE_STOP", 0.0) >= 0.4:
        new_val = _clamp("catastrophe_stop_pct", dials["catastrophe_stop_pct"] + 0.02)
        candidates.append(("catastrophe_stop_pct", new_val,
            f"{rf['CATASTROPHE_STOP']:.0%} of the last {n} closed trades exited via the "
            f"catastrophe stop. That is a high fraction -- proposing to loosen the stop from "
            f"{dials['catastrophe_stop_pct']:.0%} to {new_val:.0%} to reduce the chance of being "
            f"whipsawed out of names that would have recovered. Backtest OOS result is the "
            f"deciding factor, not this observation alone."))

    # 2. Trend-filter exits are common AND those trades were, on balance, profitable
    #    -> the trend filter may be cutting winners short on short-term wobbles.
    if rf.get("BELOW_TREND", 0.0) >= 0.5 and (stats["below_trend_avg_pnl_pct"] or 0) > 0:
        new_val = _clamp("trend_filter_len", dials["trend_filter_len"] + 20)
        candidates.append(("trend_filter_len", new_val,
            f"{rf['BELOW_TREND']:.0%} of the last {n} closed trades exited on the trend filter, "
            f"and those exits averaged a POSITIVE {stats['below_trend_avg_pnl_pct']:.1f}% return -- "
            f"a sign the trend filter may be reacting to short-term noise rather than a real "
            f"trend break. Proposing to lengthen the filter from {dials['trend_filter_len']:.0f} "
            f"to {new_val:.0f} days for a smoother signal."))

    # 3. Rank-drop exits dominate and overall win rate is weak -> maybe too little patience.
    if rf.get("RANK_DROP", 0.0) >= 0.5 and stats["win_rate"] < 0.4:
        new_val = _clamp("exit_rank_hysteresis", dials["exit_rank_hysteresis"] + 1)
        candidates.append(("exit_rank_hysteresis", new_val,
            f"{rf['RANK_DROP']:.0%} of the last {n} closed trades exited on a rank drop, with an "
            f"overall win rate of only {stats['win_rate']:.0%}. Proposing to widen the exit-rank "
            f"hysteresis from top-{dials['exit_rank_hysteresis']:.0f} to top-{new_val:.0f} to give "
            f"holdings a bit more room before a rank wobble forces an exit."))

    # 4. One trade dominates the batch's P&L -> concentration risk; consider trimming the cap.
    if stats["max_single_trade_pnl_share"] >= 0.5 and n >= config.REFLECTION_EVERY_N_TRADES:
        new_val = _clamp("position_cap_pct", dials["position_cap_pct"] - 0.03)
        candidates.append(("position_cap_pct", new_val,
            f"A single trade accounted for {stats['max_single_trade_pnl_share']:.0%} of the total "
            f"absolute P&L across the last {n} closed trades -- more concentration risk than is "
            f"comfortable for a capital-preservation-first system. Proposing to trim the per-name "
            f"cap from {dials['position_cap_pct']:.0%} to {new_val:.0%}."))

    for dial_name, new_val, rationale in candidates:
        if abs(new_val - dials[dial_name]) < 1e-9:
            continue  # clamped back to the same value; not a real change
        if rejected_check(dial_name, new_val):
            continue  # human already said no to this exact value
        return {"decision": "PROPOSE", "dial_name": dial_name, "new_value": new_val, "rationale": rationale}

    return {"decision": "HOLD", "rationale": (
        f"Reviewed the last {n} closed trades (win rate {stats['win_rate']:.0%}, exit mix "
        f"{stats['reason_counts']}); no trigger condition was met (or the only candidates were "
        f"previously rejected by the human). Holding is the correct default on a sample this size.")}


def llm_reflection(trades: list, dials: dict, reflection_history: list,
                    rejected_check) -> Optional[dict]:
    """Optional LLM brain via the Hermes CLI pointed at the Claude API.
    Returns None (caller should fall back to rule-based) on any failure --
    missing CLI, bad JSON, timeout, etc. This function must never raise."""
    try:
        prompt = _build_llm_prompt(trades, dials, reflection_history)
        proc = subprocess.run(
            ["hermes", "run", "--model", "claude", "--json"],
            input=prompt, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            db.log_event("WARN", f"LLM reflection (hermes) exited {proc.returncode}: {proc.stderr[:300]}")
            return None
        parsed = json.loads(proc.stdout.strip())
        decision = parsed.get("decision")
        if decision == "HOLD":
            return {"decision": "HOLD", "rationale": parsed.get("rationale", "LLM held with no rationale given.")}
        if decision == "PROPOSE":
            dial_name = parsed.get("dial_name")
            new_value = parsed.get("new_value")
            if dial_name not in config.TUNABLE_DIAL_NAMES:
                db.log_event("WARN", f"LLM proposed an out-of-scope dial '{dial_name}'; ignoring, falling back.")
                return None
            new_value = _clamp(dial_name, float(new_value))
            if rejected_check(dial_name, new_value):
                return {"decision": "HOLD", "rationale": (
                    f"LLM proposed {dial_name}={new_value}, but this exact value was already "
                    f"rejected by the human previously. Holding instead per the never-re-propose rule.")}
            return {"decision": "PROPOSE", "dial_name": dial_name, "new_value": new_value,
                     "rationale": parsed.get("rationale", "")}
        return None
    except FileNotFoundError:
        db.log_event("WARN", "LLM reflection mode is enabled but the 'hermes' CLI was not found on PATH. "
                              "Falling back to rule-based reflection for this cycle.")
        return None
    except Exception as e:
        db.log_event("WARN", f"LLM reflection failed ({e}); falling back to rule-based reflection.")
        return None


def _build_llm_prompt(trades: list, dials: dict, reflection_history: list) -> str:
    payload = {
        "task": "Review the last batch of closed paper trades from a mechanical sector-rotation "
                "strategy and either HOLD or propose exactly one bounded change to exactly one "
                "tunable dial. You may ONLY touch: trend_filter_len, exit_rank_hysteresis, "
                "catastrophe_stop_pct, position_cap_pct. You may never change the universe, "
                "position count, sizing model, or rebalance cadence. Never re-propose a dial "
                "value present in the rejected_changes history below. Respond with strict JSON: "
                '{"decision": "HOLD"|"PROPOSE", "dial_name": str|null, "new_value": number|null, '
                '"rationale": str}',
        "current_dials": dials,
        "dial_bounds": config.DIAL_BOUNDS,
        "closed_trades_batch": trades,
        "past_reflection_history": reflection_history,
    }
    return json.dumps(payload)


def run_reflection_cycle():
    """Call this from the worker after any trade closes. No-ops unless
    enough new closed trades have accumulated since the last reflection
    checkpoint. Writes exactly one append-only reflection_log row per call
    that actually evaluates a batch (HOLD rows count too -- 'no change' is
    a logged decision, not silence)."""
    last_checkpoint = db.get_meta("last_reflection_trade_id", 0)
    current_max = db.max_trade_id()
    n_new = current_max - last_checkpoint
    if n_new < config.REFLECTION_EVERY_N_TRADES:
        return None

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE id > ? AND id <= ? ORDER BY id ASC",
        (last_checkpoint, current_max),
    ).fetchall()
    batch = [dict(r) for r in rows]
    dials = db.get_all_dials()

    mode = config.REFLECTION_MODE
    result = None
    if mode == "llm":
        history = db.get_reflection_log(limit=20)
        result = llm_reflection(batch, dials, history, db.was_dial_change_rejected)
    if result is None:
        mode = "fallback"
        result = rule_based_reflection(batch, dials, db.was_dial_change_rejected)

    db.set_meta("last_reflection_trade_id", current_max)

    if result["decision"] == "HOLD":
        db.log_reflection(n_new, "HOLD", result["rationale"], mode)
        db.log_event("INFO", f"Reflection cycle: HOLD. {result['rationale']}")
        return {"decision": "HOLD"}

    dial_name = result["dial_name"]
    new_value = result["new_value"]
    old_value = dials[dial_name]
    approval_id = db.create_pending_approval(
        kind="DIAL_CHANGE", dial_name=dial_name, dial_old_value=old_value,
        dial_new_value=new_value, rationale=result["rationale"],
    )
    db.log_reflection(n_new, "PROPOSE", result["rationale"], mode,
                       dial_name=dial_name, old_value=old_value, new_value=new_value,
                       approval_id=approval_id, outcome="pending")
    db.log_event("INFO", f"Reflection cycle: PROPOSE {dial_name} {old_value} -> {new_value} (approval #{approval_id})")
    return {"decision": "PROPOSE", "approval_id": approval_id, "dial_name": dial_name, "new_value": new_value}
