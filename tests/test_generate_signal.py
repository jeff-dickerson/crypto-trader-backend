"""Unit tests for generate_signal: every decision path plus the purity guarantee.

Synthetic candles only; nothing here touches the network or the clock.
"""

from __future__ import annotations

from crypto_trader.strategy.bias import Bias
from crypto_trader.strategy.position import PositionSide, PositionState, PositionStatus
from crypto_trader.strategy.signal import SignalAction, generate_signal
from crypto_trader.strategy.volume_profile import build_volume_profile
from tests.fixtures.strategy_candles import (
    SYMBOL,
    TEST_STRATEGY_CONFIG,
    bar,
    falling_daily,
    flat_daily,
    long_entry_window,
    rising_daily,
    rising_then_crash_4h,
    short_entry_window,
)

C = TEST_STRATEGY_CONFIG


def _series(closes: list[float]) -> list:
    """A simple 4H series with each candle centred on its close."""
    return [
        bar(i, low=c - 1.0, high=c + 1.0, close=c, volume=100.0)
        for i, c in enumerate(closes)
    ]


def _open_long(**overrides) -> PositionState:
    base = dict(
        status=PositionStatus.OPEN,
        side=PositionSide.LONG,
        entry_price=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        extreme_price=100.0,
        filled_fraction=1.0,
    )
    base.update(overrides)
    return PositionState(**base)


# --- Entries ----------------------------------------------------------------------


def test_enters_long_at_the_boundary_not_the_centre_line():
    window = long_entry_window()
    profile = build_volume_profile(window, C)
    signal = generate_signal(window, rising_daily(), PositionState.flat(), C)

    assert signal.action is SignalAction.ENTER_LONG
    assert signal.symbol == SYMBOL
    assert signal.bias is Bias.LONG
    # The entry is the value-area LOW edge, and explicitly NOT the POC centre.
    assert signal.entry_price == profile.val
    assert signal.entry_price != profile.poc_price
    assert signal.zone_boundary == profile.val


def test_long_stop_below_entry_and_take_profit_above():
    window = long_entry_window()
    signal = generate_signal(window, rising_daily(), PositionState.flat(), C)
    assert signal.stop_price < signal.entry_price < signal.take_profit_price


def test_enters_short_at_the_value_area_high():
    window = short_entry_window()
    profile = build_volume_profile(window, C)
    signal = generate_signal(window, falling_daily(), PositionState.flat(), C)

    assert signal.action is SignalAction.ENTER_SHORT
    assert signal.bias is Bias.SHORT
    assert signal.entry_price == profile.vah
    assert signal.take_profit_price < signal.entry_price < signal.stop_price


def test_neutral_bias_produces_no_entry():
    signal = generate_signal(long_entry_window(), flat_daily(), PositionState.flat(), C)
    assert signal.action is SignalAction.NO_SIGNAL
    assert "neutral" in signal.reason


def test_insufficient_history_produces_no_entry():
    short_window = long_entry_window()[: C.min_profile_candles - 1]
    signal = generate_signal(short_window, rising_daily(), PositionState.flat(), C)
    assert signal.action is SignalAction.NO_SIGNAL
    assert "insufficient" in signal.reason


# --- Overtrading governor ----------------------------------------------------------


def test_second_touch_of_an_already_traded_zone_is_rejected():
    window = long_entry_window()
    profile = build_volume_profile(window, C)

    # First touch of a fresh zone fires an entry.
    first = generate_signal(window, rising_daily(), PositionState.flat(), C)
    assert first.action is SignalAction.ENTER_LONG

    # The same zone, now recorded as already traded, is rejected: one zone, one trade.
    already_traded = PositionState.flat(traded_zones=(profile.val,))
    second = generate_signal(window, rising_daily(), already_traded, C)
    assert second.action is SignalAction.NO_SIGNAL
    assert "already traded" in second.reason


# --- Position management: momentum-shift exit and trailing stop --------------------


def test_momentum_shift_exit_fires_when_the_ma_ensemble_flips():
    signal = generate_signal(rising_then_crash_4h(), [], _open_long(extreme_price=106.0), C)
    assert signal.action is SignalAction.EXIT_MOMENTUM_SHIFT


def test_partial_fill_is_managed_like_an_open_position():
    # A partially filled position still has exposure, so management rules apply.
    partial = _open_long(status=PositionStatus.PARTIAL, filled_fraction=0.5)
    signal = generate_signal(rising_then_crash_4h(), [], partial, C)
    assert signal.action is SignalAction.EXIT_MOMENTUM_SHIFT


def test_trailing_stop_activates_and_advances_the_stop():
    # Rising 4H series (no momentum shift); the last candle prints a new high that pushes
    # the favourable excursion past the activation threshold.
    series = _series([100.0, 102.0, 104.0, 106.0, 108.0])
    series.append(bar(len(series), low=110.0, high=115.0, close=114.0, volume=100.0))

    signal = generate_signal(series, [], _open_long(), C)
    assert signal.action is SignalAction.UPDATE_TRAILING_STOP
    # R = 10, extreme = 115, distance = 1R: new stop = 115 - 10 = 105, above the old 90.
    assert signal.stop_price == 105.0


def test_trailing_stop_does_not_activate_below_the_profit_threshold():
    series = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    series.append(bar(len(series), low=104.0, high=105.0, close=104.5, volume=100.0))
    # Best excursion is only +5 vs a 10-unit R: below the 1R activation, so no advance.
    signal = generate_signal(series, [], _open_long(), C)
    assert signal.action is SignalAction.HOLD


def test_pending_entry_holds_awaiting_fill():
    pending = PositionState(status=PositionStatus.PENDING, side=PositionSide.LONG)
    signal = generate_signal(long_entry_window(), rising_daily(), pending, C)
    assert signal.action is SignalAction.HOLD
    assert "awaiting fill" in signal.reason


# --- Purity ------------------------------------------------------------------------


def test_identical_inputs_produce_identical_output():
    window = long_entry_window()
    daily = rising_daily()
    state = PositionState.flat()
    snapshot = list(window)

    first = generate_signal(window, daily, state, C)
    second = generate_signal(window, daily, state, C)

    assert first == second
    # The inputs are not mutated by the call.
    assert window == snapshot


def test_purity_holds_for_management_decisions_too():
    series = rising_then_crash_4h()
    state = _open_long(extreme_price=106.0)
    assert generate_signal(series, [], state, C) == generate_signal(series, [], state, C)
