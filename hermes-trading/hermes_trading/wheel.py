"""The options stream's engine — "the wheel" on healthy-FCF companies (paper only).

Called from the worker every tick; everything network-bound runs in threads and is
throttled, so the ETF stream never waits on it.

    1. SELL A CASH-SECURED PUT (needs your approval) on a screened company, ~25-delta,
       ~30 days out, expiring BEFORE its next earnings report, collateral <= one slot.
    2. At expiry: finished above the strike -> keep the premium (slot frees up).
                  finished below -> ASSIGNED: buy 100 shares at the strike.
    3. WITH SHARES: SELL A COVERED CALL (needs approval) at or above your cost.
       At expiry: above the strike -> shares CALLED AWAY at the strike (slot frees up);
                  below -> keep premium and shares, sell another call.
    Any short option that has captured 50% of its premium is BOUGHT BACK automatically.

Fills need LIVE quotes (market hours): sells fill at bid + 25% of the spread, buys at
ask - 25%, plus $0.65/contract. Simplifications (documented in docs/strategy-wheel.md):
no early assignment; assignment decided by the underlying's official close on expiry day.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console

from . import db, news
from . import execution as ex
from . import wheel_book as book
from . import wheel_screen as screen

console = Console()
ET = ZoneInfo("America/New_York")
CONTRACT = book.CONTRACT
MARK_EVERY_OPEN = 300        # seconds between live marks during market hours
MARK_EVERY_CLOSED = 3600
PROPOSAL_MAX_AGE = 3 * 86400  # unapproved proposals go stale after 3 days

_screen_task: asyncio.Task | None = None
_last_mark = 0.0


# ---- clock ------------------------------------------------------------------------------


def market_open(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(ET)
    return now.weekday() < 5 and dt.time(9, 35) <= now.time() <= dt.time(15, 55)


def after_expiry(expiry: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(ET)
    e = dt.date.fromisoformat(expiry)
    return now.date() > e or (now.date() == e and now.time() >= dt.time(16, 30))


# ---- pricing (Black-Scholes, for delta and the model chance of expiring worthless) --------


def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def greeks(S: float, K: float, T: float, iv: float, r: float, kind: str) -> tuple[float, float]:
    """(delta, P(expires worthless)) under Black-Scholes. kind = 'put' | 'call'."""
    if S <= 0 or K <= 0 or T <= 0 or iv <= 0:
        return (0.0, 1.0)
    d1 = (math.log(S / K) + (r + iv * iv / 2.0) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if kind == "put":
        return (_N(d1) - 1.0, _N(d2))
    return (_N(d1), _N(-d2))


# ---- market data (blocking — run in threads) ------------------------------------------------


def underlying_price(symbol: str) -> float | None:
    from .adapters import equities
    import yfinance as yf

    q = equities.fetch_quotes([symbol]).get(symbol)
    if q:
        return float(q)
    try:
        h = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
        return float(h.iloc[-1]) if len(h) else None
    except Exception:  # noqa: BLE001
        return None


def _rows(symbol: str, expiry: str, kind: str):
    import yfinance as yf

    ch = yf.Ticker(symbol).option_chain(expiry)
    return ch.puts if kind == "put" else ch.calls


def quote(symbol: str, expiry: str, contract: str, kind: str) -> dict[str, float] | None:
    try:
        df = _rows(symbol, expiry, kind)
        r = df[df.contractSymbol == contract]
        if r.empty:
            return None
        o = r.iloc[0]
        bid, ask, last = float(o.bid or 0), float(o.ask or 0), float(o.lastPrice or 0)
        return {"bid": bid, "ask": ask, "last": last,
                "mid": (bid + ask) / 2 if bid > 0 and ask > 0 else None}
    except Exception:  # noqa: BLE001
        return None


def close_on(symbol: str, date: str) -> float | None:
    """The underlying's official close on (or the last trading day before) `date`."""
    import yfinance as yf

    d = dt.date.fromisoformat(date)
    try:
        h = yf.Ticker(symbol).history(start=(d - dt.timedelta(days=6)).isoformat(),
                                      end=(d + dt.timedelta(days=1)).isoformat())["Close"].dropna()
        h = h[h.index.date <= d]
        return float(h.iloc[-1]) if len(h) else None
    except Exception:  # noqa: BLE001
        return None


