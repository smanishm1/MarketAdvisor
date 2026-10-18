# The "too few trades" guard and the demo seeder (PR9)

Plain version first, technical detail after.

## 1. What the guard is for

The app has a "reflection" step. After some trades have closed, it looks at the results and suggests ONE change to the strategy (the AI, or a built-in rule set, proposes; you approve or reject). If only a handful of trades have closed, any pattern is mostly luck, so the app is supposed to say "too few trades to judge" and hold.

## 2. What was wrong

In `reflect.py` the minimum was `MIN_SAMPLE_TRADES = 0`. The check asks "fewer than 0 trades?", which is never true, so the guard never fired. The comment above it, and `seed_demo.py`, both say the minimum is 5.

What that allowed:
- Clicking "Reflect now" with no closed trades at all could still produce a strategy-change proposal, based on nothing (with no trades the score is zero, which the rule set reads as "below target, loosen things").
- The automatic reflection was less exposed, because it only fires after `reflection_every` (8) newly closed trades. So the gap was in the manual button and the command line.
- You still have to approve every proposal, so nothing changed by itself.

## 3. What PR9 changes

- The built-in minimum is now 5, matching the comments and `seed_demo.py`.
- It can be set in `config/goal.yaml` with `min_sample_trades:`. `0` switches the check off, which restores the old behaviour for anyone who set it to 0 on purpose for testing.
- When it holds, the message says how many trades are needed.
- `config/goal.example.yaml` documents the new line.

## 4. Is 5 enough? (a question for the owner)

Not really. This is a once-a-week strategy holding a few sectors, so 5 closed trades can be a month or two of activity. Individual trade results typically swing by several percent either way, which is bigger than the 1.5% target the reflection compares against, so with 5 trades luck can easily look like skill. 5 matches what the code comments already promised, and it is a big step up from 0, but 20 to 30 would be a more honest bar. Change it in `goal.yaml` if you agree.

## 5. `seed_demo.py` is now safer

`seed_demo.py` adds 6 fake losing trades so you can see the reflection work in a demo. It writes to the SAME database the app uses, so until you clear them they count in profit, equity, the score and the drift report. Changes:
- It refuses to run if demo trades are already there (before, running it twice made 12).
- It refuses if the database already has real closed trades or open positions. `--force` overrides, and then you should run `--clear` straight after.
- `--clear` also puts the auto-reflection counter (`last_reflection_count`) back to the real number of closed trades. Before, if a reflection ran while the demo rows were present, removing them left the counter too high and delayed the next automatic reflection by up to 6 trades.
- It prints a clear warning and returns a proper exit code.

## 6. Tests

`tests/test_reflection_guard.py` (offline, throwaway databases): seeding and clearing, each refusal, `--force`, the counter reset, the guard holding at 0 and 4 trades and letting 5 through, the `goal.yaml` override in both directions, and the example file matching the built-in default. It fails if the minimum is put back to 0.
