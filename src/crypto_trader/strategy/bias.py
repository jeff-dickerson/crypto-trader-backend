"""Daily bias gate: long, short, or neutral from daily candles.

Bias is a fast-vs-slow simple-moving-average trend filter on daily closes, with a
neutral band so a razor-thin cross does not declare a directional bias. Only trades
that agree with a directional bias are allowed; a neutral bias means no new entry.

Like the volume profile, this computes from exactly the daily candles it is handed,
however many are present, so a growing backtest window is handled honestly. When
there are too few candles to compute the slow SMA, the bias is neutral (we cannot
establish a trend, so we do not trade).
"""

from __future__ import annotations

from enum import Enum

from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.config import StrategyConfig
from crypto_trader.strategy.indicators import sma


class Bias(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


def compute_bias(daily_candles: list[Candle], config: StrategyConfig) -> Bias:
    """Long/short/neutral bias from daily closes.

    LONG when the fast SMA is above the slow SMA by more than the neutral band and
    the latest close is above the slow SMA. SHORT is the mirror. Everything else,
    including insufficient history, is NEUTRAL.
    """
    closes = [c.close for c in daily_candles]
    fast = sma(closes, config.bias_fast_period)
    slow = sma(closes, config.bias_slow_period)
    if fast is None or slow is None:
        return Bias.NEUTRAL

    band = abs(slow) * config.bias_neutral_band
    last_close = closes[-1]

    if fast > slow + band and last_close > slow:
        return Bias.LONG
    if fast < slow - band and last_close < slow:
        return Bias.SHORT
    return Bias.NEUTRAL