def best_contract(symbol: str, kind: str, cfg: dict[str, Any], S: float,
                  min_strike: float | None = None) -> dict[str, Any] | None:
    """The contract to sell: an expiry ~30 days out (inside dte_range, before earnings),
    then the out-of-the-money strike nearest the target delta, with live, liquid quotes."""
    import yfinance as yf

    r = ex.latest_annual("tbill")
    today = dt.date.today()
    earn = news.next_earnings_date(symbol) if cfg.get("avoid_earnings", True) else None
    lo, hi = cfg["dte_range"]
    dlo, dhi = cfg["delta_range"]
    target = float(cfg["target_delta"])
    frac = float(cfg["fill_spread_fraction"])
    try:
        exps = [(e, (dt.date.fromisoformat(e) - today).days) for e in yf.Ticker(symbol).options]
    except Exception:  # noqa: BLE001
        return None
    exps = [x for x in exps if lo <= x[1] <= hi and (earn is None or dt.date.fromisoformat(x[0]) < earn)]
    for e, dte in sorted(exps, key=lambda x: abs(x[1] - 30)):     # ~a month out first
        try:
            rows = _rows(symbol, e, kind)
        except Exception:  # noqa: BLE001
            continue
        T, best = dte / 365.0, None
        for _, o in rows.iterrows():
            K, iv = float(o.strike), float(o.impliedVolatility or 0)
            bid, ask, oi = float(o.bid or 0), float(o.ask or 0), float(o.openInterest or 0)
            if bid <= 0 or ask <= 0 or oi < cfg["min_open_interest"]:
                continue                                   # live, liquid quotes only
            mid = (bid + ask) / 2
            if (ask - bid) / mid * 100 > cfg["max_spread_pct"]:
                continue
            if (kind == "put" and K >= S) or (kind == "call" and K <= S):
                continue                                   # out of the money only
            if min_strike is not None and K < min_strike:
                continue                                   # never below cost basis
            delta, p_otm = greeks(S, K, T, iv, r, kind)
            if not (dlo <= abs(delta) <= dhi):
                continue
            c = {"contract": o.contractSymbol, "expiry": e, "dte": dte, "strike": K, "bid": bid,
                 "ask": ask, "mid": mid, "fill": bid + frac * (ask - bid), "iv": iv, "delta": delta,
                 "p_expire_worthless": p_otm, "open_interest": oi,
                 "earnings": earn.isoformat() if earn else None}
            if best is None or abs(abs(delta) - target) < abs(abs(best["delta"]) - target):
                best = c
        if best:
            return best
    return None


def fetch_marks(positions: list[dict[str, Any]]) -> dict[str, Any]:
    mk: dict[str, Any] = {"ts": time.time(), "stocks": {}, "options": {}}
    for sym in sorted({p["symbol"] for p in positions}):
        px = underlying_price(sym)
        if px:
            mk["stocks"][sym] = px
    for p in positions:
        if p["kind"] in ("short_put", "short_call"):
            q = quote(p["symbol"], p["expiry"], p["contract"], "put" if p["kind"] == "short_put" else "call")
            if q:
                mk["options"][p["contract"]] = q
    return mk


# ---- helpers --------------------------------------------------------------------------------


def _ctx(p: dict[str, Any]) -> dict[str, Any]:
    c = p.get("context")
    if isinstance(c, dict):
        return c
    try:
        return json.loads(c) if c else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _bg(label: str, fn, *args) -> None:
    try:
        await asyncio.to_thread(fn, *args)
        console.log(f"[dim]options stream: {label} done[/]")
    except Exception as exc:  # noqa: BLE001
        console.log(f"[yellow]options stream: {label} failed[/]: {exc}")


# ---- the tick ---------------------------------------------------------------------------------


