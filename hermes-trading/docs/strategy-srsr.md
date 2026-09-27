# Sector Relative-Strength Rotation (SRSR)

*Paper · long-only · ETF-core + a capped single-stock sleeve · human-approved. A disciplined
dual-momentum rotation, borrowing the governance/phasing discipline of the "Argus" Phase-3
framework and filling in the entry/exit/sizing rules it left unspecified.*

> Status: **built and running** (live paper trading). This doc tracks the *implemented*
> design — keep it in sync whenever the strategy or approval flow changes.

> **Changelog:** v02 trend_sma_days 200→250, exit_rank_n 4→6 (OOS-validated). v03 sizing
> equal_weight→rank_weight (OOS-validated). **v04** added `JEPI`,`JEPQ` to the universe and
> opened a 4th slot (`hold_top_n` 3→4) — a discretionary addition, **not** a backtested
> optimization (see §1 and §10). **v05** mixed 7 liquid mega-cap **individual stocks** into
> the universe under their own risk rules (20% cap · 25% stop · max 2 of 4 slots) — also a
> discretionary structural change; see §1a, §10 for the honest caveats and the backtest delta.
> **v06** widened the ETF catastrophe stop 15→30% (a loosening — trend/rank are the real exits).
> **v07** tightened the stock sleeve to **max 1 of the 4 slots** (was 2) — OOS-validated: vs
> max‑2, nearly identical return for ~4pp less drawdown and a higher Sharpe (§1a).
> **v08** is a **no-op version bump** — the `fake_hermes` stub re-proposed `catastrophe_stop_pct
> 30→30` (already 30 since v06) and it was approved, re-dumping the file with no real change.
> Config is functionally identical to v07.
> **v09** added an **earnings-blackout guard** for the stock sleeve (`earnings_blackout_days`: don't
> *open* a stock within N days of its earnings) plus **news headlines** as context on approval cards,
> Discord embeds, and the morning brief. Both live on the *human* side — no ranking/filter/sizing
> change (§1a, §8a, §10).
> **v10** (reflection loop, fallback rule): `exit_rank_n` 6→7. A drift review later confirmed it
> helped (net 20y Sharpe 0.83 vs 0.75 at 6; better in every crash regime).
> **v11** `hold_top_n` 4→6 and `exit_rank_n` 7→9 (band = slots+3) — the first change through the new
> gate (§8c): auto-backtested (net 20y Sharpe 0.83→0.87, OOS 0.56→0.59, turnover 12.5×→8.8×/yr) and
> approved in Discord with the reason *"Improved sharp to .87"*. Research in §12.
> **v12** `catastrophe_stop_pct` 30→15 — undoes the v06 test-stub drift (§8c). Backtest-neutral against
> v11 (Sharpe 0.87→0.88, OOS and max DD unchanged); roughly halves the worst-case single-position loss
> before the backstop fires (~4.5% vs ~9% of equity at a ~30% position). Auto-re-based from v10 to v11
> before approval; approved in Discord with the reason *"Reverting back to prior setting"*.
> **Sept 2026 — infrastructure, no strategy version change:** all performance is now measured
> **net** (§8b: trading costs, T-bill interest on cash, excess-return Sharpe); every strategy change
> is **auto-backtested before a human can approve it**, needs a **one-line reason**, and the live
> settings are compared against the **original v01** in a drift report (§8c). The live book was
> **re-based to $10,000** (closed trades archived, open positions re-marked) to start measuring the
> new era cleanly. Research on more slots, flatter weights and a 1-month signal is in §12.
> **2026-09-27 — second, separate stream:** an options book ("the wheel" on healthy-FCF
> companies, its own $10K) now runs alongside SRSR. It shares no cash, slots or settings with
> this strategy. Spec: [strategy-wheel.md](strategy-wheel.md).

---

## 1. Universe
The 11 SPDR sector ETFs plus 2 income / covered-call ETFs (added v04), plus 7 mega-cap
single stocks (added v05):

