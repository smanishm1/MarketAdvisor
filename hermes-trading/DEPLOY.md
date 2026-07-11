# Deploying hermes-trading to another Windows PC

A turnkey, **paper-only** install for a non-technical user. Two double-click
scripts; no command line needed after the copy.

## 1. On the target PC: install Python (one time)

1. Download Python 3.10+ from <https://www.python.org/downloads/windows/>.
2. Run the installer and **tick "Add python.exe to PATH"** before clicking Install.

## 2. Copy the app over

Copy the project folder to the target PC (e.g. to `C:\hermes-trading`). A USB
stick, OneDrive, or a zip all work.

**Do NOT copy these** — they are machine-specific or personal and must be fresh:

| Skip | Why |
|------|-----|
| `.venv\` | Has absolute paths from your PC; gets rebuilt by setup.bat |
| `state\` | Your paper book / trade history — they start clean at $10k |
| `.env` | Your settings/keys; setup.bat creates a clean one from `.env.example` |
| `.git\`, `__pycache__\`, `*.egg-info\` | Not needed |

Easiest: zip the folder, delete those four from the zip, send it.

## 3. Set it up (one time, on the target PC)

Double-click **`setup.bat`**. It checks Python, builds the environment, installs
everything, and creates `.env`. Takes a few minutes the first time.

## 4. Run it

Double-click **`start.bat`**. Two small windows open (the worker and the
dashboard) and the browser opens to <http://localhost:8000>. Keep those two
windows open while using the app; close them to stop it.

That's the whole loop: when the strategy proposes a buy, it shows up in the
dashboard's approval queue — click **Approve** or **Reject**. Exits are
automatic.

## Notes

- **Cost:** $0. Market data is free (yfinance), and reflection is set to the
  free deterministic `fallback` (see `.env`). The Claude/`hermes` brain is
  optional and off by default — turn it on only if you install the Hermes CLI
  and a Claude API key on that PC (it costs API money per run).
- **Safety:** paper mode only. There is no live-order code path in this repo;
  no real money can move. Every trade waits for a human click.
- **Keep it running:** the worker only updates while `start.bat`'s windows are
  open. To run it automatically at logon, create a Task Scheduler task that runs
  `start.bat` "At log on" — optional, and only if they want it always-on.
- **Updating later:** re-copy the `hermes_trading\`, `dashboard\`, `config\`,
  and `docs\` folders over the top (leave their `state\` and `.env` alone), then
  just run `start.bat` again. No re-setup needed unless dependencies changed.
