"""Sector Relative-Strength Rotation — pure signal & decision logic.

No I/O, no DB. Operates on price history and current holdings so the SAME code
drives both the live worker and the backtester (they can never disagree).

See docs/strategy-srsr.md for the full spec.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


def momentum_score(closes: Sequence[float], lookbacks_days: Sequence[int]) -> float | None:
    """Average total return over each lookback (e.g. 63 & 126 trading days)."""
    n = len(closes)
    rets: list[float] = []
    for lb in lookbacks_days:
        if n <= lb or closes[-1 - lb] == 0:
            return None
        rets.append(closes[-1] / closes[-1 - lb] - 1.0)
    return sum(rets) / len(rets) if rets else None


def sma(closes: Sequence[float], n: int) -> float | None:
    if len(closes) < n or n <= 0:
        return None
    return sum(closes[-n:]) / n


def volatility(closes: Sequence[float], n: int) -> float | None:
    """Sample std-dev of daily simple returns over the last `n` bars.

    Used for inverse-volatility sizing — a higher number means a choppier name
    that should get a smaller slice of the book. Returns None if there isn't
    enough history to form at least two returns.
    """
    if n <= 1 or len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets = [
        window[i] / window[i - 1] - 1.0
        for i in range(1, len(window))
        if window[i - 1]
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return var ** 0.5


@dataclass
class Decision:
    scores: dict[str, float] = field(default_factory=dict)   # symbol -> momentum score
    eligible: list[str] = field(default_factory=list)        # passed both filters, ranked desc
    targets: list[str] = field(default_factory=list)         # top-N eligible (the ones to hold)
    benchmark_score: float | None = None


# ---- single-stock risk rules -------------------------------------------------
# Names listed under cfg["stocks"] are individual companies mixed into the ETF
# rotation. They rank through the exact same dual-momentum gate, but carry their
# own (tighter) notional cap, (wider) catastrophe stop, and a slot budget so the
# book can never become an all-single-name portfolio.


def is_stock(cfg: dict[str, Any], sym: str) -> bool:
    return sym in (cfg.get("stocks") or [])


def cap_pct(cfg: dict[str, Any], sym: str) -> float:
    """Per-position notional cap %, tighter for single stocks than ETFs."""
    base = float(cfg.get("position_notional_cap_pct", 35))
    if is_stock(cfg, sym):
        return float(cfg.get("stock_notional_cap_pct", base))
    return base


def stop_pct(cfg: dict[str, Any], sym: str) -> float:
    """Catastrophe-stop %, wider for single stocks (noisier; risk is capped by size)."""
    base = float(cfg.get("catastrophe_stop_pct", 15))
    if is_stock(cfg, sym):
        return float(cfg.get("stock_catastrophe_stop_pct", base))
    return base


def pick_targets(
    eligible: Sequence[str], cfg: dict[str, Any], keep: Sequence[str] = ()
) -> list[str]:
    """Top-N eligible names honoring the single-stock slot budget.

    Walks the ranked eligible list and takes the best `hold_top_n` names, skipping
    any stock that would push the book past `max_stock_positions` (names already in
    `keep` count against the budget but are never skipped — exits handle those).
    With no stocks configured this is exactly `eligible[:hold_top_n]`.
    """
    top_n = int(cfg.get("hold_top_n", 3))
    stocks = set(cfg.get("stocks") or [])
    max_stock = int(cfg.get("max_stock_positions", top_n))
    stock_ct = sum(1 for h in keep if h in stocks)
    out: list[str] = []
    for s in eligible:
        if len(out) >= top_n:
            break
        if s in stocks and s not in keep:
            if stock_ct >= max_stock:
                continue
            stock_ct += 1
        out.append(s)
    return out


def evaluate(prices: dict[str, Sequence[float]], benchmark: Sequence[float], cfg: dict[str, Any]) -> Decision:
    """Rank the universe and apply the dual-momentum filters at the latest bar."""
    lookbacks = cfg.get("momentum_lookbacks_days", [63, 126])
    sma_n = int(cfg.get("trend_sma_days", 200))

    bench_score = momentum_score(benchmark, lookbacks)

    scores: dict[str, float] = {}
    eligible: list[str] = []
    for sym, closes in prices.items():
        ms = momentum_score(closes, lookbacks)
        if ms is None:
            continue
        scores[sym] = ms
        trend = sma(closes, sma_n)
        relative_ok = (bench_score is None) or (ms > bench_score)   # beats the market
        absolute_ok = (trend is not None) and (closes[-1] > trend)  # above its own 200d
        if relative_ok and absolute_ok:
            eligible.append(sym)

    eligible.sort(key=lambda s: scores[s], reverse=True)
    return Decision(
        scores=scores,
        eligible=eligible,
        targets=pick_targets(eligible, cfg),
        benchmark_score=bench_score,
    )


def actions(
    decision: Decision, holdings: Sequence[str], cfg: dict[str, Any]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Translate a decision + current holdings into (buys, sells-with-reason).

    Keep a holding only if it's still inside the top `exit_rank_n` eligible names
    (hysteresis). Anything else is sold: 'trend_or_rs_exit' if it failed a filter,
    'rank_drop' if it merely slipped past the hysteresis band. Buys re-pick the
    ideal top-N book around the keeps so kept single stocks count against the
    `max_stock_positions` budget.
    """
    top_n = int(cfg.get("hold_top_n", 3))
    exit_rank = int(cfg.get("exit_rank_n", top_n + 1))
    max_pos = int(cfg.get("max_positions", top_n))

    keep_band = set(decision.eligible[:exit_rank])
    keep = [h for h in holdings if h in keep_band]

    sells: list[tuple[str, str]] = []
    for h in holdings:
        if h in keep_band:
            continue
        reason = "rank_drop" if h in decision.eligible else "trend_or_rs_exit"
        sells.append((h, reason))

    slots = max(0, max_pos - len(keep))
    targets = pick_targets(decision.eligible, cfg, keep=keep)
    buys = [s for s in targets if s not in keep][:slots]
    return buys, sells
