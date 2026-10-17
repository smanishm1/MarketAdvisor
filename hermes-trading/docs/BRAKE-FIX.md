# Live safety fixes (PR3): why, in plain words

Three small problems found in the review. None of them change the strategy's signals.

## 1. The 20% drawdown brake could get stuck

**What the brake is for.** If the portfolio falls 20% from its highest value, the app sells everything to cash and stops buying. Buying resumes once the market (SPY) is back above its 200-day average.

**What went wrong.** The 20% fall was measured from the old highest value, forever. After everything is sold, the portfolio sits in cash and can never climb back near that old high, so it still looks "20% down". The brake had no way to clear, except by the tiny interest on cash nudging the value just over the line. Then the strategy bought again, lost a fraction of a percent, and the brake fired again within days.

**Evidence (backtest, 2007 to 2026, starter strategy file, brakes only):**
- 17 flatten-and-clear cycles, including a 7-year stretch (2011 to 2018) with no clearing at all.
- About 81% of days in cash, +0.5% a year.

**The fix.** When the brake sells everything, the "highest value" is reset to the portfolio's value at that moment. The 20% rule then starts again from there. Risk-off still only clears when SPY is above its 200-day average.
- Live app: `loop.py` stores `peak_reset_ts` and only looks at equity since then.
- Backtest: `backtest.py` resets `peak` on the flatten, so it matches the live app.
- Dashboard "clear risk-off" button: also restarts the clock. Before, pressing it just let the next check switch risk-off straight back on.

**Honest limit.** If a drawdown trigger happens while SPY is already above its 200-day average, the brake can clear the next day. That is how the market-trend rule was always meant to work, but it means the app can buy again quickly after a bad run. If you want a cooling-off period (say 10 days), that is a separate decision.

## 2. Old trade proposals could be approved long after they were made

**What went wrong.** A proposal is a snapshot of one day's signal. If it sat unapproved for days, approving it still bought at the live price, even if the market had since crashed or the cash was gone.

**The fix** (`approval.py`, `loop.py`):
- Proposals older than 72 hours are marked `expired` and the next rebalance proposes fresh ones. 72 hours lets a Friday proposal be approved on Monday. Change it with `pending_trade_max_age_hours` in `goal.yaml`.
- At the moment a buy would fill, it is cancelled (status `expired`, with a log line saying why) if:
  - risk-off is on, or the early-warning brake has paused buys;
  - the price has moved more than 5% since the proposal (`pending_trade_max_drift_pct`);
  - the cash is no longer there.

**Honest limit.** I did not re-run the full ranking at approval time. The checks above are the cheap, important ones. The 72-hour limit covers the rest.

## 3. The "$68M target" on pending trades

Rotation trades have no price target; they exit by rule. The code stores "price times one million" as a placeholder meaning "no target". The pending-trades list printed that placeholder as a real target. It now shows "none (exits by rule)". The open-positions table already hid it.

## Tests
`tests/test_live_safety.py` (offline, throwaway database): drawdown flatten then clear and stay clear, in both the backtest and the live worker; the dashboard clear button; expiry; each approval re-check; a still-valid approval still fills; the display text. The brake test fails if the fix is removed.
