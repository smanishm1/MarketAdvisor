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
| slots | up to 4 | **max 1 of the 4** (`max_stock_positions`; v07, was 2) — the book can never become single-name-heavy |

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
- Fill up to **4 slots** (v04; was 3) with the top-ranked names that pass both filters
  (at most 1 of them a single stock, §1a; v07, was 2).
- **Any slot that can't be filled stays in CASH.** Cash is the primary downside defense:

  | Names qualifying | Invested | Cash |
  |---|---|---|
  | 4 | ~100% | ~0% |
  | 3 | ~75% | ~25% |
  | 2 | ~50% | ~50% |
  | 1 | ~25% | ~75% |
  | 0 (bear market) | 0% | 100% |

- Max **4 open positions**, one per symbol.
- **Approved buys fill at the live price at approval time** (not the stale proposal-time
  price), so a freshly-opened position starts at ~0 unrealised rather than showing a gap that
  is really just the daily-close-vs-intraday series mismatch.

## 5. Sizing — CONVICTION BY RANK *(v03; was equal-weight, was risk-unit)*
- `sizing: rank_weight`, `rank_power: 1` — each held name's target weight ∝ `(N − rank)`
  (best name first), normalized to sum to 1. With 3 names that's ~50 / 33 / 17% before the
  cap; up to 4 slots since v04, so 4 names would be ~40 / 30 / 20 / 10%.
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
- **Catastrophe stop: 30% below entry** (25% for single stocks, §1a; ETF stop widened 15→30
  in v06), checked daily. *(originally an 8% hard stop, which fought the strategy by knocking
  you out of valid uptrends on normal pullbacks — trend + rank are the real exits.)*

## 7. Cadence
**Rebalance weekly** (Friday close): propose new buys, flag exits. Between rebalances only the
catastrophe stop is monitored daily. Weekly (not daily) deliberately — sector momentum is slow;
daily adds noise.

## 8. Governance & approvals (kept from Argus Phase 3)
Long-only · ETF core with a capped single-stock sleeve (§1a) · max 4 positions · 35%/20%
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
  stops), what's next in line, active brakes, and the last 24h (opened/closed trades,
  pending approvals, last reflection). Stored per-date in the `briefs` table.
- **Shadow-SPY comparison (fairness-scoped benchmark):** every fill snapshots SPY
  (`trades.spy_entry`) and every close snapshots it again (`spy_exit`), so each trade is
  compared to *the same dollars in SPY over its own holding window* — SPY only competes
  **while a position is open**; cash periods count for neither side. Open positions mark
  their shadow live. Shown in the dashboard's **vs S&P 500** panel (aggregate alpha, hit
  rate, per-trade rows). This isolates selection skill and deliberately excludes cash
  drag — the backtester's SPY buy-&-hold column remains the whole-strategy benchmark.
  Snapshots are price-only (no dividends): understates SPY ≈0.1%/month held.
  (`hermes_trading.spy_compare`; legacy trades backfilled from daily closes.)

## 9. App config (`strategy.yaml`)
```yaml
version: "07"
type: relative_strength_rotation
universe: [XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLU, XLB, XLRE, XLC, JEPI, JEPQ,
           NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO]
stocks: [NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO]   # single-stock sleeve (v05, §1a)
benchmark: SPY
momentum_lookbacks_days: [63, 126]   # 3mo + 6mo blend
trend_sma_days: 250                   # OOS-validated (was 200)
hold_top_n: 4                         # number of slots (v04; was 3)
exit_rank_n: 6                        # hysteresis (OOS-validated, was 4)
rebalance: weekly                     # Fridays at close
sizing: rank_weight                   # conviction-by-rank (OOS-validated, was equal_weight)
rank_power: 1                         # linear decay; weight proportional to (N - rank)
position_notional_cap_pct: 35
catastrophe_stop_pct: 30              # v06: widened 15->30 (trend + rank are the real exits)
stock_notional_cap_pct: 20            # v05: tighter per-stock cap
stock_catastrophe_stop_pct: 25        # v05: wider per-stock stop
max_stock_positions: 1                # v07: at most 1 of the 4 slots (was 2; OOS-validated)
max_positions: 4                      # always mirrors hold_top_n (see config.load_strategy)
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
  switch flips — the 30% stop is the faster backstop. **Not crash-proof.**
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
  worst case ≈ a −25%+ gap on a 20% position ≈ −5%+ of equity per name.
- yfinance is free but occasionally drops bars; the adapter validates (existing `SchemaError`
  pattern).

## 11. Build notes (historical — implemented)
A meaningfully different engine than the original single-asset RSI:
- **yfinance daily adapter** (alongside the ccxt crypto one).
- **Universe-aware** evaluation: fetch all 13 ETFs + `SPY`, compute momentum + SMA, rank, apply
  filters.
- **Multi-position** propose/exit driven by slot logic; conviction-by-rank sizing; 35% cap.
- **Weekly rebalance** scheduling (act on Friday close; daily stop checks in between).
- Config schema per §9; `paper_broker` supports multiple open positions.
