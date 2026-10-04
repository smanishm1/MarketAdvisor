"""Canonical filesystem paths. All writes stay under the project root."""
from __future__ import annotations

import os
from pathlib import Path

# project root = parent of the hermes_trading package
ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
PRESETS_DIR = CONFIG_DIR / "presets"
STATE_DIR = ROOT / "state"
HISTORY_DIR = STATE_DIR / "history"

GOAL_FILE = CONFIG_DIR / "goal.yaml"
STRATEGY_FILE = CONFIG_DIR / "strategy.yaml"
OPTIONS_FILE = CONFIG_DIR / "options.yaml"          # the options stream (the wheel)
DB_FILE = STATE_DIR / "trading.db"
HYPOTHESES_FILE = STATE_DIR / "hypotheses.jsonl"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"      # append-only audit log of human verdicts
BASELINE_FILE = CONFIG_DIR / "baseline.yaml"        # the ORIGINAL design, for drift reports
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
ENV_FILE = ROOT / ".env"

# Your live config files are personal (tuned by your approvals) and git-ignored; the repo
# ships `<name>.example.yaml` templates. A missing live file is created from its template.
_TEMPLATED = (GOAL_FILE, STRATEGY_FILE, OPTIONS_FILE, BASELINE_FILE)


def ensure_config() -> None:
    """Create any missing live config file from its .example.yaml template (never overwrites)."""
    for live in _TEMPLATED:
        template = live.with_name(live.stem + ".example.yaml")
        if not live.exists() and template.exists():
            live.write_bytes(template.read_bytes())


ensure_config()


def ensure_dirs() -> None:
    """Create the state directories if they do not yet exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Load .env into os.environ without overriding anything already set."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # dotenv not installed yet — degrade gracefully
        _manual_load_env()
        return
    load_dotenv(ENV_FILE, override=False)


def _manual_load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
