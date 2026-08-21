"""Unit tests for the daily bias gate: long, short, neutral, and insufficient data."""

from __future__ import annotations

from crypto_trader.strategy.bias import Bias, compute_bias
from tests.fixtures.strategy_candles import (
    TEST_STRATEGY_CONFIG,
    falling_daily,
    flat_daily,
    rising_daily,
)


def test_rising_daily_closes_are_long_bias():
    assert compute_bias(rising_daily(), TEST_STRATEGY_CONFIG) is Bias.LONG


def test_falling_daily_closes_are_short_bias():
    assert compute_bias(falling_daily(), TEST_STRATEGY_CONFIG) is Bias.SHORT


def test_flat_daily_closes_are_neutral_bias():
    assert compute_bias(flat_daily(), TEST_STRATEGY_CONFIG) is Bias.NEUTRAL


def test_insufficient_history_is_neutral_not_a_partial_trend():
    # Fewer candles than the slow SMA period: no trend can be established, so neutral.
    too_few = rising_daily(count=TEST_STRATEGY_CONFIG.bias_slow_period - 1)
    assert compute_bias(too_few, TEST_STRATEGY_CONFIG) is Bias.NEUTRAL


def test_bias_uses_exactly_the_candles_given():
    # A growing window: once enough candles arrive the bias resolves from just them.
    assert compute_bias([], TEST_STRATEGY_CONFIG) is Bias.NEUTRAL
    assert compute_bias(rising_daily(6), TEST_STRATEGY_CONFIG) is Bias.LONG
