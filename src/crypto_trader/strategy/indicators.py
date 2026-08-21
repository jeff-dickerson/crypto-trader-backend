"""Small, pure indicator helpers shared by the strategy modules.

Nothing here reads the clock, does I/O, or holds state: every function is a pure
transform of its inputs, so the whole strategy core stays a pure function of
(candles, htf_candles, position_state, config).
"""

from __future__ import annotations

from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last `period` values.

    Returns None when there are fewer than `period` values, so callers can treat
    "not enough history yet" as a distinct, explicit case rather than a silent
    partial average.
    """
    if period <= 0:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period