async def tick(conn, cfg: dict[str, Any] | None = None) -> None:
    global _screen_task, _last_mark
    cfg = cfg or book.load_config()
    now = dt.datetime.now(ET)
    is_open = market_open(now)

    # 0. weekly FCF screen, in the background
    if (_screen_task is None or _screen_task.done()) and screen.due(conn, cfg):
        _screen_task = asyncio.create_task(_bg("FCF screen", screen.refresh, cfg))

    # 1. interest on cash (once a day)
    earned = book.accrue_interest(conn, cfg, now.date())
    if earned:
        console.log(f"[dim]options stream: cash interest +${earned:,.2f}[/]")

    # 2. fill approved proposals (needs live quotes; otherwise they wait for the open)
    for p in book.pending(conn, "approved"):
        await _fill(conn, cfg, p)

    # 3. expirations and assignment
    await _expire(conn, now)

    # 4. marks (+ automatic take-profit right after a fresh live mark)
    positions = book.open_positions(conn)
    if positions and time.time() - _last_mark > (MARK_EVERY_OPEN if is_open else MARK_EVERY_CLOSED):
        mk = await asyncio.to_thread(fetch_marks, positions)
        db.set_meta(conn, "opt_marks", json.dumps(mk))
        _last_mark = time.time()
        if is_open:
            _take_profit(conn, cfg, mk)
        book.snapshot(conn, book.valuation(conn, cfg, mk))
    elif not positions and time.time() - _last_mark > MARK_EVERY_CLOSED:
        db.set_meta(conn, "opt_marks", json.dumps({"ts": time.time(), "stocks": {}, "options": {}}))
        book.snapshot(conn, book.valuation(conn, cfg))
        _last_mark = time.time()

    # 5. stale proposals expire unapproved
    for p in book.pending(conn):
        if time.time() - float(p["proposed_ts"]) > PROPOSAL_MAX_AGE:
            book.set_pending_status(conn, p["id"], "expired")

    # 6. new proposals: once per trading day, during market hours (live quotes)
    today = now.date().isoformat()
    if is_open and now.time() >= dt.time(10, 0) and db.get_meta(conn, "opt_last_propose_date") != today:
        db.set_meta(conn, "opt_last_propose_date", today)
        conn.commit()
        await _propose(conn, cfg)
    conn.commit()


async def _fill(conn, cfg, p: dict[str, Any]) -> None:
    kind = "put" if p["action"] == "sell_put" else "call"
    q = await asyncio.to_thread(quote, p["symbol"], p["expiry"], p["contract"], kind)
    if not q or q["bid"] <= 0 or q["ask"] <= 0:
        return   # no live quote yet (market closed) — stays approved, fills at the next open
    qty = float(p["qty"])
    fill = q["bid"] + float(cfg["fill_spread_fraction"]) * (q["ask"] - q["bid"])
    comm = float(cfg["commission_per_contract"]) * qty
    ctx = dict(p["context"], fill_quote=q, commission_open=comm)
    if p["action"] == "sell_put":
        v = book.valuation(conn, cfg)
        need = float(p["strike"]) * CONTRACT * qty
        if need > v["free_cash"] - float(cfg["min_cash_buffer_pct"]) / 100 * v["equity"]:
            book.set_pending_status(conn, p["id"], "expired")
            console.log(f"[yellow]options: put #{p['id']} {p['symbol']} not filled — not enough free cash now[/]")
            return
        book.add_cash(conn, fill * CONTRACT * qty - comm)
        pid = book.open_position(conn, "short_put", p["symbol"], qty, fill, contract=p["contract"],
                                 strike=p["strike"], expiry=p["expiry"], context=ctx)
    else:
        stock = conn.execute("SELECT * FROM opt_positions WHERE id=? AND status='open'",
                             (p["position_id"],)).fetchone()
        covered = conn.execute("SELECT 1 FROM opt_positions WHERE kind='short_call' AND status='open' "
                               "AND symbol=?", (p["symbol"],)).fetchone()
        if not stock or covered:
            book.set_pending_status(conn, p["id"], "expired")
            return
        book.add_cash(conn, fill * CONTRACT * qty - comm)
        ctx["stock_position_id"] = p["position_id"]
        pid = book.open_position(conn, "short_call", p["symbol"], qty, fill, contract=p["contract"],
                                 strike=p["strike"], expiry=p["expiry"], context=ctx)
    book.set_pending_status(conn, p["id"], "filled")
    conn.commit()
    console.log(f"[green]options: SOLD {int(qty)} {p['symbol']} {p['expiry']} ${p['strike']:g} "
                f"{kind} @ ${fill:.2f} (+${fill * CONTRACT * qty - comm:,.2f}) — position #{pid}[/]")


