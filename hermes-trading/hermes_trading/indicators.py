"""Pure-function technical indicators."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI over the most recent `period` bars.

    Returns a value in [0, 100], or None if there is not enough data.
    """
    closes = np.asarray(closes, dtype=float)
    if closes.size < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder's smoothing
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))