`XLK` tech · `XLF` financials · `XLV` health · `XLE` energy · `XLI` industrials ·
`XLY` consumer disc. · `XLP` staples · `XLU` utilities · `XLB` materials ·
`XLRE` real estate · `XLC` communications · `JEPI` · `JEPQ` (income/covered-call) ·
**`NVDA` · `MSFT` · `AAPL` · `GOOGL` · `AMZN` · `META` · `AVGO`** (single stocks, §1a)

`JEPI`/`JEPQ` are **candidates only** — they pass through the exact same momentum gate as
everything else (§3). Covered-call ETFs cap their upside to generate income, so in a strong
up-tape they trail SPY's momentum and usually sit out; they are bought only if/when they earn
a slot. (This is a deliberate user addition, not a performance-optimized change.)

`SPY` is the **benchmark** — used for the relative filter, **never traded**.
Data source: **yfinance** (free, no key), daily bars, ~260 days of history.

## 1a. Single-stock sleeve *(v05)*
The 7 stocks are listed under `stocks:` in `strategy.yaml` and rank through the **same
dual-momentum gate** as the ETFs (§3) — no special entry treatment. What differs is the
risk envelope, because single names carry earnings/idiosyncratic risk an ETF doesn't:

| rule | ETF | single stock |
|---|---|---|
| notional cap per position | 35% | **20%** (`stock_notional_cap_pct`) |
| catastrophe stop | −15% | **−25%** (`stock_catastrophe_stop_pct` — wider; single names are noisier, risk is capped by the smaller size instead) |
| slots | up to 6 | **max 1 of the 6** (`max_stock_positions`; v07, was 2) — the book can never become single-name-heavy |
| earnings timing | n/a | **skip opening within `earnings_blackout_days` (5) of earnings** (v09) — a stock in blackout is not proposed and its slot stays cash, to dodge buying right before an earnings gap. Entry-timing only; a name *held through* earnings still carries gap risk. |

Slot budgeting is enforced in the shared signal code (`srsr.pick_targets`), so the live
worker and the backtester can't disagree: when a stock would exceed the budget, the next
eligible ETF takes the slot instead. Held stocks count against the budget while they remain
in the hysteresis band.

**Backtest delta** (same 10y window 2017→2026, v04 pure-ETF → v05 with stocks):
CAGR +11.2% → +16.5%, Sharpe 0.86 → 0.92, max drawdown −17.7% → **−26.1%** (SPY: −33.7%).
More return, meaningfully more drawdown — and see §10 for why the CAGR gain is inflated by
hindsight (these names were picked *because* they won the last decade).

**Sleeve sizing (the v07 decision — out-of-sample, 2023→2026, the trustworthy window):**
max 2 stocks → CAGR +10.8% / maxDD −23.8% / Sharpe 0.70 · **max 1 (chosen)** → +10.1% /
**−19.4%** / **0.74** · no sleeve → +7.8% / −17.7% / 0.68. Max‑1 keeps almost all the return
for much less drawdown and the best Sharpe. (None beat SPY's +19.2% OOS — this is a defensive
book that earns its edge across full cycles, not in a bull run.)

## 2. Signal
For each name: **momentum score = average of its 3-month and 6-month total return**
(≈ 63 and 126 trading days). Rank all 20 highest-first. Compute SPY's score the same way.

## 3. Buy eligibility — must pass BOTH (dual momentum)
1. **Relative:** momentum score **> SPY's** (stronger than the market).
2. **Absolute:** latest close **> its own 250-day SMA** (the name is in an uptrend).

## 4. Holdings & cash
- Fill up to **6 slots** (v11; 4 since v04, 3 originally) with the top-ranked names that pass
  both filters (at most 1 of them a single stock, §1a; v07, was 2). In practice only ~4.7 names
  qualify on average, so the eligibility gate — not the slot count — usually sets the book size.
- **Any slot that can't be filled stays in CASH.** Cash is the primary downside defense: with few
  names qualifying, each is capped (35%) and the rest stays in cash; with none (bear market), the
  book is 100% cash.
