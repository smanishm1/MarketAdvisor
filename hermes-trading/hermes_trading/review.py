"""Change review: no strategy change is approved blind.

Every strategy proposal — from the reflection loop or a human analysis — goes
through the same path:

    proposed -> auto-backtested -> a human approves/rejects WITH A REASON -> applied

  * **Backtested before approval.** The worker backtests each pending proposal in
    the background — current vs proposed vs the original v01 design, net of costs,
    over the full history, a held-out out-of-sample window and named market regimes.
    Approval is refused until that result exists (dashboard, Discord and API alike).
  * **A reason on every decision.** Approve and reject both require one line of
    reasoning. It's stored on the proposal, appended to ``state/decisions.jsonl``
    (audit log), and fed back to the reflection brain via the strategy lineage.
  * **No stale approvals.** If the live strategy changes after a proposal was
    drafted, the proposal is re-based onto the current config (its structured diff
    is re-applied) and re-backtested before it can be approved — so the human always
    judges the change against what's actually running. Changes already in effect
    are closed automatically.
  * **No no-op changes.** A "change" that alters nothing (e.g. 30 -> 30) is refused.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from typing import Any

import yaml

from . import approval, db
from .config import dump_strategy, load_strategy, load_strategy_file
from .paths import BASELINE_FILE, DECISIONS_FILE

MIN_REASON_CHARS = 8
MAX_REASON_CHARS = 280
RUNNING_STALE_SECONDS = 900            # a 'running' claim older than this is presumed dead
IGNORED_KEYS = {"version", "max_positions"}   # max_positions always mirrors hold_top_n

_run_lock = threading.Lock()           # one backtest runner per process


class NoOpChange(ValueError):
    """The proposed values equal the current ones — nothing would change."""


# ---- diffs & building proposals ---------------------------------------------


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[Any]]:
    """{key: [old, new]} for every setting that differs (ignoring version bookkeeping)."""
    keys = (set(old) | set(new)) - IGNORED_KEYS
    return {k: [old.get(k), new.get(k)] for k in sorted(keys) if old.get(k) != new.get(k)}


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]" if len(v) <= 8 else f"[{len(v)} items]"
    return "—" if v is None else str(v)


def _summary(changes: dict[str, list[Any]]) -> tuple[str, str, str]:
    """(variable label, old, new) for the card — one key reads like before."""
    if len(changes) == 1:
        (k, (o, n)), = changes.items()
        return k, _fmt(o), _fmt(n)
    return (", ".join(changes),
            "; ".join(f"{k}={_fmt(v[0])}" for k, v in changes.items()),
            "; ".join(f"{k}={_fmt(v[1])}" for k, v in changes.items()))


def build(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """A proposal dict applying `changes` ({key: new_value}) on top of `current`."""
    new = copy.deepcopy(current)
    new.update(copy.deepcopy(changes))
    new["max_positions"] = int(new.get("hold_top_n", new.get("max_positions", 3)))
    d = diff(current, new)
    if not d:
        raise NoOpChange("the proposed values equal the current settings — nothing would change")
    from_v = str(current.get("version", "01")).zfill(2)
    new["version"] = f"{int(from_v) + 1:02d}"
    var, old_s, new_s = _summary(d)
    return {
        "from_version": from_v,
        "to_version": new["version"],
        "variable": var,
        "old_value": old_s,
        "new_value": new_s,
        "proposed_yaml": dump_strategy(new),
        "changes_json": json.dumps(d),
    }


def propose(conn: sqlite3.Connection, changes: dict[str, Any], source: str, rationale: str) -> int:
    """Queue a (possibly multi-setting) change for review. Caller commits."""
    prop = build(load_strategy(), changes)
    prop["source"] = source
    prop["rationale"] = rationale
    return approval.propose_strategy(conn, prop)


# ---- state of a proposal ----------------------------------------------------


def fetch(conn: sqlite3.Connection, pid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM pending_strategy WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def backtest_of(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return json.loads(row["backtest_json"]) if row.get("backtest_json") else None
    except (json.JSONDecodeError, TypeError):
        return None


def bt_status(row: dict[str, Any]) -> str:
    """missing | running | done | error"""
    bt = backtest_of(row)
    if not bt:
        return "missing"
    return bt.get("status") or ("error" if "error" in bt else "done")


def _stuck(bt: dict[str, Any] | None) -> bool:
    return bool(bt) and bt.get("status") == "running" and \
        time.time() - float(bt.get("ts", 0)) > RUNNING_STALE_SECONDS


def is_stale(row: dict[str, Any], current: dict[str, Any] | None = None) -> bool:
    """True if the live strategy moved since this proposal was drafted."""
    current = current or load_strategy()
    return str(row.get("from_version", "")).zfill(2) != str(current.get("version", "")).zfill(2)


def can_approve(row: dict[str, Any], current: dict[str, Any] | None = None) -> tuple[bool, str]:
    if row.get("status") != "pending":
        return False, f"already {row.get('status')}"
    if is_stale(row, current):
        return False, ("the live strategy changed since this was proposed — it is being "
                       "re-based onto the current settings and re-backtested")
    st = bt_status(row)
    if st == "done":
        return True, ""
    if st == "running":
        return False, "the backtest is still running — its result must be shown before approval"
    if st == "error":
        return False, "the backtest failed — re-run it before approving"
    return False, "the backtest hasn't run yet (queued) — its result must be shown before approval"


# ---- re-basing stale proposals ---------------------------------------------------


def rebase(conn: sqlite3.Connection, row: dict[str, Any], current: dict[str, Any]) -> bool:
    """Re-apply a proposal's diff onto the current config. Returns True if it is
    pending again (awaiting a fresh backtest), False if it was closed out. Caller commits."""
    try:
        ch = json.loads(row.get("changes_json") or "null")
    except json.JSONDecodeError:
        ch = None
    if not ch:  # legacy single-variable proposal: recover the new value from its yaml
        try:
            ch = {row["variable"]: [None, (yaml.safe_load(row["proposed_yaml"]) or {}).get(row["variable"])]}
        except Exception:  # noqa: BLE001
            ch = None
    try:
        if not ch:
            raise NoOpChange("cannot recover the proposed change")
        prop = build(current, {k: v[1] for k, v in ch.items()})
    except NoOpChange as exc:
        conn.execute(
            "UPDATE pending_strategy SET status='rejected', resolved_ts=?, decided_via='system', "
            "decision_reason=? WHERE id=? AND status IN ('pending','approved')",
            (db.now(), f"auto-closed after the strategy moved: {exc}", row["id"]),
        )
        return False
    conn.execute(
        "UPDATE pending_strategy SET from_version=?, to_version=?, variable=?, old_value=?, "
        "new_value=?, proposed_yaml=?, changes_json=?, backtest_json=NULL, status='pending', "
        "decision_reason=NULL, decided_via=NULL, resolved_ts=NULL WHERE id=?",
        (prop["from_version"], prop["to_version"], prop["variable"], prop["old_value"],
         prop["new_value"], prop["proposed_yaml"], prop["changes_json"], row["id"]),
    )
    return True


# ---- backtest runner ---------------------------------------------------------------


def load_baseline() -> dict[str, Any] | None:
    return load_strategy_file(BASELINE_FILE) if BASELINE_FILE.exists() else None


def _claim(conn: sqlite3.Connection, pid: int, force: bool) -> bool:
    """Atomically mark a proposal's backtest as running (safe across processes)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, backtest_json FROM pending_strategy WHERE id=?", (pid,)
        ).fetchone()
        if not row or row["status"] != "pending":
            conn.rollback()
            return False
        st = bt_status(dict(row))
        bt = backtest_of(dict(row))
        if st == "running" and not _stuck(bt):
            conn.rollback()
            return False
        if st in ("done", "error") and not force:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE pending_strategy SET backtest_json=? WHERE id=?",
            (json.dumps({"status": "running", "ts": time.time()}), pid),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def run_backtest(pid: int, force: bool = False) -> dict[str, Any] | None:
    """Backtest one proposal (re-basing it first if stale) and store the result.
    Blocking — call from a thread. Returns the result, or None if nothing ran."""
    from .backtest import compare

    conn = db.connect()
    try:
        current = load_strategy()
        row = fetch(conn, pid)
        if not row or row["status"] != "pending":
            return None
        if is_stale(row, current):
            kept = rebase(conn, row, current)
            conn.commit()
            if not kept:
                return None
            row = fetch(conn, pid)
        if not _claim(conn, pid, force):
            return None
        try:
            proposed = yaml.safe_load(row["proposed_yaml"]) or {}
            if proposed.get("type") != "relative_strength_rotation":
                raise ValueError("backtest only supported for the rotation strategy")
            result = compare(current, proposed, baseline_cfg=load_baseline())
            result.update(status="done", ts=time.time(), base_version=current["version"])
        except Exception as exc:  # noqa: BLE001 — surfaced on the card
            result = {"status": "error", "error": str(exc)[:300], "ts": time.time()}
        conn.execute(
            "UPDATE pending_strategy SET backtest_json=? WHERE id=? AND status='pending'",
            (json.dumps(result), pid),
        )
        conn.commit()
        return result
    finally:
        conn.close()


