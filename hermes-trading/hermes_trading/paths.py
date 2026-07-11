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
DB_FILE = STATE_DIR / "trading.db"
HYPOTHESES_FILE = STATE_DIR / "hypotheses.jsonl"
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
ENV_FILE = ROOT / ".env"


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
