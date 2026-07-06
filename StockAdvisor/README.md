# StockAdvisor

A local, paper-only, human-approved algorithmic trading assistant. Its #1 job
is capital preservation, not cleverness.

- **Paper trading only.** No live-order code path exists anywhere in this
  repo. See `config.py: startup_safety_check()` and `.env.example`.
- **Every trade is human-approved.** The worker proposes; you click Approve
  or Reject on the dashboard. Only protective exits (stops, circuit
  breakers) happen automatically.
- **Runs entirely on your PC.** No cloud, no accounts, free Yahoo Finance
  data via `yfinance`.
- **Strategy:** weekly sector relative-strength rotation across the 11 SPDR
  sector ETFs, with a trend filter, conviction-weighted sizing, and hard
  risk controls (per-position stop-loss, portfolio-wide circuit breakers).
- **Self-tuning, human-gated:** every 5 closed trades, it may propose one
  small, backtested tweak to one of four dials -- never a redefinition of
  the strategy itself.

See `DEPLOY.md` for the setup checklist. Quick start:

1. Double-click `setup.bat` (one time).
2. Double-click `start.bat` (every time you want to run it).
3. Your browser opens to http://127.0.0.1:8000.

## Project layout

| File | Purpose |
|---|---|
| `config.py` | Locked strategy structure, tunable-dial defaults, env settings, safety gate |
| `db.py` | SQLite (WAL) persistence -- the only shared state between worker and dashboard |
| `data.py` | All Yahoo Finance access (free, no key) |
| `strategy.py` | Pure ranking / sizing / exit logic, used by both the worker and the backtester |
| `risk.py` | Circuit breakers, stops, black-swan / early brake |
| `backtester.py` | Historical simulation + current-vs-proposed + out-of-sample comparison |
| `reflection.py` | Self-tuning loop (rule-based fallback + optional LLM brain) |
| `brief.py` | Morning brief generator |
| `worker.py` | The long-running loop -- the only process that ever fills a trade |
| `dashboard/app.py` | FastAPI dashboard -- the only process that flips approval rows |
| `setup.bat` / `start.bat` | Windows one-click setup and launch |

## Design principle worth restating

The dashboard can only ever change a `pending_approvals` row from PENDING to
APPROVED or REJECTED. It never touches positions, cash, trades, or dials
directly. The worker is the only writer that turns an approval into a real
(paper) action. That separation is what makes "every trade is human
approved, and only the worker executes" true in code, not just in spirit.