- Max **6 open positions**, one per symbol.
- **Approved buys fill at the live price at approval time** (not the stale proposal-time
  price), so a freshly-opened position starts at ~0 unrealised rather than showing a gap that
  is really just the daily-close-vs-intraday series mismatch.

## 5. Sizing — CONVICTION BY RANK *(v03; was equal-weight, was risk-unit)*
- `sizing: rank_weight`, `rank_power: 1` — each held name's target weight ∝ `(N − rank)`
  (best name first), normalized to sum to 1. With 3 names that's ~50 / 33 / 17% before the
  cap; with 6 names (v11) it's ~29 / 24 / 19 / 14 / 10 / 5%. These are *entry* weights — winners
  are not trimmed afterwards, so realised concentration drifts higher (§10, §12).
- Capped at **35% notional** per position (**20%** for single stocks, §1a). The cap *does* bind on the #1 name (which wants
  ~40–50% depending on how many qualify): the overflow goes to **cash** (~18% avg vs ~8% under
  equal-weight) — that cash is the drawdown cushion. `rank_power: 0` collapses to equal-weight;
  `2` over-concentrates (mostly hoards cash) — don't. Empty slots = cash (see §4). No leverage;
  total invested ≤ 100%.

> **Why conviction-by-rank (and why not the others)?** In a *momentum* book, the highest-ranked
> name has the highest expected return, so weighting toward it leans **into** the signal. Validated
> out-of-sample across 4 splits: vs equal-weight, +0.10–0.13 Sharpe, +0.3–0.6pp CAGR, maxDD −16.4%
> vs −20.9%, train Sharpe flat (not overfit). The CAGR gain is real conviction alpha (it survives
> even fully-invested/no-cap); the drawdown gain comes from the 35% cap diverting #1's overflow to
> cash. **Inverse-vol sizing was tested and rejected** — it does the opposite (underweights the
> volatile winners) and lost ~1.5pp CAGR / 0.08 Sharpe OOS. **Risk-unit sizing (the Argus model)**
> pins notional at `risk$ ÷ stop%` (e.g. 1% risk / 8% stop ⇒ ~37% invested, ~63% cash in a bull
> market) — that under-investment defeats a long-only rotation; it's the right tool for
> concentrated single-name bets (the leaders Phase), not a diversified-ETF rotation.

