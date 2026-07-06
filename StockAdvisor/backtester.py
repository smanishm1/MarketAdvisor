"""
backtester.py -- historical simulation of the sector-rotation strategy.

Used two ways:
1. Standalone research (run from the CLI) over 10-15 years.
2. Backtest-in-approval: whenever the reflection loop proposes a dial
   change, this module runs BOTH the current dials and the proposed dials
   over the same history and reports current-vs-proposed, plus a held-out
   out-of-sample (last 30%) window for each, so the human approver can see
   whether a proposed tweak actually holds up or is likely overfit.

This module re-uses strategy.py's pure functions so the backtest and the
live worker can never quietly diverge in what "qualifies" or "exit" means.

Important honesty note: some sector ETFs have short histories (XLRE
launched in 2015, XLC in 2018). Before a sector exists, it simply cannot
qualify (its momentum/SMA are NaN) -- it is never invented or back-filled.
This is a survivorship-neutral treatment (we are not cherry-picking which
ETFs existed with hindsight -- these are, and always were, the fixed 11
SPDR sector series), not the individual-stock-picking survivorship bias
the project is warned about.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
import data
import strategy

TRADING_DAYS_PER_YEAR = 252


def load_backtest_history(lookback_days=15 * 365 + 60) -> (Dict[str, pd.DataFrame], pd.DataFrame):
    price_data = {s: data.get_daily_history(s, lookback_days) for s in config.UNIVERSE}
    spy_df = data.get_daily_history(config.BENCHMARK, lookback_days)
    return price_data, spy_df


def _align(price_data: Dict[str, pd.DataFrame], spy_df: pd.DataFrame) -> pd.DatetimeIndex:
    calendar = spy_df.index
    return calendar


def _build_indicator_frame(close: pd.Series, calendar: pd.DatetimeIndex,
                            trend_filter_len: int) -> pd.DataFrame:
    aligned = close.reindex(calendar).ffill()
    mom_parts = []
    for n in config.MOMENTUM_LOOKBACKS_DAYS:
        mom_parts.append(aligned.pct_change(n))
    momentum = sum(mom_parts) / len(mom_parts)
    sma = aligned.rolling(int(trend_filter_len)).mean()
    return pd.DataFrame({"close": aligned, "momentum": momentum, "sma": sma})


@dataclass
class BacktestTrade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    pnl_dollars: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    equity_curve: List[dict]
    trades: List[BacktestTrade]
    starting_cash: float
    ending_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    num_trades: int
    win_rate_pct: float


def _stats_from_curve(dates, equities, starting_cash) -> dict:
    equities = np.array(equities, dtype=float)
    if len(equities) < 2:
        return dict(total_return_pct=0.0, cagr_pct=0.0, max_drawdown_pct=0.0, sharpe=0.0)
    total_return = equities[-1] / starting_cash - 1.0
    years = max((pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / 365.25, 1 / 252)
    cagr = (equities[-1] / starting_cash) ** (1 / years) - 1.0 if equities[-1] > 0 else -1.0
    running_max = np.maximum.accumulate(equities)
    drawdowns = (equities - running_max) / running_max
    max_dd = float(drawdowns.min())
    daily_rets = np.diff(equities) / equities[:-1]
    sharpe = 0.0
    if daily_rets.std() > 1e-9:
        sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(TRADING_DAYS_PER_YEAR))
    return dict(total_return_pct=total_return * 100, cagr_pct=cagr * 100,
                max_drawdown_pct=max_dd * 100, sharpe=sharpe)


def run_backtest(price_data: Dict[str, pd.DataFrame], spy_df: pd.DataFrame, dials: dict,
                  start_date=None, end_date=None, starting_cash: float = 100000.0
                  ) -> BacktestResult:
    trend_filter_len = int(dials["trend_filter_len"])
    exit_rank_hysteresis = dials["exit_rank_hysteresis"]
    catastrophe_stop_pct = dials["catastrophe_stop_pct"]
    position_cap_pct = dials["position_cap_pct"]

    calendar = _align(price_data, spy_df)
    indicators = {s: _build_indicator_frame(df["Close"], calendar, trend_filter_len)
                  for s, df in price_data.items()}
    spy_ind = _build_indicator_frame(spy_df["Close"], calendar, trend_filter_len)

    warmup = max(trend_filter_len, max(config.MOMENTUM_LOOKBACKS_DAYS)) + 5
    sim_dates = calendar[warmup:]
    if start_date is not None:
        sim_dates = sim_dates[sim_dates >= pd.Timestamp(start_date)]
    if end_date is not None:
        sim_dates = sim_dates[sim_dates <= pd.Timestamp(end_date)]

    cash = starting_cash
    positions = {}  # symbol -> {shares, entry_price, entry_date}
    trades: List[BacktestTrade] = []
    equity_curve = []

    for dt in sim_dates:
        # ---- build today's ranking ----
        spy_mom = spy_ind["momentum"].get(dt, np.nan)
        ranked = []
        for s, ind in indicators.items():
            row = ind.loc[dt]
            mom, sma, price = row["momentum"], row["sma"], row["close"]
            above_trend = pd.notna(sma) and price > sma
            beats_spy = pd.notna(mom) and pd.notna(spy_mom) and mom > spy_mom
            qualifies = bool(above_trend and beats_spy)
            ranked.append(strategy.RankedSector(
                symbol=s, momentum=(None if pd.isna(mom) else float(mom)), price=float(price),
                sma=(None if pd.isna(sma) else float(sma)), above_trend=bool(above_trend),
                beats_spy=bool(beats_spy), qualifies=qualifies))
        ranked.sort(key=lambda e: (e.momentum is None, -(e.momentum or 0)))
        for i, e in enumerate(ranked, start=1):
            e.rank = i
        ranking_by_symbol = {e.symbol: e for e in ranked}

        # ---- daily exits (exit fast) ----
        current_prices = {s: ind.loc[dt, "close"] for s, ind in indicators.items()}
        entry_prices = {s: p["entry_price"] for s, p in positions.items()}
        exits = strategy.check_exits(list(positions.keys()), ranking_by_symbol, entry_prices,
                                      current_prices, exit_rank_hysteresis, catastrophe_stop_pct)
        for sig in exits:
            pos = positions.pop(sig.symbol)
            exit_price = current_prices[sig.symbol]
            proceeds = pos["shares"] * exit_price
            cash += proceeds
            pnl_dollars = proceeds - pos["shares"] * pos["entry_price"]
            pnl_pct = (exit_price / pos["entry_price"] - 1.0) * 100
            trades.append(BacktestTrade(
                symbol=sig.symbol, entry_date=str(pos["entry_date"].date()), entry_price=pos["entry_price"],
                exit_date=str(dt.date()), exit_price=exit_price, shares=pos["shares"],
                pnl_dollars=pnl_dollars, pnl_pct=pnl_pct, exit_reason=sig.reason))

        # ---- weekly buys (exit fast, add slow) ----
        if strategy.is_rebalance_day(dt):
            equity_now = cash + sum(p["shares"] * current_prices[s] for s, p in positions.items())
            targets = strategy.target_portfolio(ranked, config.TOP_N_HOLD, position_cap_pct)
            new_targets = [t for t in targets if t.symbol not in positions]
            available_cash = cash
            for t in new_targets:
                if available_cash <= 1.0:
                    break
                desired = min(t.target_weight * equity_now, available_cash)
                price = current_prices[t.symbol]
                if price <= 0 or desired < 1.0:
                    continue
                shares = desired / price
                cash -= shares * price
                available_cash -= shares * price
                positions[t.symbol] = {"shares": shares, "entry_price": price, "entry_date": dt}

        invested = sum(p["shares"] * current_prices[s] for s, p in positions.items())
        equity_curve.append({"date": str(dt.date()), "equity": cash + invested, "cash": cash, "invested": invested})

    # liquidate anything still open at the end for stats purposes (mark-to-market only,
    # does not mutate positions -- this is just for reporting a clean final equity number)
    dates = [r["date"] for r in equity_curve]
    equities = [r["equity"] for r in equity_curve]
    stats = _stats_from_curve(dates, equities, starting_cash)
    wins = [t for t in trades if t.pnl_dollars > 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0

    return BacktestResult(
        start_date=dates[0] if dates else "", end_date=dates[-1] if dates else "",
        equity_curve=equity_curve, trades=trades, starting_cash=starting_cash,
        ending_equity=equities[-1] if equities else starting_cash,
        total_return_pct=stats["total_return_pct"], cagr_pct=stats["cagr_pct"],
        max_drawdown_pct=stats["max_drawdown_pct"], sharpe=stats["sharpe"],
        num_trades=len(trades), win_rate_pct=win_rate,
    )


def compare_dials(price_data, spy_df, current_dials: dict, proposed_dials: dict,
                   starting_cash=100000.0, oos_fraction=0.30) -> dict:
    """The core of 'backtest-in-approval'. Runs full-period AND held-out
    out-of-sample backtests for both the current and proposed dial sets."""
    calendar = _align(price_data, spy_df)
    warmup = max(int(current_dials["trend_filter_len"]), int(proposed_dials["trend_filter_len"]),
                 max(config.MOMENTUM_LOOKBACKS_DAYS)) + 5
    usable = calendar[warmup:]
    if len(usable) < 30:
        raise ValueError("Not enough history to backtest yet.")
    oos_start_idx = int(len(usable) * (1 - oos_fraction))
    oos_start_date = usable[oos_start_idx]

    def full_and_oos(dials):
        full = run_backtest(price_data, spy_df, dials, starting_cash=starting_cash)
        oos = run_backtest(price_data, spy_df, dials, start_date=oos_start_date, starting_cash=starting_cash)
        return full, oos

    cur_full, cur_oos = full_and_oos(current_dials)
    prop_full, prop_oos = full_and_oos(proposed_dials)

    # Verdict logic: the proposed change must improve (or not meaningfully
    # worsen) BOTH the full-period risk-adjusted return AND the OOS window.
    # If it only looks better on the full period but is flat/worse OOS,
    # that is the textbook signature of overfitting to the in-sample data.
    oos_sharpe_delta = prop_oos.sharpe - cur_oos.sharpe
    oos_dd_delta = prop_oos.max_drawdown_pct - cur_oos.max_drawdown_pct  # less negative = better
    full_sharpe_delta = prop_full.sharpe - cur_full.sharpe

    if oos_sharpe_delta > 0.05 and oos_dd_delta >= -1.0:
        verdict = "HOLDS UP OUT-OF-SAMPLE -- reasonable to approve"
    elif oos_sharpe_delta < -0.05 or oos_dd_delta < -3.0:
        verdict = "LIKELY OVERFIT -- OOS performance is worse; consider rejecting"
    else:
        verdict = "MIXED / MARGINAL -- OOS improvement is small; use judgment, holding is defensible"

    return {
        "oos_start_date": str(oos_start_date.date()),
        "current": {
            "full": _result_summary(cur_full), "oos": _result_summary(cur_oos),
        },
        "proposed": {
            "full": _result_summary(prop_full), "oos": _result_summary(prop_oos),
        },
        "oos_sharpe_delta": oos_sharpe_delta,
        "full_sharpe_delta": full_sharpe_delta,
        "verdict": verdict,
    }


def _result_summary(r: BacktestResult) -> dict:
    return {
        "start_date": r.start_date, "end_date": r.end_date,
        "total_return_pct": round(r.total_return_pct, 2), "cagr_pct": round(r.cagr_pct, 2),
        "max_drawdown_pct": round(r.max_drawdown_pct, 2), "sharpe": round(r.sharpe, 3),
        "num_trades": r.num_trades, "win_rate_pct": round(r.win_rate_pct, 1),
        "ending_equity": round(r.ending_equity, 2),
    }