def process_pending() -> int:
    """Backtest every pending proposal that needs it (missing, stale or stuck).
    Returns how many ran. Safe to call every worker tick from a thread."""
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        conn = db.connect()
        try:
            current = load_strategy()
            todo = [
                r for r in approval.list_pending_strategy(conn)
                if is_stale(r, current) or bt_status(r) == "missing" or _stuck(backtest_of(r))
            ]
        finally:
            conn.close()
        return sum(1 for r in todo if run_backtest(int(r["id"])) is not None)
    finally:
        _run_lock.release()


# ---- decisions ---------------------------------------------------------------------


def _one_line(reason: str | None) -> str:
    return " ".join((reason or "").split())[:MAX_REASON_CHARS]


def decide(conn: sqlite3.Connection, pid: int, action: str, reason: str | None,
           via: str) -> tuple[bool, str]:
    """Approve or reject a strategy proposal. Returns (ok, status-or-error-message).

    Both verdicts need a one-line reason; approval also needs a finished,
    non-stale backtest (see can_approve)."""
    if action not in ("approve", "reject"):
        return False, f"unknown action '{action}'"
    text = _one_line(reason)
    if len(text) < MIN_REASON_CHARS:
        return False, f"a one-line reason is required (at least {MIN_REASON_CHARS} characters)"
    row = fetch(conn, pid)
    if not row:
        return False, "proposal not found"
    if action == "approve":
        ok, why = can_approve(row)
        if not ok:
            return False, why
    elif row["status"] != "pending":
        return False, f"already {row['status']}"
    status = "approved" if action == "approve" else "rejected"
    cur = conn.execute(
        "UPDATE pending_strategy SET status=?, resolved_ts=?, decision_reason=?, decided_via=? "
        "WHERE id=? AND status='pending' AND from_version=?",
        (status, db.now(), text, via, pid, row["from_version"]),
    )
    conn.commit()
    if cur.rowcount != 1:
        return False, "it changed or was decided elsewhere a moment ago — refresh and retry"
    bt = backtest_of(row) or {}
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": db.now(), "pending_id": pid, "decision": status, "reason": text, "via": via,
            "change": row["variable"], "old": row["old_value"], "new": row["new_value"],
            "versions": f"v{row['from_version']}->v{row['to_version']}", "source": row["source"],
            "backtest_oos_sharpe": [
                (bt.get("current_oos") or {}).get("sharpe"), (bt.get("proposed_oos") or {}).get("sharpe"),
            ],
        }) + "\n")
    return True, status
