"""Reflection cycle.

Two modes, both of which PROPOSE exactly one strategy variable change for human
approval (they never write strategy.yaml directly — the worker applies it once a
human approves in the dashboard):

  --fallback   deterministic rule (works with no Hermes installed)
  --hermes     shells out to the `hermes` CLI for a smarter hypothesis

    python -m hermes_trading.reflect --fallback
    python -m hermes_trading.reflect --hermes
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
from typing import Any

from rich.console import Console

from . import approval, db, memory
from .config import dump_strategy, load_goal, load_strategy
from .paths import HYPOTHESES_FILE, load_env
from .score import score

console = Console()

# Tuning dials the agent may propose changes to — by strategy type. Everything
# NOT listed (universe, sizing model, max_positions, rebalance, …) is locked: the
# agent can tune the strategy, never redefine what it is.
ALLOWED_VARS_RSI = {
    "entry.threshold",
    "entry.period",
    "stop_loss_pct",
    "take_profit_pct",
    "position_size_r",
}
ALLOWED_VARS_SRSR = {
    "trend_sma_days",
    "exit_rank_n",
    "catastrophe_stop_pct",
    "position_notional_cap_pct",
    "stock_notional_cap_pct",
}
# hold_top_n is intentionally NOT tunable by the agent: it's a structural risk choice
# (how concentrated the book is), and it's mirrored by max_positions — see config.load_strategy.


def allowed_vars(strat: dict[str, Any]) -> set[str]:
    if strat.get("type") == "relative_strength_rotation":
        return ALLOWED_VARS_SRSR
    return ALLOWED_VARS_RSI


# ---- helpers ---------------------------------------------------------------


def _get(strat: dict[str, Any], dotted: str) -> Any:
    node: Any = strat
    for part in dotted.split("."):
        node = node[part]
    return node


def _set(strat: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = strat
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _bump_version(strat: dict[str, Any]) -> str:
    cur = int(str(strat.get("version", "01")))
    return f"{cur + 1:02d}"


def recent_closed_trades(conn: db.sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    trades = list(reversed(db.rows_to_dicts(rows)))
    for t in trades:  # decode the entry-context JSON so consumers see a dict
        try:
            t["context"] = json.loads(t["context"]) if t.get("context") else None
        except (json.JSONDecodeError, TypeError):
            t["context"] = None
    return trades


def equity_curve(conn: db.sqlite3.Connection) -> list[float]:
    rows = conn.execute("SELECT equity FROM equity ORDER BY ts").fetchall()
    return [r["equity"] for r in rows]


def build_change(strat: dict[str, Any], variable: str, new_value: Any) -> dict[str, Any]:
    """Return a proposal dict for a single-variable change."""
    if variable not in allowed_vars(strat):
        raise ValueError(f"variable '{variable}' is not in the allowed set")
    old_value = _get(strat, variable)
    new_strat = copy.deepcopy(strat)
    # coerce to the type of the existing value
    coerced = type(old_value)(new_value) if old_value is not None else new_value
    _set(new_strat, variable, coerced)
    to_version = _bump_version(new_strat)
    new_strat["version"] = to_version
    return {
        "from_version": str(strat.get("version", "01")).zfill(2),
        "to_version": to_version,
        "variable": variable,
        "old_value": old_value,
        "new_value": coerced,
        "proposed_yaml": dump_strategy(new_strat),
    }


# ---- fallback (deterministic) ---------------------------------------------


def propose_fallback(strat, goal, scored) -> tuple[str, Any, str]:
    """Return (variable, new_value, rationale) — exactly one change."""
    if strat.get("type") == "relative_strength_rotation":
        return _propose_fallback_srsr(strat, goal, scored)
    return _propose_fallback_rsi(strat, goal, scored)


def _propose_fallback_rsi(strat, goal, scored) -> tuple[str, Any, str]:
    if scored["max_drawdown"] > goal["max_drawdown"]:
        cur = float(_get(strat, "stop_loss_pct"))
        return (
            "stop_loss_pct",
            round(max(0.2, cur - 0.2), 2),
            f"drawdown {scored['max_drawdown']:.2%} exceeds max "
            f"{goal['max_drawdown']:.2%} — tighten the stop.",
        )
    if scored["realised_return"] < goal["target_return_30d"]:
        cur = float(_get(strat, "entry.threshold"))
        return (
            "entry.threshold",
            round(cur + 2, 2),
            f"return {scored['realised_return']:.2%} below target "
            f"{goal['target_return_30d']:.2%} — loosen entry to trade more often.",
        )
    return ("none", None, "meeting return and drawdown goals — no change warranted.")


def _propose_fallback_srsr(strat, goal, scored) -> tuple[str, Any, str]:
    if scored["max_drawdown"] > goal["max_drawdown"]:
        cur = int(_get(strat, "trend_sma_days"))
        return (
            "trend_sma_days",
            max(50, cur - 20),
            f"drawdown {scored['max_drawdown']:.2%} exceeds max "
            f"{goal['max_drawdown']:.2%} — shorten the trend filter to de-risk sooner.",
        )
    if scored["realised_return"] < goal["target_return_30d"]:
        cur = int(_get(strat, "exit_rank_n"))
        return (
            "exit_rank_n",
            cur + 1,
            f"return {scored['realised_return']:.2%} below target "
            f"{goal['target_return_30d']:.2%} — widen rank hysteresis to hold winners longer.",
        )
    return ("none", None, "meeting return and drawdown goals — no change warranted.")


# ---- hermes (subprocess) ---------------------------------------------------


def _memory_block(mem: dict[str, Any] | None) -> str:
    """Cross-version context for the prompt: what was tried before and how it went.

    This is what keeps reflections coherent from strategy to strategy — the brain
    sees the lineage of past changes (and their outcomes) instead of judging each
    tick of history in isolation and re-proposing something already rejected.
    """
    if not mem:
        return ""
    lineage = [
        {
            "change": f"{c['variable']}: {c['old_value']} -> {c['new_value']}",
            "versions": f"v{c['from_version']} -> v{c['to_version']}",
            "outcome": c["status"],  # applied | rejected (by the human)
            "rationale": (c.get("rationale") or "")[:140],
        }
        for c in mem.get("strategy_lineage", [])
    ]
    reflections = [
        {
            "decision": r.get("decision", "proposed"),
            "variable": r.get("variable"),
            "rationale": (r.get("rationale") or "")[:140],
        }
        for r in mem.get("reflections", [])
    ]
    return (
        f"Strategy change history (your past proposals and the human's verdict):\n"
        f"{json.dumps(lineage, indent=2)}\n\n"
        f"Performance by strategy version (closed trades opened under each):\n"
        f"{json.dumps(mem.get('version_performance', []), indent=2)}\n\n"
        f"Recent reflection decisions (including holds):\n"
        f"{json.dumps(reflections, indent=2)}\n\n"
        "Use this history: do NOT re-propose a change the human rejected or one that "
        "made a version perform worse, and do not ping-pong a variable back and forth.\n\n"
    )


def _hermes_prompt(strat, goal, trades, scored, mem: dict[str, Any] | None = None) -> str:
    return (
        "You are the reflection brain of a paper-trading agent. Propose EXACTLY ONE "
        "change to ONE strategy variable, using the scientific method.\n\n"
        f"Allowed variables (change only one): {sorted(allowed_vars(strat))}\n\n"
        f"Goal:\n{json.dumps(goal, indent=2)}\n\n"
        f"Current strategy:\n{json.dumps(strat, indent=2)}\n\n"
        f"Score of recent trades:\n{json.dumps(scored, indent=2)}\n\n"
        + _memory_block(mem)
        + f"Last {len(trades)} closed trades (pnl_pct, exit_reason, entry context):\n"
        + json.dumps(
            [
                {
                    "symbol": t.get("symbol"),
                    "pnl_pct": t.get("pnl_pct"),
                    "exit_reason": t.get("exit_reason"),
                    "version": t.get("strategy_version"),
                    "context": t.get("context"),
                }
                for t in trades
            ],
            indent=2,
        )
        + "\n\nHolding (no change) is a valid and often-correct answer — do NOT tweak if the "
        "strategy is meeting its goals or the evidence is weak. Changing a variable off a small, "
        "noisy sample is overfitting.\n"
        "Respond with ONLY a single JSON object, no prose. To change one variable:\n"
        '{"variable": "<one of the allowed>", "new_value": <number>, "rationale": "<one sentence>"}\n'
        'To hold (recommended unless something is clearly off): '
        '{"variable": "none", "rationale": "<why no change>"}\n'
    )


def _extract_json(text: str) -> dict[str, Any]:
    start = text.rfind("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in hermes output:\n{text[:400]}")
    return json.loads(text[start : end + 1])


def propose_hermes(strat, goal, trades, scored, mem=None) -> tuple[str, Any, str]:
    cmd = os.environ.get("HERMES_CMD", "hermes")
    # Headless prompt flag. Hermes v0.17 uses -z; override via env if a future
    # version changes it (e.g. HERMES_PROMPT_FLAG=--print).
    flag = os.environ.get("HERMES_PROMPT_FLAG", "-z")
    prompt = _hermes_prompt(strat, goal, trades, scored, mem)
    try:
        proc = subprocess.run(
            [cmd, flag, prompt],
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = proc.stdout.strip() or proc.stderr.strip()
        parsed = _extract_json(out)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"'{cmd}' not found — install Hermes (Phase 6) or set HERMES_CMD"
        ) from exc
    var = parsed.get("variable")
    if var in (None, "none", "hold"):
        return "none", None, parsed.get("rationale", "within spec — holding")
    if var not in allowed_vars(strat):
        raise ValueError(f"hermes proposed disallowed variable '{var}'")
    return var, parsed.get("new_value"), parsed.get("rationale", "hermes hypothesis")


# ---- driver ----------------------------------------------------------------

# Below this many closed trades, reflection holds — too small a sample to judge.
MIN_SAMPLE_TRADES = 0


def _record_hold(conn, source: str, scored: dict, rationale: str, announce: bool) -> dict:
    """Record a 'no change' decision — no proposal is queued."""
    db.set_meta(conn, "last_reflection_note", f"hold — {rationale}")
    db.set_meta(conn, "last_reflection_ts", str(db.now()))
    conn.commit()
    HYPOTHESES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HYPOTHESES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": db.now(), "source": source, "score": scored,
            "variable": "none", "decision": "hold", "rationale": rationale,
        }) + "\n")
    if announce:
        console.print(f"[cyan]No change recommended[/] — {rationale}")
    return {"hold": True, "rationale": rationale, "source": source}


def propose_once(mode: str, *, announce: bool = False) -> dict | None:
    """Run one reflection: propose ONE variable change and queue it for approval.

    Opens its own DB connection (safe to call from a worker background thread).
    Returns the proposal dict (including its pending-row 'id'), or None on failure.
    """
    load_env()
    db.init_db()
    conn = db.connect()
    try:
        goal = load_goal()
        strat = load_strategy()
        trades = recent_closed_trades(conn)
        scored = score(trades, goal, equity_curve(conn))
        source = "hermes" if mode == "hermes" else "fallback"

        # too small a sample to judge -> hold (never tweak on noise)
        if len(trades) < MIN_SAMPLE_TRADES:
            return _record_hold(
                conn, source, scored,
                f"only {len(trades)} closed trade(s) — too few to judge; holding.",
                announce,
            )

        if mode == "hermes":
            try:
                mem = memory.build(conn)
                variable, new_value, rationale = propose_hermes(strat, goal, trades, scored, mem)
                source = "hermes"
            except Exception as exc:  # noqa: BLE001 — never crash an autonomous run
                console.print(
                    f"[yellow]Hermes reflection failed ({exc}); "
                    f"using deterministic fallback instead.[/]"
                )
                variable, new_value, rationale = propose_fallback(strat, goal, scored)
                source = "fallback"
        else:
            variable, new_value, rationale = propose_fallback(strat, goal, scored)

        # strategy is within spec / no evidence to act -> hold, queue nothing
        if variable in (None, "none"):
            return _record_hold(conn, source, scored, rationale, announce)

        change = build_change(strat, variable, new_value)
        change["source"] = source
        change["rationale"] = rationale
        prop_id = approval.propose_strategy(conn, change)
        db.set_meta(conn, "last_reflection_note",
                    f"proposed {variable}: {change['old_value']} -> {change['new_value']}")
        db.set_meta(conn, "last_reflection_ts", str(db.now()))
        conn.commit()
        change["id"] = prop_id

        # append a hypothesis record for the audit trail
        HYPOTHESES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HYPOTHESES_FILE.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": db.now(),
                        "source": source,
                        "score": scored,
                        "variable": variable,
                        "old_value": change["old_value"],
                        "new_value": change["new_value"],
                        "rationale": rationale,
                        "pending_id": prop_id,
                    }
                )
                + "\n"
            )

        if announce:
            console.print(
                f"[bold green]Proposed[/] ({source}) #{prop_id}: "
                f"[cyan]{variable}[/] {change['old_value']} -> {change['new_value']} "
                f"(v{change['from_version']} -> v{change['to_version']})\n"
                f"  rationale: {rationale}\n"
                f"  [yellow]Awaiting your approval in the dashboard.[/]"
            )
        return change
    finally:
        conn.close()


def reflect(mode: str) -> None:
    """CLI driver — run one reflection and print the result."""
    if propose_once(mode, announce=True) is None:
        console.print("[red]No proposal was generated.[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description="reflection cycle")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fallback", action="store_true", help="deterministic rule")
    group.add_argument("--hermes", action="store_true", help="use the hermes CLI")
    args = parser.parse_args()
    reflect("hermes" if args.hermes else "fallback")


if __name__ == "__main__":
    main()
