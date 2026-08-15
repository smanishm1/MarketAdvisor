"""The 24/7 worker loop.

Each tick: reconcile human decisions, pull price, auto-exit stops/targets,
evaluate the strategy, and PROPOSE (never auto-fill) new entries. Resilient to
transient adapter failures (per-fetch retries + circuit breaker).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import time

from rich.console import Console

from . import approval, brief, db, memory, paper_broker, settings, srsr
from .adapters import equities
from .adapters import price as price_adapter
from .config import load_goal, load_strategy, save_strategy
from .indicators import rsi
from .paths import HEARTBEAT_FILE, HISTORY_DIR, STRATEGY_FILE, ensure_dirs, load_env
from .reflect import propose_once

console = Console()

# Worker loop cadence. Default 20s for near-real-time price/value/unreal on the dashboard.
# Bump HERMES_TICK_SECONDS / HERMES_RISK_CHECK_SECONDS up if Yahoo starts throttling.
TICK_SECONDS = int(os.environ.get("HERMES_TICK_SECONDS", "20"))
FETCH_RETRIES = 3
CIRCUIT_BREAK_AFTER = 5
# how often to pull intraday quotes for held positions + benchmark (live prices + the
# stop/black-swan check). Decisions still run on daily bars — this is display + risk only.
RISK_CHECK_SECONDS = int(os.environ.get("HERMES_RISK_CHECK_SECONDS", "20"))
# write a full equity-curve point at most this often (keeps the table from bloating at 20s)
SNAPSHOT_SECONDS = int(os.environ.get("HERMES_SNAPSHOT_SECONDS", "60"))
_last_snapshot_ts = 0.0

# guards against overlapping auto-reflections (the Hermes call can take ~30s)
_reflection_inflight = False


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


async def fetch_price_with_retry(symbol: str, period: int) -> dict:
    limit = max(period + 5, 100)
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            return await asyncio.to_thread(price_adapter.fetch, symbol, "1m", limit)
        except price_adapter.SchemaError:
            raise  # schema problems are not transient — halt
        except Exception as exc:  # noqa: BLE001 — network/exchange transient
            last_exc = exc
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"price fetch failed after {FETCH_RETRIES} tries: {last_exc}")


def reconcile_strategy(conn: sqlite3.Connection) -> None:
    """Apply any human-approved strategy change: archive prior, write new."""
    for prop in approval.list_approved_strategy(conn):
        ensure_dirs()
        current = load_strategy()
        from_v = str(current.get("version", "01")).zfill(2)
        archive = HISTORY_DIR / f"v{from_v.zfill(4)}.yaml"
        archive.write_text(STRATEGY_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        STRATEGY_FILE.write_text(prop["proposed_yaml"], encoding="utf-8")
        approval.set_strategy_status(conn, prop["id"], "applied")
        console.log(
            f"[cyan]strategy applied[/]: v{from_v} -> v{prop['to_version']} "
            f"({prop['variable']}: {prop['old_value']} -> {prop['new_value']})"
        )


async def reconcile_trades(conn: sqlite3.Connection, quote, spy_quote=None) -> None:
    """Fill any human-approved trade proposal at the LIVE price at approval time.

    A proposal's price is the daily close captured when it was proposed; marking
    then uses the live intraday quote, so a fresh position would show a small
    instant gain/loss purely from that series mismatch. We re-price the fill to the
    live quote (`quote(symbol)`) so a new position starts at ~0 unrealised, scaling
    the stop/target by the same ratio to preserve their percentage distance. If no
    live quote is available (market closed / fetch failed), we fall back to the
    proposed price — so this degrades to the old behaviour, never blocks a fill.

    `spy_quote` (async, optional) snapshots the benchmark once per fill batch so
    each trade's shadow-SPY window starts at the same moment as the fill.
    """
    pends = approval.list_approved_unfilled(conn)
    if not pends:
        return
    spy: float | None = None
    if spy_quote is not None:
        try:
            spy = await spy_quote()
        except Exception:  # noqa: BLE001 — the comparison is best-effort
            spy = None

    for pend in pends:
        proposed = float(pend["price"])
        live: float | None = None
        try:
            live = await quote(pend["symbol"])
        except Exception:  # noqa: BLE001 — re-pricing is best-effort
            live = None

        if live and live > 0 and proposed > 0:
            ratio = live / proposed
            pend["stop_price"] = float(pend["stop_price"]) * ratio
            pend["target_price"] = float(pend["target_price"]) * ratio
            fill_price = live
        else:
            fill_price = proposed

        tid = paper_broker.fill(conn, pend, price=fill_price, spy=spy)
        approval.set_trade_status(conn, pend["id"], "filled")
        note = "" if fill_price == proposed else f" (re-priced from {proposed:.2f})"
        console.log(
            f"[green]filled[/] #{tid} {pend['symbol']} {pend['side']} "
            f"{pend['size']:.6f} @ {fill_price:.2f}{note}"
        )


def evaluate_and_propose(conn: sqlite3.Connection, data: dict) -> None:
    """If the entry condition fires and nothing is pending/open, propose a trade."""
    strat = load_strategy()
    entry = strat["entry"]
    if entry.get("indicator") != "rsi":
        return
    period = int(entry.get("period", 14))
    threshold = float(entry.get("threshold", 30))
    value = rsi(data["closes"], period)
    if value is None:
        return

    symbol = data["symbol"]
    if value < threshold and not approval.has_pending_or_open(conn, symbol):
        price = data["last"]
        equity, _, _ = paper_broker.equity_now(conn, price)
        size = paper_broker.position_size(equity, price, float(strat["position_size_r"]))
        stop = price * (1 - float(strat["stop_loss_pct"]) / 100.0)
        target = price * (1 + float(strat["take_profit_pct"]) / 100.0)
        pid = approval.propose_trade(
            conn,
            {
                "symbol": symbol,
                "side": "long",
                "price": price,
                "rsi": value,
                "stop_price": stop,
                "target_price": target,
                "size": size,
                "strategy_version": strat["version"],
            },
        )
        console.log(
            f"[yellow]PROPOSED[/] trade #{pid} {symbol} long @ {price:.2f} "
            f"(RSI {value:.1f} < {threshold}) — awaiting approval"
        )


def write_heartbeat(data: dict, equity: float, status: str) -> None:
    HEARTBEAT_FILE.write_text(
        json.dumps(
            {
                "ts": db.now(),
                "status": status,
                "symbol": data.get("symbol"),
                "last": data.get("last"),
                "prices": data.get("prices") or {},   # per-symbol prices (rotation)
                "equity": round(equity, 2),
            }
        ),
        encoding="utf-8",
    )


async def _run_reflection_bg(mode: str) -> None:
    """Run one reflection off the event loop (the Hermes subprocess is blocking)."""
    global _reflection_inflight
    try:
        change = await asyncio.to_thread(propose_once, mode)
        if change:
            console.log(
                f"[green]auto-reflection proposed[/] #{change['id']}: "
                f"{change['variable']} {change['old_value']} -> {change['new_value']} "
                f"— awaiting approval"
            )
    except Exception as exc:  # noqa: BLE001
        console.log(f"[red]auto-reflection failed[/]: {exc}")
    finally:
        _reflection_inflight = False


async def maybe_auto_reflect(conn: sqlite3.Connection) -> None:
    """Fire a reflection after every `reflection_every` closed trades.

    Skips if disabled, if one is already running, or if a proposal is already
    awaiting approval (we never stack proposals). The change it produces still
    requires a human click — this only automates *proposing*, not applying.
    """
    global _reflection_inflight
    if not settings.auto_reflect(conn):
        return
    if _reflection_inflight:
        return
    if approval.list_pending_strategy(conn):
        return  # a proposal is already waiting for you — don't pile on another

    goal = load_goal()
    every = int(goal.get("reflection_every", 5) or 5)
    closed = conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE status='closed'"
    ).fetchone()["c"]
    last = int(db.get_meta(conn, "last_reflection_count", "0") or "0")
    if closed - last < every:
        return

    db.set_meta(conn, "last_reflection_count", str(closed))
    conn.commit()
    mode = settings.reflect_mode(conn)
    _reflection_inflight = True
    console.log(
        f"[cyan]auto-reflection[/] triggered — {closed} closed trades "
        f"(every {every}); mode={mode}"
    )
    asyncio.create_task(_run_reflection_bg(mode))


async def rsi_tick(conn: sqlite3.Connection, symbol: str) -> None:
    """One tick of the single-asset RSI strategy (crypto via ccxt)."""
    strat = load_strategy()
    period = int(strat["entry"].get("period", 14))

    async def _crypto_quote(sym: str) -> float | None:
        d = await fetch_price_with_retry(sym, period)
        return d.get("last")

    reconcile_strategy(conn)
    await reconcile_trades(conn, _crypto_quote)
    conn.commit()

    data = await fetch_price_with_retry(symbol, period)
    price = data["last"]

    for msg in paper_broker.check_exits(conn, price):
        console.log(f"[magenta]exit[/]: {msg}")
    evaluate_and_propose(conn, data)
    paper_broker.snapshot_equity(conn, price)
    conn.commit()

    await maybe_auto_reflect(conn)
    equity, _, _ = paper_broker.equity_now(conn, price)
    write_heartbeat(data, equity, "ok")


# ---- rotation engine -------------------------------------------------------

_universe_cache: dict[str, object] = {"key": None, "data": None}


async def _fetch_universe_cached(symbols: list[str]) -> dict:
    """Fetch daily data once per calendar day (rotation acts on daily bars)."""
    key = dt.date.today().isoformat() + "|" + ",".join(symbols)
    if _universe_cache["key"] == key and _universe_cache["data"] is not None:
        return _universe_cache["data"]  # type: ignore[return-value]
    data = await asyncio.to_thread(equities.fetch_daily, symbols, "2y")
    _universe_cache["key"] = key
    _universe_cache["data"] = data
    return data


# intraday quotes for the RISK check only — refreshed every RISK_CHECK_SECONDS
_live_cache: dict[str, object] = {"ts": 0.0, "data": {}}


async def _fetch_live_prices(symbols: list[str]) -> dict[str, float]:
    """Current intraday prices for `symbols`, refreshed at most every RISK_CHECK_SECONDS.
    Returns {} (caller falls back to daily close) on failure or a closed market."""
    import time

    now = time.time()
    if now - float(_live_cache["ts"]) < RISK_CHECK_SECONDS and _live_cache["data"]:
        return _live_cache["data"]  # type: ignore[return-value]
    data = await asyncio.to_thread(equities.fetch_quotes, symbols)
    _live_cache["ts"] = now
    _live_cache["data"] = data
    return data


def _rebalance_due(conn: sqlite3.Connection, cfg: dict) -> bool:
    last = db.get_meta(conn, "last_rebalance_date", None)
    today = dt.date.today()
    if last is None:
        return True
    last_d = dt.date.fromisoformat(last)
    if last_d == today:
        return False
    if today.weekday() == 4:  # Friday
        return True
    return (today - last_d).days >= 7  # catch up if the worker missed Fridays


def _black_swan_guard(
    conn: sqlite3.Connection, bench: list[float], last: dict[str, float],
    spy_live: float | None = None,
) -> bool:
    """Portfolio circuit breaker. Flips to risk-off (and flattens) on a severe market
    shock; auto-clears once the market trend repairs. Returns True if risk-off now.

    Honest limit: on daily data this reacts at the daily close — it cushions crashes and
    blocks buying into one, but can't dodge an intraday gap.
    """
    goal = load_goal()
    bs = goal.get("black_swan", {})
    crash_pct = float(bs.get("crash_day_pct", -0.07))
    early_pct = float(bs.get("early_warn_day_pct", -0.05))
    maxdd_pct = float(bs.get("max_portfolio_drawdown_pct", 0.20))
    flatten = bool(bs.get("flatten_on_trigger", True))

    risk_off = db.get_meta(conn, "risk_off", "0") == "1"
    if spy_live and bench and bench[-1]:
        spy_ret = spy_live / bench[-1] - 1.0            # intraday vs last daily close
    elif len(bench) >= 2 and bench[-2]:
        spy_ret = bench[-1] / bench[-2] - 1.0           # daily close-to-close fallback
    else:
        spy_ret = 0.0
    row = conn.execute("SELECT MAX(equity) AS m FROM equity").fetchone()
    peak = row["m"] if row and row["m"] else paper_broker.start_equity()
    equity_now, _, _ = paper_broker.equity_now_multi(conn, last)
    dd = (peak - equity_now) / peak if peak else 0.0

    reason = None
    if spy_ret <= crash_pct:
        reason = f"market crash: SPY {spy_ret:+.1%} in a day (limit {crash_pct:.0%})"
    elif dd >= maxdd_pct:
        reason = f"portfolio drawdown {dd:.1%} (limit {maxdd_pct:.0%})"

    if reason and not risk_off:
        db.set_meta(conn, "risk_off", "1")
        db.set_meta(conn, "risk_off_reason", reason)
        db.set_meta(conn, "risk_off_ts", str(db.now()))
        console.log(f"[bold red]BLACK SWAN — risk-off[/]: {reason}")
        if flatten:
            spy_px = spy_live or (bench[-1] if bench else None)
            for sym in paper_broker.held_symbols(conn):
                tid = paper_broker.close_symbol(
                    conn, sym, last.get(sym, 0.0), "black_swan", spy=spy_px
                )
                if tid:
                    console.log(f"[red]flattened #{tid} {sym} -> cash[/]")
        conn.commit()
        return True

    # auto-clear once the market's own trend repairs (SPY back above its 200-day)
    if risk_off and not reason:
        sma200 = srsr.sma(bench, 200)
        if sma200 is not None and bench[-1] > sma200:
            db.set_meta(conn, "risk_off", "0")
            db.set_meta(conn, "risk_off_reason", "")
            console.log("[green]risk-off cleared — SPY back above its 200-day average[/]")
            conn.commit()

    # Early-warning brake (Al's point — act before the -7% NYSE Level-1 halt): on a sharp
    # down day, PAUSE new buys (no selling — that would whipsaw on an intraday dip). The
    # -7% / 20%-DD flatten above is the hard stop; this just stops adding risk on the way in.
    risk_off = db.get_meta(conn, "risk_off", "0") == "1"
    halted = db.get_meta(conn, "buys_halted", "0") == "1"
    if not risk_off and spy_ret <= early_pct:
        if not halted:
            db.set_meta(conn, "buys_halted", "1")
            db.set_meta(
                conn, "buys_halted_reason",
                f"SPY {spy_ret:+.1%} today (early-warning {early_pct:.0%}) — new buys paused",
            )
            console.log(f"[yellow]EARLY BRAKE — new buys paused[/]: SPY {spy_ret:+.1%} today")
            conn.commit()
    elif halted:
        db.set_meta(conn, "buys_halted", "0")
        db.set_meta(conn, "buys_halted_reason", "")
        console.log("[green]early brake cleared — SPY recovered above the warning level[/]")
        conn.commit()
    return risk_off


async def rotation_tick(conn: sqlite3.Connection, cfg: dict) -> None:
    """One tick of the sector relative-strength rotation strategy (ETFs via yfinance)."""
    universe = list(cfg.get("universe", []))
    benchmark = cfg.get("benchmark", "SPY")
    if not universe:
        raise equities.SchemaError("rotation strategy has an empty universe")

    data = await _fetch_universe_cached(universe + [benchmark])
    prices = {s: data[s]["closes"] for s in universe}   # daily closes — for ranking
    last = {s: data[s]["last"] for s in universe}        # daily close per ETF
    bench = data[benchmark]["closes"]

    async def _equity_quote(sym: str) -> float | None:
        q = await asyncio.to_thread(equities.fetch_quotes, [sym])
        return q.get(sym)

    async def _spy_quote() -> float | None:
        # live benchmark for the shadow-SPY snapshot; daily close when market closed
        q = await asyncio.to_thread(equities.fetch_quotes, [benchmark])
        return q.get(benchmark) or data[benchmark]["last"]

    reconcile_strategy(conn)
    await reconcile_trades(conn, _equity_quote, _spy_quote)
    conn.commit()

    # intraday RISK overlay: live prices for held positions + benchmark only.
    # Decisions stay on daily closes; this just makes stops/black-swan react faster.
    held = paper_broker.held_symbols(conn)
    live = await _fetch_live_prices(list(dict.fromkeys(held + [benchmark])))
    for sym in held:
        if sym in live:
            last[sym] = live[sym]
    spy_live = live.get(benchmark)

    spy_px = spy_live or bench[-1]   # benchmark mark for shadow-SPY close snapshots

    # protective exits (auto) — react at the intraday cadence
    for msg in paper_broker.check_catastrophe_stops(conn, last, spy=spy_px):
        console.log(f"[magenta]{msg}[/]")
    # equity is recomputed live in the dashboard from the heartbeat; only persist a
    # curve point every SNAPSHOT_SECONDS so the table doesn't bloat at the 20s tick.
    global _last_snapshot_ts
    if time.time() - _last_snapshot_ts >= SNAPSHOT_SECONDS:
        paper_broker.snapshot_equity_multi(conn, last)
        _last_snapshot_ts = time.time()
    conn.commit()

    # black-swan circuit breaker: may flatten to cash and halt new buys
    risk_off = _black_swan_guard(conn, bench, last, spy_live)

    # morning market brief — once a day, from the data this tick already fetched
    if brief.due(conn):
        try:
            brief.store(conn, brief.compose(conn, cfg, data))
            console.log("[cyan]morning brief generated[/]")
        except Exception as exc:  # noqa: BLE001 — the brief must never kill a tick
            console.log(f"[yellow]morning brief failed[/]: {exc}")

    rebalance = _rebalance_due(conn, cfg)
    fast = settings.fast_exits(conn)
    if rebalance or fast:
        decision = srsr.evaluate(prices, bench, cfg)
        held = paper_broker.held_symbols(conn)
        buys, sells = srsr.actions(decision, held, cfg)

        # rotation exits are auto (like stops). fast_exits ON -> evaluated every tick (the
        # daily-close ranking is what moves), so a fader leaves the day it weakens; OFF ->
        # only at the weekly rebalance.
        for sym, reason in sells:
            tid = paper_broker.close_symbol(conn, sym, last.get(sym, 0.0), reason, spy=spy_px)
            if tid:
                console.log(f"[magenta]rotation exit[/] #{tid} {sym} ({reason})")
        conn.commit()

        buys_halted = db.get_meta(conn, "buys_halted", "0") == "1"
        if rebalance and not risk_off and not buys_halted:   # NEW positions only at the weekly rebalance
            equity, cash, _ = paper_broker.equity_now_multi(conn, last)
            held_after = paper_broker.held_symbols(conn)
            pend_rows = approval.list_pending_trades(conn)
            pending = [t["symbol"] for t in pend_rows]
            slots_left = int(cfg.get("max_positions", 3)) - len(held_after) - len(pending)
            # paper book has NO margin: cap total commitments at available cash. Subtract
            # what already-pending (unfilled) proposals will consume when approved, then
            # decrement as we propose, so cash can never go negative once everything fills.
            cash_left = cash - sum(float(t["price"]) * float(t["size"]) for t in pend_rows)

            # position sizing — same scheme/weights as the backtester (paper_broker)
            sizing = str(cfg.get("sizing", "equal_weight")).strip().lower()
            rank_power = float(cfg.get("rank_power", 1.0))
            basket = held_after + [s for s in buys if s not in held_after]
            vols = (
                {s: v for s in basket
                 if (v := srsr.volatility(
                     prices.get(s, []), int(cfg.get("vol_lookback_days", 63)))) is not None}
                if sizing == "inverse_vol" else None
            )
            weights = paper_broker.sizing_weights(
                sizing, basket, scores=decision.scores, vols=vols, rank_power=rank_power
            )
            ranked = sorted(decision.scores, key=lambda s: decision.scores[s], reverse=True)
            rank_of = {s: i + 1 for i, s in enumerate(ranked)}
            for sym in buys:
                if slots_left <= 0:
                    break
                if sym in held_after or sym in pending or approval.has_pending_or_open(conn, sym):
                    continue
                price = last[sym]
                size = paper_broker.weighted_size(
                    sizing, weights, sym, equity, price,
                    int(cfg.get("max_positions", 3)),
                    srsr.cap_pct(cfg, sym),   # tighter cap for single stocks
                )
                if price > 0:
                    size = min(size, cash_left / price)   # never commit more than we have
                if size <= 0:
                    continue
                cash_left -= size * price
                stop = price * (1 - srsr.stop_pct(cfg, sym) / 100.0)
                # entry context: WHY this buy, plus how the last round-trip in this
                # symbol ended — carried onto the filled trade (trade-to-trade memory)
                context = json.dumps({
                    "score": round(decision.scores.get(sym, 0.0), 4),
                    "rank": rank_of.get(sym),
                    "of": len(ranked),
                    "bench_score": (
                        round(decision.benchmark_score, 4)
                        if decision.benchmark_score is not None else None
                    ),
                    "is_stock": srsr.is_stock(cfg, sym),
                    "last_exit": memory.trade_context(conn, sym),
                })
                pid = approval.propose_trade(
                    conn,
                    {
                        "symbol": sym, "side": "long", "price": price, "rsi": None,
                        "stop_price": stop, "target_price": price * 1e6,  # no target; rules exit
                        "size": size, "strategy_version": cfg["version"],
                        "context": context,
                    },
                )
                slots_left -= 1
                console.log(
                    f"[yellow]PROPOSED[/] #{pid} BUY {sym} @ {price:.2f} "
                    f"(score {decision.scores.get(sym, 0):+.3f}) — awaiting approval"
                )
        if rebalance:
            db.set_meta(conn, "last_rebalance_date", dt.date.today().isoformat())
        conn.commit()

    await maybe_auto_reflect(conn)
    equity, _, _ = paper_broker.equity_now_multi(conn, last)
    write_heartbeat(
        {
            "symbol": f"{len(universe)} symbols",
            "last": spy_live or data[benchmark]["last"],
            "prices": last,
        },
        equity,
        "ok",
    )


async def run_loop(symbol_override: str | None = None) -> None:
    load_env()
    db.init_db()
    goal = load_goal()
    strat0 = load_strategy()
    stype = strat0.get("type", "rsi")
    label = (
        f"rotation · {len(strat0.get('universe', []))} symbols"
        if stype == "relative_strength_rotation"
        else (symbol_override or goal["asset"])
    )
    console.log(f"[bold]Booting hermes-trading worker[/] — paper mode — {label}")

    consecutive_failures = 0
    while True:
        conn = db.connect()
        try:
            strat = load_strategy()
            if strat.get("type") == "relative_strength_rotation":
                await rotation_tick(conn, strat)
            else:
                await rsi_tick(conn, symbol_override or goal["asset"])
            consecutive_failures = 0
        except (price_adapter.SchemaError, equities.SchemaError) as exc:
            console.log(f"[red]SCHEMA ERROR — halting[/]: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            console.log(f"[red]tick failed[/] ({consecutive_failures}): {exc}")
            if consecutive_failures >= CIRCUIT_BREAK_AFTER:
                console.log("[red]circuit breaker tripped — halting[/]")
                break
        finally:
            conn.close()
        await asyncio.sleep(TICK_SECONDS)