## 6. Exits
Primary (the strategy's own logic does the work):
- **Trend break:** weekly close below the 250-day SMA → sell, go to cash.
- **Rank drop:** at a rebalance, no longer in the **top 6** (`exit_rank_n`, hysteresis vs. the
  top-4 held, to avoid churning a name hovering at the boundary) → sell.

Backstop (fast, for gaps/crashes only):
- **Catastrophe stop: 15% below entry** (25% for single stocks, §1a), checked daily. *(Was
  widened to 30% in v06 by the test stub and restored to 15% in v12. Originally an 8% hard stop,
  which fought the strategy by knocking you out of valid uptrends on normal pullbacks — trend +
  rank are the real exits; the stop is a backstop and can't prevent a gap-through loss.)*

## 7. Cadence
**Rebalance weekly** (Friday close): propose new buys, flag exits. Between rebalances only the
catastrophe stop is monitored daily. Weekly (not daily) deliberately — sector momentum is slow;
daily adds noise.

## 8. Governance & approvals (kept from Argus Phase 3)
Long-only · ETF core with a capped single-stock sleeve (§1a) · max 6 positions · 35%/20%
notional caps · **mandatory human approval** (enforced by the app's approval queue) · cash
always allowed. Single-name/earnings risk exists since v05 but is bounded by the sleeve rules.
**Graduate to Phase 2 only after ~3 months of boring, rule-following paper trading** —
discipline in the journal, not just green P/L.

Approvals can be actioned **in the dashboard or in Discord**: an optional bot posts each pending
trade and strategy change with **Approve / Reject** buttons (and a **Backtest** button on strategy
changes). A click only flips the pending row's status — the worker still fills/applies it on its
next tick — so the human-approval invariant holds, and both surfaces stay in sync (act in one and
the other updates).

## 8a. Memory & the morning brief *(app features around the strategy)*
- **Trade-to-trade context:** every buy proposal records *why* it was proposed — momentum
  rank, score vs SPY, whether it's a single stock, and how the **last round-trip in that
  symbol** ended (pnl, exit reason). The context shows on the approval card and is carried
  onto the filled trade (`trades.context`), so the trade log remembers its own reasoning.
- **Strategy-to-strategy context:** the reflection prompt now includes the full **strategy
  lineage** (every past change with the human's applied/rejected verdict), **per-version
  performance** (closed trades grouped by the version that opened them), and recent
  reflection decisions including holds — with an explicit instruction not to re-propose
  rejected changes or ping-pong a dial. Surfaced in the dashboard **Journal** panel.
- **Morning brief:** generated automatically once a day (first worker tick after 08:00
  local, from data the tick already fetched — no API cost) and on demand via **Brief now**.
  Covers: SPY vs trend, universe rankings, holdings health (rank, distance to exit band,
  stops), what's next in line, active brakes, **headlines** (§ below), and the last 24h
  (opened/closed trades, pending approvals, last reflection). Stored per-date in `briefs`.
- **News & earnings context *(v09)*:** best-effort **headlines** (yfinance, free, cached
  hourly) surface wherever the human decides — on each buy **approval card**, in the **Discord**
  embed, and in the brief's **Headlines** section — plus a stock's **next earnings date**. It is
  decision support only: it changes no ranking, filter, or sizing. The earnings *date* also drives
  the §1a blackout guard. All calls fail soft (no headlines / no date → the card just omits them);
  `hermes_trading.news`.
- **Shadow-SPY comparison (fairness-scoped benchmark):** every fill snapshots SPY
  (`trades.spy_entry`) and every close snapshots it again (`spy_exit`), so each trade is
  compared to *the same dollars in SPY over its own holding window* — SPY only competes
  **while a position is open**; cash periods count for neither side. Open positions mark
  their shadow live. Shown in the dashboard's **vs S&P 500** panel (aggregate alpha, hit
  rate, per-trade rows). This isolates selection skill and deliberately excludes cash
  drag — the backtester's SPY buy-&-hold column remains the whole-strategy benchmark.
  Snapshots are price-only (no dividends): understates SPY ≈0.1%/month held.
  (`hermes_trading.spy_compare`; legacy trades backfilled from daily closes.)

## 8b. Measurement — net of frictions *(Sept 2026)*
Configured in `goal.yaml → execution:` and applied identically in the backtester and the live
paper book (`hermes_trading.execution`):
- **Trading costs:** `cost_bps_per_side: 5` — every buy fills 5 bps above the quote and every
  sell 5 bps below (half-spread + slippage; 10 bps round trip). The live book records net fills.
- **Interest on cash:** idle cash earns the **13-week T-bill (`^IRX`), time-varying** (≈0% in
  2020-21, ≈5% in 2023-24) — daily in the backtest, credited once a day to the live book.
- **Sharpe in excess of the T-bill rate** — for the strategy *and* SPY, so the comparison stays
  fair. (`sharpe_no_rf` is still reported for reference.)
- Backtests now default to **20 years** (from 2007, so the GFC is included) with a held-out
  out-of-sample window (last 30%: 2020-26, which includes the 2020 crash and 2022 bear) and a
  **per-regime table** (2008 crash, 2009 rebound, 2011, 2015-16 chop, 2018 Q4, 2020 crash/rebound,
  2022 bear, 2023-26 bull). Also reported: costs paid, interest earned, turnover, average
  holdings, effective number of holdings (1/Σw²), average top-position weight.

**Re-baselined numbers (v10 config, net):** 10y Sharpe **0.85 → 0.69** (SPY 0.71); 20y Sharpe
**0.93 → 0.83** (SPY 0.55), CAGR 14.3%, max DD −21.9% (SPY −55%). Costs (~$5.5k over 20y) and cash
interest (~$4.6k) nearly offset; the risk-free adjustment is the big correction. Turnover is
**~12.5× equity/yr** (≈5-week average hold). By regime: strong protection in crashes (GFC −18% vs
SPY −55%, 2022 −9.5% vs −24.5%) but slow off the bottom (2009 rebound +21% vs SPY +67%).

## 8c. Governance — backtest before approval, reasons, drift *(Sept 2026)*
(`hermes_trading.review`, `hermes_trading.drift`)
- **Every strategy change is backtested before it can be approved.** The worker backtests each
  pending proposal automatically (original v01 vs current vs proposed, 20y net, OOS + regimes).
  **Approve stays locked** — dashboard, Discord and API — until the result is shown.
- **One line of reasoning on every decision.** Approve and reject both require a reason (dashboard
  text box, Discord pop-up). Stored on the proposal, appended to `state/decisions.jsonl`, and fed
  to the reflection brain via the lineage ("respect the human's stated reasons").
- **No stale approvals.** Proposals store a structured diff (`changes_json`). If the live strategy
  moves before a proposal is decided (or applied), it is re-based onto the current settings and
  re-backtested; a change that's already in effect is auto-closed.
- **No no-op changes.** A proposal that changes nothing (the `30 → 30` stub bug that produced v08)
  is recorded as a hold, never queued.
- **Drift vs the original.** `config/baseline.yaml` freezes the original v01 design. The dashboard
  **Drift** panel and the morning brief compare every key setting (stop loss, position caps,
  rolling windows, dropout rank, slots, sizing, universe…) against it, label which way each change
  leans (more aggressive / more cautious), and flag which changes the **reflection loop** made (vs
  human edits). The worker re-backtests original-vs-live weekly. First report: 11 of 15 settings
  moved (7 more aggressive, 2 more cautious); the loop itself moved 2, **both toward more
  aggressive** — the ETF stop 15→30% (set by the `fake_hermes` *test stub*, which always answers
  30; backtest-neutral — reverted to 15% in v12) and the dropout rank 6→7 (beneficial).
  Original v01 net 20y Sharpe 0.41 vs 0.83 live — the human-made changes clearly helped.

## 9. App config (`strategy.yaml`)
```yaml
version: "12"                         # v12 = ETF stop back to 15% (see changelog)
earnings_blackout_days: 5             # v09: skip OPENING a stock within N days of its earnings
type: relative_strength_rotation
universe: [XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLU, XLB, XLRE, XLC, JEPI, JEPQ,
           NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO]
stocks: [NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO]   # single-stock sleeve (v05, §1a)
benchmark: SPY
momentum_lookbacks_days: [63, 126]   # 3mo + 6mo blend
trend_sma_days: 250                   # OOS-validated (was 200)
hold_top_n: 6                         # number of slots (v11; was 4 from v04, 3 originally)
exit_rank_n: 9                        # dropout band = slots+3 (v02 4->6, v10 6->7, v11 7->9)
# momentum_weights: [..]              # optional, one per lookback; negative = reversal (§12). Absent = equal.
rebalance: weekly                     # Fridays at close
sizing: rank_weight                   # conviction-by-rank (OOS-validated, was equal_weight)
rank_power: 1                         # linear decay; weight proportional to (N - rank)
position_notional_cap_pct: 35
catastrophe_stop_pct: 15              # backstop only (v06 stub widened to 30; restored in v12)
stock_notional_cap_pct: 20            # v05: tighter per-stock cap
stock_catastrophe_stop_pct: 25        # v05: wider per-stock stop
max_stock_positions: 1                # v07: at most 1 of the slots (was 2; OOS-validated)
max_positions: 6                      # always mirrors hold_top_n (see config.load_strategy)
```
Trades still hit the **approval queue**, and Hermes reflection still applies — proposing
one-variable tweaks to the *effective* tuning dials only: `trend_sma_days`, `exit_rank_n`,
`catastrophe_stop_pct`, `position_notional_cap_pct`, `stock_notional_cap_pct`. Structural
choices (`hold_top_n` / `max_positions`, universe, `stocks`, `max_stock_positions`, sizing)
are **locked to the agent** — `max_positions` always mirrors `hold_top_n`. A human can still
change them by editing `strategy.yaml` directly (that's how v04 added JEPI/JEPQ and v05 added
the stock sleeve); the worker picks the file up live on its next tick.

## 10. Honest caveats
- Momentum **lags turns** — you buy strength (late to new leaders) and sell weakness (give a
  little back at tops). Inherent; the filters + cash limit it.
- The 250-day filter is **slow** and checks weekly, so a fast crash draws down *some* before the
  switch flips — the 15% stop is the faster backstop. **Not crash-proof.**
- **Whipsaw** in choppy markets (sell to cash, rebuy) — hysteresis + weekly cadence soften it.
- A handful of sectors aren't truly diversified in a market-wide selloff — the 250-day/cash rule
  is the answer, not position count.
- **Income ETFs in a momentum book rarely qualify.** `JEPI`/`JEPQ` are in the universe but, by
  design, lag SPY's momentum in rising markets, so they will often sit out. If the goal is to
  *hold* them for income/defense regardless of momentum, that needs a different mechanism (a fixed
  sleeve) — the rotation will not force them in.
- **The v05 stock list is survivorship-biased.** NVDA/MSFT/AAPL/GOOGL/AMZN/META/AVGO were picked
  in 2026 *because* they dominated the last decade — a backtest over that same decade inevitably
  flatters them (the +5pp CAGR in §1a is an optimistic ceiling, not an expectation). The forward
  protection is structural, not statistical: same momentum gate, tighter cap, wider stop, and
  only **max 1 slot** (v07). At max 1 the sleeve adds little drawdown vs the pure-ETF book
  (≈−19% vs −18% OOS — the tightened budget nearly closes the gap that max 2 opened at −24%).
- **Single names add event risk.** An earnings gap can blow through the −25% stop overnight;
  the stop limits the damage, it does not prevent it. Position size (≤20%) is the real bound:
  worst case ≈ a −25%+ gap on a 20% position ≈ −5%+ of equity per name. **v09's earnings-blackout
  guard reduces this by not *opening* within `earnings_blackout_days` of earnings — but it is
  entry-timing only: a position *held through* an earnings date still carries full gap risk, and
  the guard fails open if the earnings date can't be fetched.**
- **Most backtest differences are within noise.** Over ~19 years the standard error of a Sharpe
  ratio is ≈0.25; differences between close variants of ±0.05 are not evidence on their own. That's
  why §12 requires consistency across four separate 5-year blocks, not just a better full-period or
  OOS number.
- **Concentration comes mostly from winners that are never trimmed.** Caps and rank weights apply
  only at purchase; winners drift up, so the average top position is ~28-31% of equity whatever the
  slots, weights or cap (§12). A trim/rebalance-to-cap rule is the lever — untested so far.
- **The reflection loop can ratchet.** The fallback rule widens `exit_rank_n` whenever return is
  below target, which can repeat. The §8c gates (backtest + reason + drift report) are the defense.
- yfinance is free but occasionally drops bars; the adapter validates (existing `SchemaError`
  pattern).

## 12. Research log — slots, weights, 1-month signal *(Sept 2026, net, 20y)*
**Signal overlap:** the average cross-sectional rank correlation between the 3-month and 6-month
signals is **+0.63** (top-4 lists share 62% of names) — real overlap, but not redundant. The 1-month
return is much less correlated (**+0.49** vs 3m, **+0.35** vs 6m), so it *would* add new information.

**Consistency test** — Sharpe per 5-year block, net:

| config | full | 2007-11 | 2012-16 | 2017-21 | 2022-26 | blocks better | turnover |
|---|---|---|---|---|---|---|---|
| current (4 slots, 3m+6m) | 0.83 | 0.07 | 1.56 | 1.19 | 0.39 | — | 12.5× |
| **6 slots** | **0.87** | 0.17 | 1.77 | 1.13 | 0.41 | **3/4** | **8.8×** |
| 8 slots | 0.86 | 0.16 | 1.69 | 1.16 | 0.43 | 3/4 | 6.8× |
| current + 1m half-reversal | 0.82 | −0.06 | 1.48 | 1.14 | 0.55 | 1/4 | 12.2× |
| 6 slots, flatter weights (0.5) | 0.85 | 0.08 | 1.74 | 1.13 | 0.36 | 2/4 | 10.5× |

- **More slots — adopted as v11 (4 → 6, dropout 7 → 9).** Better in 3 of 4
  blocks, ~+1 pt CAGR, 30% less turnover; ~1 pt deeper max drawdown. Only ~4.7 names qualify on
  average, so the eligibility gate caps the diversification gain (effective holdings 3.2 → 3.6).
- **Flatter weights — not adopted.** Worse Sharpe and deeper crash losses at every slot count. With
  rank weights + the 35% cap, when few names qualify the #1 name hits the cap and the overflow sits
  in cash — a hidden cushion. Flattening mostly converts that cash into exposure (27% → 9-19% cash).
- **1-month signal — not adopted (capability kept: `momentum_weights`).** Reversal beats
  same-direction as hypothesised (same-direction was the worst variant), but no variant beats the
  current blend consistently: the half-reversal's whole edge is 2022-26 (1 of 4 blocks) — recency.
- **Lower position cap** (35/30/25/20% at 6 slots): noisy, non-monotonic — no robust change.
- **Trailing stop — not adopted** (tested on v12; backtester option `trailing_stop: true`, default
  off). Stop = the higher of X% below entry and X% below the highest close since entry. Trailing
  15/25: Sharpe 0.88 → 0.88, OOS 0.59 → 0.59, max DD −22.7% → −23.3%, stop-outs **3 → 27** over
  20y, slightly worse in the GFC (−21.5% vs −19.9%) and 2022 (−9.8% vs −9.1%); block "wins" were
  +0.01 = noise. The rank + trend exits already act as a trailing exit, so a price-based trailing
  stop mostly duplicates them with extra whipsaw (sell on a pullback, re-buy at the next rebalance).
  It also leaves concentration unchanged (top weight 31%).
- **Trim-to-cap — not adopted; owner decision 2026-09-26: do not implement** (don't re-propose
  it; the trim-and-redeploy variant was also declined). Tested on v12; backtester option `trim_to_cap: true` with
  `trim_weight_pct` / `trim_band_pct`, default off). At each rebalance a winner above its cap +
  band is sold back to the cap; proceeds go to cash. It *does* cut concentration (top weight
  31% → 22-25%, effective holdings 3.6 → ~4.0) and max DD by 2-3 pts, but costs **3-4 pts of CAGR/yr**
  (15.4% → 11.3-12.5%) and lowers full-period Sharpe in every variant (0.88 → 0.81-0.86); at most
  2 of 4 blocks improve and 2017-21 is always worse. It cuts the momentum leaders and parks the
  proceeds in cash (27% → 32-36%). Mildest version (>40% → 35%): Sharpe 0.83, OOS 0.61, CAGR 12.4%,
  max DD −20.1%. **Conclusion for observation 1: concentration in the leaders is how momentum earns
  its return — flatter weights, lower caps, trailing stops and trims all gave up more return than
  they removed in risk.** Untested variant: trim *and redeploy* into under-weight holdings.

## 11. Build notes (historical — implemented)
A meaningfully different engine than the original single-asset RSI:
- **yfinance daily adapter** (alongside the ccxt crypto one).
- **Universe-aware** evaluation: fetch all 13 ETFs + `SPY`, compute momentum + SMA, rank, apply
  filters.
- **Multi-position** propose/exit driven by slot logic; conviction-by-rank sizing; 35% cap.
- **Weekly rebalance** scheduling (act on Friday close; daily stop checks in between).
- Config schema per §9; `paper_broker` supports multiple open positions.
