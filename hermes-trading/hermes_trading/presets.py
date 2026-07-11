"""List and switch strategy presets.

    python -m hermes_trading.presets list
    python -m hermes_trading.presets use leaders --reset   # make leaders the live strategy

Switching changes the whole universe, so positions from the previous strategy don't
carry over — use --reset to wipe the live book for a clean start, then restart the worker.
"""
from __future__ import annotations

import argparse
import shutil

from rich.console import Console

from .paths import DB_FILE, HISTORY_DIR, HYPOTHESES_FILE, PRESETS_DIR, STATE_DIR, STRATEGY_FILE

console = Console()


def list_presets() -> None:
    console.print("[bold]Available presets[/] (config/presets/):")
    for p in sorted(PRESETS_DIR.glob("*.yaml")):
        console.print(f"  - {p.stem}")
    console.print("  backtest one:  python -m hermes_trading.backtest --preset <name>")


def use(name: str, reset: bool) -> None:
    src = PRESETS_DIR / f"{name}.yaml"
    if not src.exists():
        console.print(f"[red]no preset '{name}' in {PRESETS_DIR}[/]")
        return
    shutil.copyfile(src, STRATEGY_FILE)
    console.print(f"[green]active strategy -> {name}[/]  (wrote config/strategy.yaml)")

    if reset:
        for f in (
            DB_FILE,
            DB_FILE.parent / (DB_FILE.name + "-wal"),
            DB_FILE.parent / (DB_FILE.name + "-shm"),
            HYPOTHESES_FILE,
            STATE_DIR / "heartbeat.json",
        ):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(HISTORY_DIR, ignore_errors=True)
        console.print("[yellow]live book wiped — fresh start.[/]")
    else:
        console.print(
            "[yellow]note:[/] existing positions/state were NOT cleared — if you had open "
            "positions from another strategy, re-run with --reset for a clean book."
        )
    console.print("[dim]restart the worker to apply.[/]")


def main() -> None:
    ap = argparse.ArgumentParser(description="strategy presets")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list available presets")
    u = sub.add_parser("use", help="make a preset the active strategy")
    u.add_argument("name")
    u.add_argument("--reset", action="store_true", help="wipe the live book for a clean start")
    args = ap.parse_args()
    if args.cmd == "use":
        use(args.name, args.reset)
    else:
        list_presets()


if __name__ == "__main__":
    main()
