# The goal bar, and what the backtests say about it (PR4)

Written for readers who want the plain version first. The technical terms come after.

## 1. What `goal.yaml` is for

`goal.yaml` is the report card the app marks its own trades against. When a trade batch is scored, the app compares the result to three bars, and the AI "reflection" step reads the gaps and suggests changes:

| bar in `goal.example.yaml` | meaning |
|---|---|
| `target_return_30d: 0.015` | about +1.5% every 30 days, roughly 18% a year |
| `max_drawdown: 0.15` | a fall of more than 15% from the highest value counts as a failure |
| `min_sharpe: 1.0` | return per unit of ups and downs ("Sharpe ratio") of at least 1.0 |

## 2. How the strategy compares (backtest, Aug/Oct 2007 to Oct 2026, net of costs)

Run on the starter strategy file and on the ETF preset, with the original backtest timing and the live app's timing (Friday rebalance, daily exits, next-day fills, safety brakes). Same data for all rows.

| | per year | worst fall | Sharpe |
|---|---|---|---|
| Starter (v01), original timing | +7.1% | -28.2% | 0.44 |
| Starter (v01), live-like timing | +8.0% | -27.4% | 0.52 |
| ETF preset (v03), original timing | +7.9% | -21.2% | 0.54 |
| ETF preset (v03), live-like timing | +6.8% | -24.4% | 0.48 |
| SPY, buy and hold | +10.9% to +11.1% | -55% | 0.56 |
| The bar in `goal.example.yaml` | about +18% | -15% | 1.0 |

Plain reading: neither version comes close to the bar, and neither beats SPY on return. They give up about 3 to 4 points a year to avoid roughly half of SPY's worst fall.

## 3. Why this matters

- The self-review step measures every result against a bar the strategy cannot reach on this data. A bar that is always missed tends to push the AI to keep suggesting changes. Changes tuned on past data are how overfitting happens.
- The 15% "failure" line sits below the 20% emergency brake (`black_swan.max_portfolio_drawdown_pct`). The app counts a run as failed before the brake ever acts.
- The backtests also show how noisy the comparison is. Moving the weekly rebalance from Monday to Friday changed the ETF preset by 1.4 points a year, and changed the starter by 0.6 in the other direction. For a once-a-week strategy that is calendar luck, and it is about the size of the gap between v01 and v03. So these backtests cannot say which of the two is better; the "OOS-validated" labels on the ETF preset were judged with the old timing and are not proven by the live-like numbers.

## 4. What PR4 changes (and what it leaves alone)

Changed:
- If a key is missing from `goal.yaml`, the app used to fall back to old crypto-era numbers (5% target, 8% drawdown, Sharpe 1.2, failure at -4%, reflect every 5). It now falls back to the numbers in `goal.example.yaml`. One table of defaults lives in `config.py` (`DEFAULT_GOAL`, `DEFAULT_BLACK_SWAN`) and `score.py` uses the same one. A test checks the code defaults against the example file so they cannot drift apart again.
- `config/presets/etf.yaml`: the comment said it "mirrors the default". It does not (v03 vs the v01 starter). Corrected, with a note on the point above.
- `config/presets/leaders.yaml`: labelled "illustrative only, not evidence". The 26-stock list was picked today knowing these stocks did well, so any backtest of it over past years is flattered by hindsight, and it does not fit an ETF-only IRA.

Not changed:
- The bar itself, and every strategy setting. Those are the owner's decisions.
- `asset` still defaults to "BTC/USDT": only the legacy single-asset RSI path reads it.
- A strategy file with no `type:` line still falls back to the old RSI crypto engine. This is worth knowing but changing it could break an old file, so it is left as is.

## 5. Questions for the owner

1. Is +1.5% per 30 days a target you really expect, or an aspiration? A bar nearer what the data shows (for example 8% to 10% a year, Sharpe 0.5 to 0.6) would make the reflection step's signals meaningful.
2. Should the "failure" drawdown (15%) and the emergency brake (20%) match?
3. Could you share your live `strategy.yaml` (it holds no secrets)? The repository only contains the starter and the presets, so nobody else can backtest what is actually running.
