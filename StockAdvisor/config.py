"""
config.py -- central configuration for StockAdvisor.

Two kinds of settings live here, and they are kept deliberately separate:

1. LOCKED STRUCTURE constants. These define WHAT the strategy is (universe,
   number of positions, sizing model, rebalance cadence). The reflection
   loop is NEVER allowed to change these -- only a human editing this file
   can. This is intentional: it stops the self-tuning loop from quietly
   turning into a different strategy over time.

2. Environment-derived settings (.env). These are operational / deployment
   settings: paper-mode gates, dashboard host/port, polling cadence,
   reflection mode, DB path.

The four TUNABLE DIALS (trend filter length, exit-rank hysteresis,
catastrophe stop %, position cap %) are NOT defined here as fixed values --
their live values are stored in the database `dials` table, seeded from the
DEFAULT_DIALS below on first run. That is what the reflection loop is
allowed to touch, subject to human approval.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


# ============================================================================
# LOCKED STRATEGY STRUCTURE -- the reflection loop may never modify these.
# Changing these requires a human editing this file directly, which is a
# deliberate speed bump: redefining the strategy is a different act from
# tuning it.
# ============================================================================
UNIVERSE = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
BENCHMARK = "SPY"
TOP_N_HOLD = 3                  # hold the top 3 qualifying sectors
EXIT_RANK_WINDOW_DEFAULT = 6    # sell when a holding drops out of top-6 (tunable, see dials)
MOMENTUM_LOOKBACKS_DAYS = (63, 126)   # ~3mo + ~6mo momentum blend, equally weighted
REBALANCE_WEEKDAY = 4           # Monday=0 ... Friday=4. New BUYS proposed only on this day.
MAX_POSITIONS_CONSIDERED_FOR_SIZING = 3

# Risk / circuit-breaker thresholds (structural -- not reflection-tunable;
# these are safety limits, not performance dials)
BLACK_SWAN_SPY_DAY_DROP = -0.07     # SPY single-day return <= this -> flatten & halt
BLACK_SWAN_DRAWDOWN = -0.20         # portfolio drawdown from peak <= this -> flatten & halt
EARLY_BRAKE_SPY_DAY_DROP = -0.05    # SPY single-day return <= this -> pause new buys only
SPY_RESUME_MA_DAYS = 200            # auto-resume from black-swan halt once SPY > its 200dma

# ============================================================================
# TUNABLE DIALS -- default seed values only. Live values are read from the
# DB `dials` table (see db.py: get_dial / set_dial). The reflection loop may
# propose changes to these four, and only these four, subject to human
# approval via the dashboard.
# ============================================================================
DEFAULT_DIALS = {
    "trend_filter_len": 250.0,        # days, moving-average trend filter
    "exit_rank_hysteresis": float(EXIT_RANK_WINDOW_DEFAULT),  # drop-below-rank exit threshold
    "catastrophe_stop_pct": 0.15,     # hard stop-loss, fraction (0.15 = -15%)
    "position_cap_pct": 0.35,         # max weight per name, fraction of equity
}
TUNABLE_DIAL_NAMES = tuple(DEFAULT_DIALS.keys())

# Reasonable safety bounds a reflection proposal must stay within, regardless
# of what the fallback/LLM logic computes. These prevent a runaway proposal
# (e.g. "set stop to 90%") from ever reaching the approval queue.
DIAL_BOUNDS = {
    "trend_filter_len": (100.0, 300.0),
    "exit_rank_hysteresis": (3.0, 11.0),
    "catastrophe_stop_pct": (0.08, 0.25),
    "position_cap_pct": (0.20, 0.50),
}

# ============================================================================
# Environment-derived operational settings
# ============================================================================
# --- Live-trading safety gate (see .env.example for the full explanation).
# Both flags AND a not-yet-written execution adapter are required for any
# live order to ever be possible. As of this codebase, no such adapter
# exists anywhere in the repo, so live trading is categorically impossible
# no matter how these flags are set.
ALLOW_LIVE_TRADING = _env_bool("ALLOW_LIVE_TRADING", False)
LIVE_TRADING_CONFIRMED = _env_bool("LIVE_TRADING_CONFIRMED_I_UNDERSTAND_THE_RISK", False)
PAPER_MODE = True  # hardcoded; see startup_safety_check() below

PAPER_STARTING_CASH = _env_float("PAPER_STARTING_CASH", 100000.0)

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8000)

DAILY_LOOP_INTERVAL_SEC = _env_int("DAILY_LOOP_INTERVAL_SEC", 1800)
INTRADAY_RISK_POLL_SEC = _env_int("INTRADAY_RISK_POLL_SEC", 20)

REFLECTION_MODE = os.getenv("REFLECTION_MODE", "fallback").strip().lower()
REFLECTION_EVERY_N_TRADES = _env_int("REFLECTION_EVERY_N_TRADES", 5)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

DB_PATH = str(BASE_DIR / os.getenv("DB_PATH", "data/stockadvisor.db"))
LOG_DIR = BASE_DIR / "logs"


def startup_safety_check():
    """
    Called once at worker startup. This is the single choke point that
    guarantees paper-only operation. There is no execution adapter in this
    codebase capable of sending a live order -- this function's job is just
    to make the intent loud and to fail closed if someone tries to bypass it
    by, e.g., importing a live adapter module that does not exist.
    """
    if ALLOW_LIVE_TRADING or LIVE_TRADING_CONFIRMED:
        raise RuntimeError(
            "SAFETY HALT: a live-trading flag is set to true in .env, but no "
            "live execution adapter exists in this codebase. Refusing to "
            "start. This system is PAPER-ONLY by construction. If you see "
            "this message, revert the flags in your .env file."
        )
    return True
