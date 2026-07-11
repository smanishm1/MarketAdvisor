# hermes-trading

A **local, paper-only** trading agent where **every trade is human-approved**, with a
web dashboard and **Hermes (Claude API) as the reflection brain**. No cloud, no VM, no
real money.

## What it is

- **Worker** (`hermes_trading`) — pulls market data, evaluates the strategy, and *proposes*
  trades. It never auto-fills: an entry signal becomes a card in the dashboard that you
  Approve or Reject.
- **Dashboard** (`dashboard`) — http://localhost:8000 — the **morning market brief**, live
  equity curve, positions, the **approval queue**, trade log, the **journal (strategy
  memory)**, a **vs S&P 500 panel** (each trade vs the same dollars in SPY over the same
  holding window — SPY only competes while a position is open, so cash periods count for
  neither side), and current strategy/goal.
- **Reflection** (`hermes_trading.reflect`) — every N closed trades, proposes **one**
  strategy-variable change (deterministic fallback, or Hermes). That change is *also*
  queued for your approval before it takes effect.
- **Memory** (`hermes_trading.memory`) — context persists from trade to trade and strategy
  to strategy: every buy proposal records *why* (momentum rank/score, how the last
  round-trip in that symbol ended) and carries it onto the filled trade; the reflection
  brain sees the full strategy lineage (past changes + your approve/reject verdicts +
  per-version performance) so it doesn't re-propose rejected ideas.
- **Morning brief** (`hermes_trading.brief`) — auto-generated once a day while the worker
  runs (no API cost): market trend, rankings, holdings health, what's next in line, active
  brakes, and what changed since yesterday. Regenerate anytime with the dashboard's
  **Brief now** button.

State lives in `state/trading.db` (SQLite, WAL). Config is `config/goal.yaml` and
`config/strategy.yaml`.

## Setup (Windows, Python 3.10+)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

## Run (two terminals)

```bash
# terminal 1 — the worker
.venv/Scripts/python -m hermes_trading.run

# terminal 2 — the dashboard
.venv/Scripts/python -m uvicorn dashboard.app:app --port 8000
```

Open http://localhost:8000. When the strategy signals, a pending trade appears — click
**Approve** to open a paper position (or **Reject**). Approved buys fill at the **live price
at approval time**, so a fresh position opens at ~0 unrealised (not a stale gap).

## Discord approvals (optional)

Get pinged in Discord when a trade or strategy change needs approval, and **Approve /
Reject right from the message** with buttons — no need to be at the dashboard.

```bash
.venv/Scripts/python -m pip install -e ".[discord]"
```

One-time Discord setup:
1. Create an application + bot at https://discord.com/developers/applications.
2. Copy the **bot token** into `.env` as `DISCORD_BOT_TOKEN`.
3. Invite the bot to your server (OAuth2 → URL generator → scope **bot**, permission
   **Send Messages**), then open that URL and add it.
4. Enable Developer Mode (Discord Settings → Advanced), right-click the target channel →
   **Copy Channel ID**, and put it in `.env` as `DISCORD_CHANNEL_ID`.

Once `DISCORD_BOT_TOKEN` is set in `.env`, `start.bat` launches the bot automatically
alongside the worker + dashboard. To run just the bot on its own:

```bash
.venv/Scripts/python -m hermes_trading.discord_bot
```

The bot is **symmetric with the dashboard**: a button click only flips the pending row to
approved/rejected — the worker still fills/applies it on its next tick. Acting in the
dashboard updates the Discord message too (and vice-versa), so the two never disagree.
No privileged intents are required. The bot is fully optional — leave the token blank and
nothing changes.

## Reflection

**Automatic (default):** while the worker is running, it fires a reflection after every
`reflection_every` closed trades (set in `config/goal.yaml`) and queues one change for your
approval. Control it in `.env`:
- `HERMES_AUTO_REFLECT=true|false` — turn the auto-trigger on/off
- `HERMES_REFLECT_MODE=hermes|fallback` — Claude brain vs deterministic rule

It never stacks: if a proposal is already awaiting your approval, it waits until you clear it.

**Manual (anytime):**

```bash
# deterministic (no Hermes needed) — proposes one change for your approval
.venv/Scripts/python -m hermes_trading.reflect --fallback

# Hermes brain (after installing Hermes and `hermes model` -> Claude API)
.venv/Scripts/python -m hermes_trading.reflect --hermes
```

Install Hermes (Nous Research) on Windows:

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

Then `hermes model` and pick Anthropic / a Claude model (e.g. `claude-sonnet-4-6`). The
reflection step shells out to `hermes`; override the binary with `HERMES_CMD` if needed.

## Backtest

The active strategy is **SRSR** (relative-strength rotation over 20 symbols — 11 SPDR
sectors, `JEPI`/`JEPQ`, plus 7 mega-cap single stocks under tighter risk rules: 20% cap,
25% stop, max 2 of the 4 slots — see `docs/strategy-srsr.md`). Judge it (and any dial
change) on years of history, not 5 live trades:

```bash
.venv/Scripts/python -m hermes_trading.backtest --years 15
```

Prints total return / CAGR / max drawdown / Sharpe / % cash / # trades vs SPY buy & hold.
*Honest warning: tuning dials until the backtest looks great is curve-fitting — prefer robust
round numbers and out-of-sample checks.*

**Strategies & presets:**
```bash
.venv/Scripts/python -m hermes_trading.presets list             # etf (default) | leaders
.venv/Scripts/python -m hermes_trading.backtest --preset leaders
.venv/Scripts/python -m hermes_trading.presets use leaders --reset   # make it live (wipes book)
```

**Train/test optimizer** (disciplined "strategy discovery" — sweeps dials on a TRAIN window,
validates the winner on a held-out TEST window, and shows the in-sample→OOS shrinkage so you don't
fool yourself):
```bash
.venv/Scripts/python -m hermes_trading.optimize --preset etf --years 15 --test-frac 0.3
```

## Safety

- Paper mode only. `.env` has `HERMES_TRADING_MODE=paper` and
  `HERMES_TRADING_I_ACCEPT_RISK=false`. There is **no live-order code path** in this repo —
  going live would require deliberately adding an execution adapter. Don't, yet.
- Every trade and every strategy change waits for a human click.
- All writes stay under this folder.

## Cost

Local + paper: the only recurring cost is Claude API tokens during `reflect --hermes`
(a few dollars/month). Market data is free via ccxt public endpoints. The worker and
dashboard make zero API calls.
