"""
risk.py -- capital-preservation circuit breakers. This module's whole job
is to be paranoid on the user's behalf.

Three independent layers, from slowest/broadest to fastest/narrowest:

1. Black-swan brake (portfolio-wide): SPY down >=7% today, OR portfolio
   drawdown from peak >=20% -> FLATTEN TO CASH, halt all new buys. Auto-
   resumes only once SPY closes back above its 200-day average, or via a
   manual "Resume" click in the dashboard.
2. Early brake (portfolio-wide, softer): SPY down >=5% today -> PAUSE new
   buys only (existing positions are left alone -- no whipsaw selling).
   Auto-clears once SPY's daily return recovers above the threshold.
3. Per-position catastrophe stop: any single holding down >=15% from its
   own entry price -> sell that position, checked on both daily closes and
   the ~20s intraday poll so it reacts in minutes, not at end of day.

All of this only ever SELLS or PAUSES/HALTS BUYING automatically. Nothing
in this module ever buys anything -- that stays human-gated.
"""
from dataclasses import dataclass
from typing import Dict, List

import config
import data
import db


@dataclass
class SpyState:
    price: float
    day_return: float
    sma200: float
    above_200dma: bool


def get_spy_state() -> SpyState:
    df = data.get_daily_history(config.BENCHMARK, lookback_days=400)
    sma200 = None
    if len(df) >= config.SPY_RESUME_MA_DAYS:
        sma200 = float(df["Close"].iloc[-config.SPY_RESUME_MA_DAYS:].mean())
    last_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else last_close
    live = data.get_latest_price(config.BENCHMARK) or last_close
    day_return = (live - prev_close) / prev_close if prev_close else 0.0
    above = (sma200 is not None) and (live > sma200)
    return SpyState(price=live, day_return=day_return, sma200=sma200 or 0.0, above_200dma=above)


def portfolio_drawdown(current_equity: float) -> float:
    peak = db.get_peak_equity()
    if not peak or peak <= 0:
        return 0.0
    return (current_equity - peak) / peak


def evaluate_breakers(current_equity: float) -> dict:
    """Call every tick (daily and intraday). Updates halted/pause_buys meta
    flags and returns the current risk state for logging/display. This is
    the only place those flags are ever set to True; clearing back to False
    also happens only here (auto-resume) or via the dashboard's manual
    resume endpoint (which just clears the flag -- the worker re-evaluates
    next tick regardless)."""
    spy = get_spy_state()
    dd = portfolio_drawdown(current_equity)

    was_halted = bool(db.get_meta("halted", False))
    was_paused = bool(db.get_meta("pause_buys", False))

    black_swan = (spy.day_return <= config.BLACK_SWAN_SPY_DAY_DROP) or (dd <= config.BLACK_SWAN_DRAWDOWN)
    early_brake = spy.day_return <= config.EARLY_BRAKE_SPY_DAY_DROP

    result = {
        "spy_price": spy.price, "spy_day_return": spy.day_return,
        "spy_sma200": spy.sma200, "spy_above_200dma": spy.above_200dma,
        "portfolio_drawdown": dd, "black_swan_triggered": black_swan,
        "early_brake_triggered": early_brake, "flatten_required": False,
    }

    if black_swan and not was_halted:
        db.set_meta("halted", True)
        reason = (f"BLACK-SWAN BRAKE: SPY day return {spy.day_return:.2%} "
                  f"(threshold {config.BLACK_SWAN_SPY_DAY_DROP:.0%}) or portfolio "
                  f"drawdown {dd:.2%} (threshold {config.BLACK_SWAN_DRAWDOWN:.0%}).")
        db.set_meta("halt_reason", reason)
        db.log_event("ERROR", reason + " Flattening to cash and halting new buys.")
        result["flatten_required"] = True
    elif was_halted:
        # already halted -- check auto-resume condition
        if spy.above_200dma:
            db.set_meta("halted", False)
            db.set_meta("halt_reason", None)
            db.log_event("INFO", "Auto-resume: SPY closed back above its 200-day average. Halt lifted.")
        else:
            result["flatten_required"] = False  # already flat; nothing new to do

    if early_brake and not was_paused:
        db.set_meta("pause_buys", True)
        reason = f"EARLY BRAKE: SPY day return {spy.day_return:.2%} (threshold {config.EARLY_BRAKE_SPY_DAY_DROP:.0%}). Pausing new buys only."
        db.set_meta("pause_reason", reason)
        db.log_event("WARN", reason)
    elif was_paused and not early_brake and not black_swan:
        db.set_meta("pause_buys", False)
        db.set_meta("pause_reason", None)
        db.log_event("INFO", "Early brake cleared: SPY daily return recovered. Resuming new buys.")

    result["halted"] = bool(db.get_meta("halted", False))
    result["pause_buys"] = bool(db.get_meta("pause_buys", False))
    return result


def manual_resume():
    """Called by the dashboard's Resume button. Only ever CLEARS flags --
    never sets them, never places an order."""
    db.set_meta("halted", False)
    db.set_meta("halt_reason", None)
    db.set_meta("pause_buys", False)
    db.set_meta("pause_reason", None)
    db.log_event("INFO", "Manual resume triggered from dashboard.")


@dataclass
class CatastropheHit:
    symbol: str
    entry_price: float
    current_price: float
    drawdown: float


def check_catastrophe_stops_intraday(catastrophe_stop_pct: float) -> List[CatastropheHit]:
    """Fast, live-price-based check of the per-position hard stop. Safe to
    call every ~20s. Only ever identifies candidates to sell -- the worker
    performs the actual (paper) sell."""
    positions = db.get_positions()
    hits = []
    for pos in positions:
        symbol = pos["symbol"]
        entry_price = pos["avg_cost"]
        price = data.get_latest_price(symbol)
        if price is None or not entry_price:
            continue
        drawdown = (price - entry_price) / entry_price
        if drawdown <= -abs(catastrophe_stop_pct):
            hits.append(CatastropheHit(symbol=symbol, entry_price=entry_price,
                                        current_price=price, drawdown=drawdown))
    return hits
