"""Worker entrypoint:  python -m hermes_trading.run [--asset BTC/USDT]"""
from __future__ import annotations

import argparse
import asyncio

from .loop import run_loop


def main() -> None:
    parser = argparse.ArgumentParser(description="hermes-trading paper worker")
    parser.add_argument(
        "--asset", default=None, help="override the asset from goal.yaml, e.g. ETH/USDT"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_loop(args.asset))
    except KeyboardInterrupt:
        print("\nworker stopped.")


if __name__ == "__main__":
    main()