def _take_profit(conn, cfg, mk: dict[str, Any]) -> None:
    tp = float(cfg["take_profit_pct"]) / 100.0
    frac = float(cfg["fill_spread_fraction"])
    for pos in book.open_positions(conn):
        if pos["kind"] not in ("short_put", "short_call"):
            continue
        q = mk["options"].get(pos["contract"]) or {}
        if not q.get("bid") or not q.get("ask") or q["bid"] <= 0 or q["ask"] <= 0:
            continue
        buy = q["ask"] - frac * (q["ask"] - q["bid"])
        if buy > float(pos["open_price"]) * (1.0 - tp):
            continue
        qty = float(pos["qty"])
        comm = float(cfg["commission_per_contract"]) * qty
        book.add_cash(conn, -(buy * CONTRACT * qty + comm))
        pnl = (float(pos["open_price"]) - buy) * CONTRACT * qty - comm - float(_ctx(pos).get("commission_open", 0))
        book.close_position(conn, pos, buy, "take_profit", pnl)
        console.log(f"[green]options: took profit on {pos['symbol']} {pos['kind']} — bought back @ ${buy:.2f} "
                    f"(sold @ ${pos['open_price']:.2f}), P&L ${pnl:+,.2f}[/]")
    conn.commit()


async def _expire(conn, now: dt.datetime) -> None:
    for pos in book.open_positions(conn):
        if pos["kind"] not in ("short_put", "short_call") or not after_expiry(pos["expiry"], now):
            continue
        close = await asyncio.to_thread(close_on, pos["symbol"], pos["expiry"])
        if close is None:
            continue   # retry next tick
        qty, K = float(pos["qty"]), float(pos["strike"])
        premium = float(pos["open_price"]) * CONTRACT * qty - float(_ctx(pos).get("commission_open", 0))
        if pos["kind"] == "short_put":
            if close < K:      # ASSIGNED: buy the shares at the strike
                book.add_cash(conn, -K * CONTRACT * qty)
                book.close_position(conn, pos, 0.0, "assigned", premium)
                sid = book.open_position(conn, "stock", pos["symbol"], qty * CONTRACT, K, context={
                    "from_put": pos["id"], "assigned_on": pos["expiry"], "close_on_expiry": close,
                    "effective_basis": K - float(pos["open_price"]),
                    "sector": _ctx(pos).get("sector"), "why": _ctx(pos).get("why")})
                console.log(f"[magenta]options: {pos['symbol']} put ASSIGNED — bought {int(qty * CONTRACT)} "
                            f"shares @ ${K:g} (close ${close:.2f}); stock position #{sid}[/]")
            else:
                book.close_position(conn, pos, 0.0, "expired", premium)
                console.log(f"[green]options: {pos['symbol']} ${K:g} put expired worthless — kept ${premium:,.2f}[/]")
        else:
            sid = _ctx(pos).get("stock_position_id")
            stock = conn.execute("SELECT * FROM opt_positions WHERE id=? AND status='open'", (sid,)).fetchone()
            if close > K and stock:   # CALLED AWAY: shares sold at the strike
                shares = float(stock["qty"])
                book.add_cash(conn, K * shares)
                book.close_position(conn, dict(stock), K, "called_away", (K - float(stock["open_price"])) * shares)
                book.close_position(conn, pos, 0.0, "called_away", premium)
                console.log(f"[green]options: {pos['symbol']} shares CALLED AWAY @ ${K:g} — wheel cycle complete[/]")
            else:
                book.close_position(conn, pos, 0.0, "expired", premium)
                console.log(f"[green]options: {pos['symbol']} ${K:g} call expired — kept ${premium:,.2f} and the shares[/]")
        conn.commit()


