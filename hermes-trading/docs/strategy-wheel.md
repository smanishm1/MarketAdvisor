# Options stream — "the wheel" on healthy free cash flow

**Status:** v01, paper only, started 2026-09-27 with $10,000 in a book separate from the ETF rotation.
**Config:** `config/options.yaml` (live-reloaded) · **Code:** `hermes_trading/wheel.py` (engine),
`wheel_book.py` (ledger), `wheel_screen.py` (FCF screen) · **UI:** dashboard "Options stream"
panel, Discord cards, and a morning-brief section.

> Educational paper stream. The screen and proposals are rule outputs, not personalized
> investment advice.

## 1. Why the wheel for a beginner

The wheel uses only two option trades, and each one is defined-risk and fully funded:

1. **Sell a cash-secured put** on a company you would be happy to own. The full collateral
   (strike × 100) is set aside in cash, so there is no margin and no leverage.
   - If the stock ends **above** the strike at expiry, you keep the premium and repeat.
   - If it ends **below**, you are **assigned**: you buy 100 shares at the strike. Your
     effective cost is the strike minus the premium.
2. **Sell a covered call** on those shares, at or above your cost.
   - If the stock ends above the strike, the shares are **called away** at a gain and the
     cycle restarts.
   - Otherwise you keep the premium and the shares, and sell another call.

The worst case is the same as owning the stock, bought slightly below where it traded when
the put was sold. The upside is capped, because you trade some upside for steady premium
income. That is why the quality of the company matters more than the option mechanics.
Hence the free-cash-flow screen.

**What a beginner should expect:**
- Many small wins.
- Occasional assignment in a falling market.
- Underperformance in sharp rallies, because the calls cap gains.

## 2. The free-cash-flow screen (weekly, S&P 500)

| Filter | Rule | Why |
|---|---|---|
| Sector | exclude Financials and Real Estate | FCF isn't a meaningful measure for banks, insurers or REITs |
| Price | $10–65 | one contract's collateral must fit a slot ($6,500 cap) |
| FCF definition | trailing 12 months, operating cash flow − capex, from the quarterly statements | Yahoo's `freeCashflow` field is *levered* FCF and was badly off (e.g. GPN at 77%) |
| FCF margin | 10–60% of revenue | real cash generation; > 60% is almost always a data error |
| FCF yield | ≥ 4% of market cap | you aren't overpaying for that cash |
| Balance sheet | net debt ≤ 3 × FCF | debt can be repaid from cash flow |
| Consistency | FCF positive every year on record (≥ 3 annual reports) | not a one-off good year |
| Trend | price ≥ 0.95 × 200-day average and 6-month return > −20% | no falling knives; puts on a stock in freefall get assigned |

Survivors are ranked by FCF-yield percentile + FCF-margin percentile + ½ × (low-debt
percentile). Each candidate carries a plain-English `why`.

First run (2026-09-27), per stage:

| Stage | Companies |
|---|---|
| S&P 500 | 503 |
| ex-financials/REITs | 397 |
| Affordable | 132 |
| Healthy FCF | 22 |
| Not in a downtrend | 12 |

## 3. Position rules

- **Slots:** at most 2 positions (a short put or a stock holding each uses one), with at most
  $6,500 of collateral each.
  - Always keep ≥ 10% of equity unreserved.
  - One slot per company, and never two in the same sector.
- **Contract choice:**
  - Expiry nearest 30 days, within 14–50 days, and **before the next earnings report**.
  - Strike nearest **0.25 delta**, within 0.15–0.35. The Black-Scholes delta comes from the
    option's implied volatility plus the T-bill rate, and implies about a 75% model chance
    of expiring worthless.
  - Open interest ≥ 50 and bid-ask spread ≤ 30% of mid.
  - Live quotes only: proposals are made on weekdays from 10:00 ET, never from stale weekend
    quotes.
- **Covered calls:** only at or above the assigned strike (`never_sell_calls_below_basis`).

## 4. Governance and automation

| Event | Who |
|---|---|
| New put or call sale | **Proposed** once a day after 10:00 ET. **Needs your approval** (dashboard or Discord). Unanswered proposals expire after 3 days. Approved sales fill at the next live quote. |
| Take-profit | Automatic buy-back once 50% of the premium is captured (marks every 5 min while open) |
| Expiry | Automatic, from the expiry-day close: expired worthless / assigned / called away |
| Screen refresh | Automatic weekly, or **Re-run screen** on the dashboard |

## 5. Paper-fill realism and accounting

- **Sell fills:** bid + 25% of the spread.
- **Buy fills:** ask − 25% of the spread.
- **Commission:** $0.65 per contract on each side.
- **Cash ledger:** premium credited; put collateral **reserved** but kept in cash, where it
  earns the 13-week T-bill; assignment debits strike × 100; call-away credits strike × 100.
- **Equity:**
  - equity = cash + shares at market − cost to buy back open short options (at the mid);
  - free cash = cash − reserved collateral.
- Verified by an offline lifecycle test: propose → fill → take-profit → assignment →
  covered call → called away, with the ledger reconciled to the cent.

## 6. Caveats

- Option quotes come from Yahoo (delayed, sometimes thin).
  - Paper fills are an approximation; real fills on small names can be worse.
- Early assignment (American options) isn't modelled. It is rare for out-of-the-money puts
  and matters mainly before an ex-dividend date.
- The delta-implied probability is a model estimate under risk-neutral assumptions, not a
  forecast.
- Two positions is a small sample: results will be lumpy, and one assignment in a sell-off
  dominates a quarter.

## Changelog

- **v01 (2026-09-27):** stream created.
  - $10K; wheel; 2 slots + 10% cash; 0.25 delta; 14–50 DTE before earnings; 50% take-profit.
  - FCF screen as above.
