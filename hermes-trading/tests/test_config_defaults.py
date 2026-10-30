"""Offline checks for PR4: goal defaults match the example file, no leftover crypto numbers.

Run:  .venv\\Scripts\\python tests\\test_config_defaults.py
"""
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_trading import config, score  # noqa: E402

FAILS = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load_with(text):
    p = Path(tempfile.mkdtemp()) / "goal.yaml"
    p.write_text(text, encoding="utf-8")
    config.GOAL_FILE = p
    return config.load_goal()


print("1. a goal.yaml with missing keys falls back to the ETF numbers, not the old crypto ones")
g = load_with("")
ok(g["target_return_30d"] == 0.015 and g["max_drawdown"] == 0.15, "empty file -> 1.5% target, 15% max drawdown")
ok(g["min_sharpe"] == 1.0 and g["failure_below"] == -0.10, "empty file -> Sharpe 1.0, failure below -10%")
ok(g["reflection_every"] == 8 and g["one_variable_only"] is True, "empty file -> reflect every 8 trades, one variable")
ok(g["black_swan"]["early_warn_day_pct"] == -0.05 and g["black_swan"]["crash_day_pct"] == -0.07
   and g["black_swan"]["max_portfolio_drawdown_pct"] == 0.20, "black-swan limits all present")
ok(g["asset"] == "BTC/USDT", "legacy single-asset default is unchanged (only the RSI path uses it)")

print("2. values the user wrote are kept")
g = load_with("target_return_30d: 0.03\nblack_swan:\n  crash_day_pct: -0.06\n")
ok(g["target_return_30d"] == 0.03 and g["black_swan"]["crash_day_pct"] == -0.06, "user values win")
ok(g["max_drawdown"] == 0.15 and g["black_swan"]["max_portfolio_drawdown_pct"] == 0.20, "the rest still default")

print("3. defaults cannot drift away from config/goal.example.yaml")
ex = yaml.safe_load((ROOT / "config" / "goal.example.yaml").read_text(encoding="utf-8"))
for k, v in config.DEFAULT_GOAL.items():
    ok(ex.get(k) == v, f"{k}: code default {v} == example file {ex.get(k)}")
for k, v in config.DEFAULT_BLACK_SWAN.items():
    ok(ex["black_swan"].get(k) == v, f"black_swan.{k}: code default {v} == example file {ex['black_swan'].get(k)}")

print("4. scoring uses the same fallbacks")
trade = {"status": "closed", "pnl": 150.0, "pnl_pct": 0.015, "entry_price": 100.0, "size": 1.0,
         "exit_ts": 2.0, "entry_ts": 1.0}
r = score.score([trade], {}, [10000.0, 10150.0], rf_annual=0.0)
ok(r["composite"] != 0.0 and abs(r["return_score"] - 1.0) < 1e-6, f"a +1.5% of the account scores 1.0 against the 1.5% target (got {r['return_score']})")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