async def _propose(conn, cfg) -> None:
    """Covered calls on held shares first, then cash-secured puts into free slots."""
    pend = book.pending(conn)
    pend_calls = {p["position_id"] for p in pend if p["action"] == "sell_call"}
    covered = {p["symbol"] for p in book.open_positions(conn, "short_call")}
    for st in book.open_positions(conn, "stock"):
        if st["symbol"] in covered or st["id"] in pend_calls or float(st["qty"]) < CONTRACT:
            continue
        S = await asyncio.to_thread(underlying_price, st["symbol"])
        if not S:
            continue
        basis = float(st["open_price"]) if cfg.get("never_sell_calls_below_basis", True) else None
        c = await asyncio.to_thread(best_contract, st["symbol"], "call", cfg, S, basis)
        if not c:
            continue
        qty = int(float(st["qty"]) // CONTRACT)
        comm = float(cfg["commission_per_contract"]) * qty
        gain = (c["strike"] - float(st["open_price"])) * CONTRACT * qty + c["fill"] * CONTRACT * qty - comm
        ctx = {**{k: c[k] for k in ("delta", "p_expire_worthless", "dte", "iv", "earnings", "open_interest")},
               "stock_price": S, "cost_basis": float(st["open_price"]), "premium_total": c["fill"] * CONTRACT * qty - comm,
               "if_called_away_gain": gain, "sector": _ctx(st).get("sector"),
               "why": (f"You own {int(st['qty'])} {st['symbol']} at ${float(st['open_price']):.2f}. Selling the "
                       f"${c['strike']:g} call (at/above your cost) earns ${c['fill'] * CONTRACT * qty - comm:,.2f} now; "
                       f"if {st['symbol']} ends above ${c['strike']:g} on {c['expiry']}, your shares are sold there "
                       f"for a total gain of ${gain:,.2f} and the wheel starts over.")}
        pid = book.propose(conn, "sell_call", st["symbol"], c["contract"], c["strike"], c["expiry"], qty,
                           c["fill"], ctx, position_id=st["id"])
        console.log(f"[yellow]options: PROPOSED #{pid} sell covered call {st['symbol']} ${c['strike']:g} "
                    f"{c['expiry']} @ ~${c['fill']:.2f} — awaiting approval[/]")
    conn.commit()

    scr = screen.latest(conn)
    if not scr or not scr.get("candidates"):
        return
    used = book.slots_used(conn)
    pend = book.pending(conn)
    pend_puts = [p for p in pend if p["action"] == "sell_put"]
    free_slots = int(cfg["max_positions"]) - len(used) - len(pend_puts)
    if free_slots <= 0:
        return
    held_sectors = {(_ctx(p).get("sector")) for p in book.open_positions(conn)} | {p["context"].get("sector") for p in pend_puts}
    v = book.valuation(conn, cfg)
    avail = (v["free_cash"] - sum(float(p["strike"]) * CONTRACT * float(p["qty"]) for p in pend_puts)
             - float(cfg["min_cash_buffer_pct"]) / 100.0 * v["equity"])
    taken = used | {p["symbol"] for p in pend_puts}
    for rank, cand in enumerate(scr["candidates"], 1):
        if free_slots <= 0:
            break
        if cand["symbol"] in taken or cand["sector"] in held_sectors:
            continue   # one slot per company, and never two in the same sector
        S = await asyncio.to_thread(underlying_price, cand["symbol"])
        if not S:
            continue
        c = await asyncio.to_thread(best_contract, cand["symbol"], "put", cfg, S)
        if not c:
            continue
        coll = c["strike"] * CONTRACT
        if coll > float(cfg["max_collateral_per_position"]) or coll > avail:
            continue
        comm = float(cfg["commission_per_contract"])
        be = c["strike"] - c["fill"]
        ctx = {**{k: c[k] for k in ("delta", "p_expire_worthless", "dte", "iv", "earnings", "open_interest")},
               "stock_price": S, "collateral": coll, "premium_total": c["fill"] * CONTRACT - comm,
               "return_on_collateral": c["fill"] / c["strike"],
               "annualized": c["fill"] / c["strike"] * 365.0 / max(c["dte"], 1),
               "breakeven": be, "discount_to_price": 1.0 - be / S, "screen_rank": rank,
               "sector": cand["sector"], "fcf_margin": cand["fcf_margin"], "fcf_yield": cand["fcf_yield"],
               "net_debt_to_fcf": cand["net_debt_to_fcf"], "why_company": cand["why"],
               "why": (f"Sell the ${c['strike']:g} put expiring {c['expiry']} ({c['dte']} days, before earnings "
                       f"{c['earnings'] or 'n/a'}) for ~${c['fill'] * CONTRACT - comm:,.2f}, reserving "
                       f"${coll:,.0f}. If {cand['symbol']} (now ${S:.2f}) ends above ${c['strike']:g}, you keep the "
                       f"premium (model chance ~{c['p_expire_worthless']:.0%}); if below, you buy 100 shares at an "
                       f"effective ${be:.2f} — {1 - be / S:.1%} under today's price — then sell covered calls.")}
        pid = book.propose(conn, "sell_put", cand["symbol"], c["contract"], c["strike"], c["expiry"], 1,
                           c["fill"], ctx)
        console.log(f"[yellow]options: PROPOSED #{pid} sell put {cand['symbol']} ${c['strike']:g} {c['expiry']} "
                    f"@ ~${c['fill']:.2f} (collateral ${coll:,.0f}) — awaiting approval[/]")
        avail -= coll
        free_slots -= 1
        taken.add(cand["symbol"])
        held_sectors.add(cand["sector"])
    conn.commit()


# ---- read-only state for the dashboard / Discord / brief ------------------------------------------


def decide(conn, pid: int, action: str) -> tuple[bool, str]:
    status = "approved" if action == "approve" else "rejected"
    cur = conn.execute("UPDATE opt_pending SET status=?, resolved_ts=? WHERE id=? AND status='pending'",
                       (status, db.now(), pid))
    conn.commit()
    return (True, status) if cur.rowcount == 1 else (False, "not pending (already decided or expired)")


def state(conn) -> dict[str, Any]:
    cfg = book.load_config()
    mk = book.marks(conn)
    v = book.valuation(conn, cfg, mk)
    today = dt.date.today()
    pos = []
    for p in book.open_positions(conn):
        c = _ctx(p)
        row = {k: p[k] for k in ("id", "kind", "symbol", "contract", "strike", "expiry", "qty", "open_price", "open_ts")}
        row["underlying"] = mk["stocks"].get(p["symbol"])
        if p["kind"] == "stock":
            px = row["underlying"] or p["open_price"]
            row.update(mark=px, unrealised=(px - float(p["open_price"])) * float(p["qty"]),
                       effective_basis=c.get("effective_basis"))
        else:
            q = mk["options"].get(p["contract"]) or {}
            m = q.get("mid") or q.get("last") or p["open_price"]
            row.update(mark=m, unrealised=(float(p["open_price"]) - m) * CONTRACT * float(p["qty"]),
                       dte=(dt.date.fromisoformat(p["expiry"]) - today).days,
                       captured=1.0 - m / float(p["open_price"]) if float(p["open_price"]) else 0.0)
        pos.append(row)
    closed = db.rows_to_dicts(conn.execute(
        "SELECT id, kind, symbol, strike, expiry, open_price, close_price, close_reason, pnl, close_ts "
        "FROM opt_positions WHERE status='closed' ORDER BY close_ts DESC LIMIT 12").fetchall())
    scr = screen.latest(conn)
    return {
        "config": {k: cfg.get(k) for k in ("max_positions", "max_collateral_per_position", "min_cash_buffer_pct",
                                           "target_delta", "dte_range", "take_profit_pct", "start_equity")},
        "valuation": v, "positions": pos, "closed": closed, "pending": book.pending(conn),
        "market_open": market_open(),
        "screen": ({"date": scr["date"], "funnel": scr["funnel"], "excluded_downtrend": scr["excluded_downtrend"],
                    "candidates": scr["candidates"][:10]} if scr else None),
    }
