"""Paper broker: simulated fills, position tracking, mark-to-market PnL.

Pure paper. There is no live-order code path in this module at all — going live
would require a separate, deliberately-added execution adapter (see README).

Equity model:
    equity        = start_equity + realised_pnl + unrealised_pnl
    position_value= sum(size * current_price) for open long positions
    cash          = equity - position_value
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from . import db


def start_equity() -> float:
    return float(os.environ.get("HERMES_TRADING_START_EQUITY", "10000"))


def realised_pnl(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0.0) AS s FROM trades WHERE status='closed'"
    ).fetchone()
    return float(row["s"])


def open_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
    return db.rows_to_dicts(rows)


def position_size(equity: float, entry_price: float, position_size_r: float) -> float:
    """Units to buy so that deployed capital = position_size_r * equity."""
    if entry_price <= 0:
        return 0.0
    return (position_size_r * equity) / entry_price


def equity_now(conn: sqlite3.Connection, price: float) -> tuple[float, float, float]:
    """Return (equity, cash, position_value) marked at `price`."""
    realised = realised_pnl(conn)
    unreal = 0.0
    pos_value = 0.0
    for p in open_positions(conn):
        # long-only paper book
        unreal += p["size"] * (price - p["entry_price"])
        pos_value += p["size"] * price
    equity = start_equity() + realised + unreal
    cash = equity - pos_value
    return equity, cash, pos_value


def fill(
    conn: sqlite3.Connection,
    pending: dict[str, Any],
    price: float | None = None,
    spy: float | None = None,
) -> int:
    """Open a paper position from an approved pending trade. Returns trade id.

    `price` overrides the entry price (e.g. the live quote at approval time, so a
    fresh position starts at ~0 unrealised); defaults to the proposed price.
    `spy` snapshots the benchmark at fill time for the shadow-SPY comparison.
    """
    entry_price = pending["price"] if price is None else price
    cur = conn.execute(
        "INSERT INTO trades(symbol, side, entry_ts, entry_price, size, "
        "stop_price, target_price, strategy_version, status, context, spy_entry) "
        "VALUES(?,?,?,?,?,?,?,?, 'open', ?, ?)",
        (
            pending["symbol"],
            pending["side"],
            db.now(),
            entry_price,
            pending["size"],
            pending["stop_price"],
            pending["target_price"],
            pending["strategy_version"],
            pending.get("context"),
            spy,
        ),
    )
    return int(cur.lastrowid)


def close_position(
    conn: sqlite3.Connection,
    trade: dict[str, Any],
    price: float,
    reason: str,
    spy: float | None = None,
) -> None:
    """Close an open long position at `price` and record realised PnL.

    `spy` snapshots the benchmark at close time, ending the trade's shadow-SPY
    window (see hermes_trading.spy_compare)."""
    pnl = trade["size"] * (price - trade["entry_price"])
    cost = trade["size"] * trade["entry_price"]
    pnl_pct = (pnl / cost) if cost else 0.0
    conn.execute(
        "UPDATE trades SET status='closed', exit_ts=?, exit_price=?, "
        "exit_reason=?, pnl=?, pnl_pct=?, spy_exit=? WHERE id=?",
        (db.now(), price, reason, pnl, pnl_pct, spy, trade["id"]),
    )


def check_exits(conn: sqlite3.Connection, price: float) -> list[str]:
    """Auto-exit any open position whose stop or target is hit. Returns messages."""
    msgs: list[str] = []
    for p in open_positions(conn):
        if price <= p["stop_price"]:
            close_position(conn, p, price, "stop_loss")
            msgs.append(f"stop-loss hit on #{p['id']} @ {price:.2f}")
        elif price >= p["target_price"]:
            close_position(conn, p, price, "take_profit")
            msgs.append(f"take-profit hit on #{p['id']} @ {price:.2f}")
    return msgs


def snapshot_equity(conn: sqlite3.Connection, price: float) -> None:
    equity, cash, pos_value = equity_now(conn, price)
    conn.execute(
        "INSERT OR REPLACE INTO equity(ts, equity, cash, position_value) "
        "VALUES(?,?,?,?)",
        (round(db.now(), 1), equity, cash, pos_value),
    )


# ---- multi-symbol helpers (rotation strategy) ------------------------------


def equal_weight_size(
    equity: float, price: float, max_positions: int, notional_cap_pct: float
) -> float:
    """Shares for an equal-weight slot: equity/N per position, capped at notional %."""
    if price <= 0 or max_positions <= 0:
        return 0.0
    target_notional = equity / max_positions
    cap = equity * (notional_cap_pct / 100.0)
    return min(target_notional, cap) / price


def inverse_vol_weights(vols: dict[str, float]) -> dict[str, float]:
    """Normalized inverse-volatility weights over a basket (sum to 1.0).

    Each name's weight is proportional to 1/vol, so a choppier holding gets a
    smaller slice and every position contributes roughly equal *risk* rather than
    equal *dollars*. Names with missing/zero vol fall back to an equal split.
    """
    if not vols:
        return {}
    inv = {s: (1.0 / v) for s, v in vols.items() if v and v > 0}
    total = sum(inv.values())
    if total <= 0:
        n = len(vols)
        return {s: 1.0 / n for s in vols}
    return {s: w / total for s, w in inv.items()}


def inverse_vol_size(
    equity: float, price: float, weight: float, notional_cap_pct: float
) -> float:
    """Shares for an inverse-vol slot: weight*equity of capital, capped at notional %."""
    if price <= 0 or weight <= 0:
        return 0.0
    target_notional = equity * weight
    cap = equity * (notional_cap_pct / 100.0)
    return min(target_notional, cap) / price


def rank_weights(ranked_syms: list[str], power: float = 1.0) -> dict[str, float]:
    """Conviction-by-rank weights over a basket (sum to 1.0), best name first.

    Weight ∝ (N - rank)**power, so the #1-ranked name gets the biggest slice and
    conviction decays down the list — the opposite of inverse-vol, and aligned WITH
    a momentum signal. power=1 is linear decay; power=0 collapses to equal weight.
    """
    n = len(ranked_syms)
    if n == 0:
        return {}
    raw = {s: (n - i) ** power for i, s in enumerate(ranked_syms)}
    total = sum(raw.values())
    if total <= 0:
        return {s: 1.0 / n for s in ranked_syms}
    return {s: v / total for s, v in raw.items()}


def sizing_weights(
    sizing: str,
    basket: list[str],
    *,
    scores: dict[str, float] | None = None,
    vols: dict[str, float] | None = None,
    rank_power: float = 1.0,
) -> dict[str, float]:
    """Normalized target weights for a basket under the chosen sizing scheme.

    Returns {} for ``equal_weight`` (the caller falls back to ``equal_weight_size``).
    Shared by the live loop and the backtester so they can never disagree.
    """
    if sizing == "inverse_vol":
        vols = vols or {}
        return inverse_vol_weights({s: vols[s] for s in basket if vols.get(s)})
    if sizing == "rank_weight":
        scores = scores or {}
        ranked = sorted(basket, key=lambda s: scores.get(s, float("-inf")), reverse=True)
        return rank_weights(ranked, rank_power)
    return {}


def weighted_size(
    sizing: str,
    weights: dict[str, float],
    sym: str,
    equity: float,
    price: float,
    max_positions: int,
    notional_cap_pct: float,
) -> float:
    """Shares to buy for `sym`: weighted slot if the scheme produced a weight for it,
    else an equal-weight slot. Single sizing entry point for live + backtest."""
    if sizing in ("inverse_vol", "rank_weight") and sym in weights:
        return inverse_vol_size(equity, price, weights[sym], notional_cap_pct)
    return equal_weight_size(equity, price, max_positions, notional_cap_pct)


def equity_now_multi(
    conn: sqlite3.Connection, prices: dict[str, float]
) -> tuple[float, float, float]:
    """(equity, cash, position_value) marked with a per-symbol price map."""
    realised = realised_pnl(conn)
    unreal = 0.0
    pos_value = 0.0
    for p in open_positions(conn):
        px = prices.get(p["symbol"], p["entry_price"])
        unreal += p["size"] * (px - p["entry_price"])
        pos_value += p["size"] * px
    equity = start_equity() + realised + unreal
    return equity, equity - pos_value, pos_value


def snapshot_equity_multi(conn: sqlite3.Connection, prices: dict[str, float]) -> None:
    equity, cash, pos_value = equity_now_multi(conn, prices)
    conn.execute(
        "INSERT OR REPLACE INTO equity(ts, equity, cash, position_value) "
        "VALUES(?,?,?,?)",
        (round(db.now(), 1), equity, cash, pos_value),
    )


def check_catastrophe_stops(
    conn: sqlite3.Connection, prices: dict[str, float], spy: float | None = None
) -> list[str]:
    """Auto-close any position whose price has hit its catastrophe stop."""
    msgs: list[str] = []
    for p in open_positions(conn):
        px = prices.get(p["symbol"])
        if px is not None and px <= p["stop_price"]:
            close_position(conn, p, px, "catastrophe_stop", spy=spy)
            msgs.append(f"catastrophe stop #{p['id']} {p['symbol']} @ {px:.2f}")
    return msgs


def close_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    price: float,
    reason: str,
    spy: float | None = None,
) -> int | None:
    """Close the open position for `symbol` (rotation exit). Returns trade id or None."""
    for p in open_positions(conn):
        if p["symbol"] == symbol:
            close_position(conn, p, price, reason, spy=spy)
            return p["id"]
    return None


def held_symbols(conn: sqlite3.Connection) -> list[str]:
    return [p["symbol"] for p in open_positions(conn)]
