"""
strategy.py -- sector relative-strength rotation (momentum).

Pure functions only: everything here takes price data + dial values in, and
returns decisions out. No DB access, no I/O. This makes it trivially
testable and reusable by both the live worker and the backtester, which is
important -- if the backtest logic and the live logic ever drift apart, the
backtest stops meaning anything.

Decisions are made on DAILY CLOSES only. Intraday prices are used
elsewhere (risk.py) for the catastrophe stop / circuit breakers between
closes, and for display -- never for ranking or rebalance.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

import config


def compute_momentum(df: pd.DataFrame, lookbacks=config.MOMENTUM_LOOKBACKS_DAYS) -> Optional[float]:
    """Equal-weighted blend of trailing returns over each lookback window
    (in trading days). Returns None if there isn't enough history yet."""
    closes = df["Close"]
    if len(closes) < max(lookbacks) + 1:
        return None
    rets = []
    for n in lookbacks:
        past = closes.iloc[-1 - n]
        now = closes.iloc[-1]
        if past == 0 or pd.isna(past) or pd.isna(now):
            return None
        rets.append((now - past) / past)
    return sum(rets) / len(rets)


def compute_sma(df: pd.DataFrame, length: int) -> Optional[float]:
    closes = df["Close"]
    length = int(length)
    if len(closes) < length:
        return None
    return float(closes.iloc[-length:].mean())


def last_close(df: pd.DataFrame) -> float:
    return float(df["Close"].iloc[-1])


@dataclass
class RankedSector:
    symbol: str
    momentum: Optional[float]
    price: float
    sma: Optional[float]
    above_trend: bool
    beats_spy: bool
    qualifies: bool
    rank: int = 0  # 1 = best momentum, assigned across the whole universe


def build_ranking(price_data: Dict[str, pd.DataFrame], spy_df: pd.DataFrame,
                   trend_filter_len: float) -> List[RankedSector]:
    """Rank the full 11-ETF universe by momentum. A sector QUALIFIES only if
    it both beats SPY's momentum AND sits above its own trend-filter moving
    average. Non-qualifying sectors are still ranked (needed for the
    exit-rank-hysteresis check) but will never be newly bought."""
    spy_mom = compute_momentum(spy_df)
    entries = []
    for symbol, df in price_data.items():
        mom = compute_momentum(df)
        sma = compute_sma(df, trend_filter_len)
        price = last_close(df)
        above_trend = (sma is not None) and (price > sma)
        beats_spy = (mom is not None) and (spy_mom is not None) and (mom > spy_mom)
        qualifies = bool(above_trend and beats_spy)
        entries.append(RankedSector(
            symbol=symbol, momentum=mom, price=price, sma=sma,
            above_trend=above_trend, beats_spy=beats_spy, qualifies=qualifies,
        ))
    # Sectors with no momentum yet (insufficient history) sort last.
    entries.sort(key=lambda e: (e.momentum is None, -(e.momentum or 0)))
    for i, e in enumerate(entries, start=1):
        e.rank = i
    return entries


@dataclass
class TargetPosition:
    symbol: str
    rank: int
    target_weight: float  # fraction of equity, already capped


def target_portfolio(ranking: List[RankedSector], top_n: int = config.TOP_N_HOLD,
                      position_cap_pct: float = config.DEFAULT_DIALS["position_cap_pct"]
                      ) -> List[TargetPosition]:
    """Conviction-by-rank sizing. Weight scale is linear decay across the
    top_n slots (e.g. top_n=3 -> raw scale 3,2,1 summing to 6). Only
    QUALIFYING sectors can fill a slot; if fewer than top_n qualify, the
    remaining slots' share of the scale is simply left as cash -- it is
    never redistributed to the names that did qualify. Any weight beyond
    the per-name cap is also left as cash, not redistributed."""
    qualifying = [e for e in ranking if e.qualifies][:top_n]
    raw_scale_total = sum(range(1, top_n + 1))  # e.g. 1+2+3=6 for top_n=3
    out = []
    for slot_index, sector in enumerate(qualifying, start=1):
        raw_weight_units = top_n - slot_index + 1  # slot 1 gets top_n units
        raw_weight = raw_weight_units / raw_scale_total
        capped_weight = min(raw_weight, position_cap_pct)
        out.append(TargetPosition(symbol=sector.symbol, rank=slot_index, target_weight=capped_weight))
    return out


@dataclass
class ExitSignal:
    symbol: str
    reason: str  # 'RANK_DROP' | 'BELOW_TREND' | 'CATASTROPHE_STOP'
    detail: str


def check_exits(held_symbols: List[str], ranking_by_symbol: Dict[str, RankedSector],
                 entry_prices: Dict[str, float], current_prices: Dict[str, float],
                 exit_rank_hysteresis: float, catastrophe_stop_pct: float) -> List[ExitSignal]:
    """Daily exit checks ('exit fast'). Three independent triggers, first
    match wins per symbol (order below reflects escalating urgency)."""
    signals = []
    for symbol in held_symbols:
        entry_price = entry_prices.get(symbol)
        cur_price = current_prices.get(symbol)
        if entry_price and cur_price:
            drawdown = (cur_price - entry_price) / entry_price
            if drawdown <= -abs(catastrophe_stop_pct):
                signals.append(ExitSignal(
                    symbol=symbol, reason="CATASTROPHE_STOP",
                    detail=f"down {drawdown:.1%} from entry {entry_price:.2f}, stop is -{catastrophe_stop_pct:.0%}"))
                continue
        sector = ranking_by_symbol.get(symbol)
        if sector is None:
            continue
        if not sector.above_trend:
            signals.append(ExitSignal(
                symbol=symbol, reason="BELOW_TREND",
                detail=f"price {sector.price:.2f} below trend MA {sector.sma:.2f}" if sector.sma else "trend MA unavailable"))
            continue
        if sector.rank > int(exit_rank_hysteresis):
            signals.append(ExitSignal(
                symbol=symbol, reason="RANK_DROP",
                detail=f"rank {sector.rank} fell outside top-{int(exit_rank_hysteresis)}"))
            continue
    return signals


def is_rebalance_day(dt, rebalance_weekday: int = config.REBALANCE_WEEKDAY) -> bool:
    return dt.weekday() == rebalance_weekday
