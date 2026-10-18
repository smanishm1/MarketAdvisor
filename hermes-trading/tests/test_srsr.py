"""Offline tests for the core SRSR rules (hermes_trading/srsr.py) and the sizing maths.

Pure logic: no network, no database. Small hand-made price lists so every expected number can be
checked by eye. Run:
    .venv\\Scripts\\python tests\\test_srsr.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_trading import paper_broker as pb  # noqa: E402
from hermes_trading import srsr  # noqa: E402

FAILS = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def near(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ---- 1. momentum score ---------------------------------------------------------------------------
print("1. momentum score")
closes = [100, 100, 100, 110, 121]
ok(near(srsr.momentum_score(closes, [1, 2]), (0.10 + 0.21) / 2), "blend = plain average of the lookback returns (0.155)")
ok(srsr.momentum_score(closes, [1, 5]) is None, "not enough history for a lookback -> None (not a guess)")
ok(srsr.momentum_score([0, 100, 100, 110, 121], [4]) is None, "a zero price at the start of a lookback -> None")
ok(near(srsr.momentum_score(closes, [1, 2], [1, -1]), (0.10 - 0.21) / 2),
   "a negative weight uses that horizon in reverse, scaled by the sum of |weights|")
ok(near(srsr.momentum_score(closes, [1, 2], [2, 0]), 0.10), "a zero weight ignores that horizon")

# ---- 2. moving average and volatility ------------------------------------------------------------
print("2. moving average and volatility")
ok(near(srsr.sma([1, 2, 3, 4, 5], 3), 4.0), "3-bar average of the latest bars")
ok(srsr.sma([1, 2], 3) is None and srsr.sma([1, 2, 3], 0) is None, "too little history or n<=0 -> None")
ok(near(srsr.volatility([100, 100, 100, 100], 3), 0.0), "a flat series has zero volatility")
ok(near(srsr.volatility([100, 110, 99], 2), (0.02) ** 0.5), "returns +10% and -10% -> sample std dev 0.1414")
ok(srsr.volatility([100, 101], 2) is None and srsr.volatility([100, 101, 102], 1) is None, "not enough history -> None")

# ---- 3. evaluate: the two filters and the ranking ------------------------------------------------
print("3. evaluate: beat the market AND be above its own average, ranked by score")


def grow(rate, n=10, start=100.0):
    return [start * (1 + rate) ** i for i in range(n)]


cfg = {"momentum_lookbacks_days": [2, 4], "trend_sma_days": 8, "hold_top_n": 2, "exit_rank_n": 3, "max_positions": 2}
bench = [100.0 + i for i in range(10)]                        # +1 a day
prices = {
    "A": grow(0.03),                                          # strong and rising
    "B": grow(0.02),                                          # good and rising
    "D": grow(0.005),                                         # rising but slower than the market
    "C": grow(-0.02),                                         # falling
    "E": [200, 200, 200, 200, 200, 100, 100, 100, 110, 130],  # sharp rebound, still under its 8-day average
    "NEW": [100, 101, 102],                                   # not enough history to score
}
d = srsr.evaluate(prices, bench, cfg)
ok(d.eligible == ["A", "B"], f"only A and B pass both filters, best first ({d.eligible})")
ok("D" in d.scores and "D" not in d.eligible, "D has a score but does not beat the market, so it is excluded")
ok(d.scores["E"] > d.benchmark_score and "E" not in d.eligible, "E beats the market but is below its own average, so it is excluded")
ok("C" not in d.eligible, "C (falling) is excluded")
ok("NEW" not in d.scores, "a symbol without enough history is skipped, not scored")
ok(d.targets == ["A", "B"], "targets are the top hold_top_n eligible names")
ok(d.scores["A"] > d.scores["B"] > d.scores["D"], "scores order as expected")

d1 = srsr.evaluate(prices, bench, dict(cfg, hold_top_n=1))
ok(d1.targets == ["A"], "hold_top_n=1 holds only the best")

d2 = srsr.evaluate(prices, [100.0, 101.0], cfg)               # benchmark too short to score
ok(d2.benchmark_score is None and "D" in d2.eligible,
   "if the benchmark cannot be scored, the beat-the-market test is switched off (the average filter still applies)")
ok("C" not in d2.eligible and "E" not in d2.eligible, "the above-its-own-average filter still applies then")

# ---- 4. hold-until-it-drops-out rule (hysteresis) --------------------------------------------------
print("4. actions: keep a holding until it falls out of the top exit_rank_n")
dec = srsr.Decision(scores={s: 1.0 - i / 10 for i, s in enumerate("ABCDEF")}, eligible=list("ABCDEF"),
                    targets=["A", "B"], benchmark_score=0.0)
cfg4 = {"hold_top_n": 2, "exit_rank_n": 4, "max_positions": 2}
buys, sells = srsr.actions(dec, [], cfg4)
ok(buys == ["A", "B"] and sells == [], "empty book -> buy the top two")
buys, sells = srsr.actions(dec, ["A", "B"], cfg4)
ok(buys == [] and sells == [], "already holding the top two -> nothing to do")
buys, sells = srsr.actions(dec, ["C"], cfg4)
ok(sells == [] and buys == ["A"], "C is rank 3 (inside the band of 4): kept, and only ONE slot is left to fill, with the best name")
buys, sells = srsr.actions(dec, ["E"], cfg4)
ok(sells == [("E", "rank_drop")], "E is eligible but rank 5 (outside the band): sold as 'rank_drop'")
ok(buys == ["A", "B"], "and both slots are free for the top two")
buys, sells = srsr.actions(dec, ["X"], cfg4)
ok(sells == [("X", "trend_or_rs_exit")], "a holding that is no longer eligible at all is sold as 'trend_or_rs_exit'")
buys, sells = srsr.actions(dec, ["C", "E", "X"], cfg4)
ok(dict(sells) == {"E": "rank_drop", "X": "trend_or_rs_exit"} and buys == ["A"],
   "mixed book: E and X are sold, C is kept, and the freed slot buys the best name (A)")
cfg4b = {"hold_top_n": 2, "max_positions": 2}
_, sells = srsr.actions(dec, ["D"], cfg4b)
ok(sells == [("D", "rank_drop")], "exit_rank_n missing -> band is hold_top_n+1 (3): rank 4 is sold")
_, sells = srsr.actions(dec, ["C"], cfg4b)
ok(sells == [], "exit_rank_n missing -> rank 3 is still kept")

# ---- 5. single-stock budget ---------------------------------------------------------------------
print("5. single-stock rules")
cfg5 = {"hold_top_n": 3, "stocks": ["S1", "S2", "S3"], "max_stock_positions": 1,
        "position_notional_cap_pct": 35, "stock_notional_cap_pct": 20,
        "catastrophe_stop_pct": 15, "stock_catastrophe_stop_pct": 25}
ranked = ["S1", "S2", "E1", "E2", "E3"]
ok(srsr.pick_targets(ranked, cfg5) == ["S1", "E1", "E2"], "only one stock is allowed in: the second stock is skipped")
ok(srsr.pick_targets(ranked, cfg5, keep=["S2"]) == ["S2", "E1", "E2"],
   "an already-held stock counts against the budget and is never skipped")
ok(srsr.pick_targets(ranked, {"hold_top_n": 3}) == ranked[:3], "no stocks configured -> exactly the top N")
ok(srsr.cap_pct(cfg5, "S1") == 20 and srsr.cap_pct(cfg5, "E1") == 35, "stocks get the tighter position cap")
ok(srsr.stop_pct(cfg5, "S1") == 25 and srsr.stop_pct(cfg5, "E1") == 15, "stocks get the wider stop")
ok(srsr.cap_pct({"position_notional_cap_pct": 30, "stocks": ["S"]}, "S") == 30, "stock cap falls back to the ETF cap if not set")

# ---- 6. sizing ---------------------------------------------------------------------------------
print("6. sizing")
ok(near(pb.equal_weight_size(10000, 100, 3, 35), 10000 / 3 / 100), "equal weight: equity / slots / price")
ok(near(pb.equal_weight_size(10000, 100, 2, 35), 35.0), "the per-position cap binds when slots are few (35 shares, not 50)")
ok(pb.equal_weight_size(10000, 0, 3, 35) == 0.0 and pb.equal_weight_size(10000, 100, 0, 35) == 0.0, "zero price or zero slots -> 0 shares")

w = pb.inverse_vol_weights({"A": 0.01, "B": 0.02})
ok(near(w["A"], 2 / 3) and near(w["B"], 1 / 3) and near(sum(w.values()), 1.0), "inverse-vol: the calmer name gets twice the weight, sum is 1")
ok(pb.inverse_vol_weights({"A": 0.01, "B": 0.0}) == {"A": 1.0}, "a zero-vol name is dropped when others have a vol")
ok(pb.inverse_vol_weights({"A": 0.0, "B": 0.0}) == {"A": 0.5, "B": 0.5}, "all zero vol -> equal split")
ok(pb.inverse_vol_weights({}) == {}, "empty basket -> empty")

r = pb.rank_weights(["A", "B", "C"])
ok(near(r["A"], 3 / 6) and near(r["B"], 2 / 6) and near(r["C"], 1 / 6), "rank weights decay linearly: 3:2:1")
ok(all(near(v, 1 / 3) for v in pb.rank_weights(["A", "B", "C"], 0).values()), "power 0 collapses to equal weight")
ok(pb.rank_weights([]) == {}, "empty basket -> empty")

ok(pb.sizing_weights("equal_weight", ["A", "B"]) == {}, "equal_weight returns no weights (caller uses equal slots)")
rw = pb.sizing_weights("rank_weight", ["B", "A", "C"], scores={"A": 0.3, "B": 0.2, "C": 0.1})
ok(near(rw["A"], 0.5) and near(rw["B"], 1 / 3) and near(rw["C"], 1 / 6), "rank_weight orders the basket by score, whatever order it is given in")
iv = pb.sizing_weights("inverse_vol", ["A", "B"], vols={"A": 0.01, "B": 0.02})
ok(near(iv["A"], 2 / 3), "inverse_vol uses the vols given")

ok(near(pb.weighted_size("rank_weight", {"A": 0.5}, "A", 10000, 100, 3, 35), 35.0),
   "weighted slot is capped at the notional cap (0.5 of 10000 = 5000, cap 3500 -> 35 shares)")
ok(near(pb.weighted_size("rank_weight", {"A": 0.2}, "A", 10000, 100, 3, 35), 20.0), "a weight under the cap is used as is (2000 -> 20 shares)")
ok(near(pb.weighted_size("rank_weight", {"A": 0.2}, "Z", 10000, 100, 3, 35), 10000 / 3 / 100),
   "a name with no weight falls back to an equal slot")
ok(near(pb.weighted_size("equal_weight", {"A": 0.2}, "A", 10000, 100, 3, 35), 10000 / 3 / 100), "equal_weight ignores weights")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
