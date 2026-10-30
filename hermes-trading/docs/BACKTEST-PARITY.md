# Making the backtest behave like the live app

## Why this exists

Every strategy change in this project is approved on backtest numbers, so the backtest has to
test the same thing the live app does. A code review (October 2026) found four ways it did not.
The live app:

| | Live app | Original backtest |
|---|---|---|
| Rebalance day | Friday | first trading day of the week (Monday) |
| Exits | checked every day (setting "fast exits", on by default) | only at the weekly rebalance |
| When a trade happens | signal first, you approve later, filled at the live price | decided and filled at the same closing price |
| Safety brakes | -7% SPY day or 20% drawdown sells everything; -5% day pauses buying | not modelled |

The backtest's own code comment said exits were daily by default. In the code they were weekly,
and nothing in the project ever turned daily exits on for a backtest. So the Sharpe numbers in
`docs/strategy-srsr.md` describe a slightly different strategy from the one that is running.

## What changed

**Nothing about the default.** Run with no new options, the backtest gives exactly the same
numbers as before (checked on 60 old-versus-new comparisons across sizing schemes, stops,
trailing stops, trimming and date windows; `tests/test_backtest_parity.py` keeps four of them
pinned). Old results stay comparable.

New options let you switch each live-app behaviour on:

| Option | Command line | Meaning |
|---|---|---|
| `rebalance_day="friday"` | `--rebalance-day friday` | rebalance on the last trading day of the week |
| `fast_exits=True` | `--daily-exits` | check rotation exits every day |
| `lag=1` | `--lag 1` | trade at the next day's close, one day after the signal |
| `brakes={...}` | `--brakes` | the live crash / drawdown / early-warning brakes, limits from `goal.yaml` |
| all four | `--live-like` | the bundle above |

## How to use it

```
.venv\Scripts\python -m hermes_trading.backtest --compare-modes
```

This runs the same strategy on the same data six times: the original timing, then **each**
live-app difference on its own, then all four together. The table shows how far each one moves
CAGR, worst drawdown and Sharpe, so you can see which difference matters. It takes a few
minutes because it downloads 20 years of prices once and runs six simulations.

For a single run: `--live-like` (or any of the options above) on the normal command.

**How to read the result.** A small gap means the old numbers were fair. A big gap means past
approvals (the changelog's "OOS-validated" changes) should be re-checked under the live-like
timing before trusting them. The report header now says which timing was used.

## What is still different from the live app

- **Daily bars only.** The live app checks stops and the crash brake against prices every 20
  seconds. The backtest sees only the daily close, so brakes and stops react at the close.
- **Decision day.** The live worker rebalances on a Friday using the data it fetched that day,
  which is normally Thursday's close, and you fill later. `--live-like` decides on Friday's close
  and fills a day later. Both leave one day between signal and fill, but on different weekdays.
- **Approval delay.** Live fills can be days after a signal if a proposal sits unapproved
  (proposals never expire; see the code review). The backtest assumes at most one day.
- **Brakes and old orders.** An order decided the day before a crash still fills after the
  brake fires. The live app can do the same with an unapproved proposal.
- **You can skip trades.** Rejecting a proposal is not modelled.

## Known quirk, kept on purpose

On each bar the equity curve is recorded before that bar's own trading costs are deducted (the
costs show up the next day). It is a tiny timing effect. It is left as it was so the default
numbers do not change; fixing it would be a separate, labelled change.

## Suggested next step

Once `--compare-modes` shows the size of the gap, decide whether `--live-like` should become the
default for the approval backtests (`compare()`, `optimize.py`). That is a one-line change, but it
changes every number the dashboard shows, so it is deliberately not part of this PR.

## Tests

```
.venv\Scripts\python tests\test_backtest_parity.py
```

Offline, with fake prices (`tests/synthetic_prices.py`). Checks that the defaults reproduce the
original numbers, Friday versus Monday rebalancing, that `lag=1` buys at the next close (worked
out by hand), that daily exits leave earlier, that a -9% day sells to cash and stays out until
SPY is back above its 200-day average, and that a -5.5% day only pauses buying.
