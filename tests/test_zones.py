"""Unit tests for zone setup detection and entry geometry.

Covers the separation-then-return setup (fires / does not fire), the one-zone-one-trade
governor, and the exact stop (adjacent LVN beyond the zone) and take-profit (before the
opposing HVN) derivation, including the data-edge fallbacks.
"""

from __future__ import annotations

from crypto_trader.strategy.bias import Bias
from crypto_trader.strategy.volume_profile import build_volume_profile
from crypto_trader.strategy.zones import (
    _long_stop,
    _long_take_profit,
    detect_entry_setup,
    is_zone_already_traded,
)
from tests.fixtures.strategy_candles import (
    TEST_STRATEGY_CONFIG,
    long_entry_window,
    long_no_separation_window,
    profile_bars_from_bin_volumes,
    short_entry_window,
)

C = TEST_STRATEGY_CONFIG


def _profile(window):
    profile = build_volume_profile(window, C)
    assert profile is not None
    return profile


def test_clean_separation_then_return_fires_long_at_the_value_area_low():
    window = long_entry_window()
    profile = _profile(window)
    setup = detect_entry_setup(profile, window, Bias.LONG, C)
    assert setup is not None
    assert setup.side is Bias.LONG
    # Entry is the value-area LOW edge (the boundary), never the POC centre line.
    assert setup.entry_price == profile.val
    assert setup.entry_price != profile.poc_price
    assert setup.stop_price < setup.entry_price < setup.take_profit_price


def test_clean_separation_then_return_fires_short_at_the_value_area_high():
    window = short_entry_window()
    profile = _profile(window)
    setup = detect_entry_setup(profile, window, Bias.SHORT, C)
    assert setup is not None
    assert setup.side is Bias.SHORT
    assert setup.entry_price == profile.vah
    assert setup.entry_price != profile.poc_price
    assert setup.take_profit_price < setup.entry_price < setup.stop_price


def test_window_that_never_separates_yields_no_setup():
    window = long_no_separation_window()
    profile = _profile(window)
    assert detect_entry_setup(profile, window, Bias.LONG, C) is None


def test_neutral_bias_never_produces_a_setup():
    window = long_entry_window()
    profile = _profile(window)
    assert detect_entry_setup(profile, window, Bias.NEUTRAL, C) is None


def test_one_zone_one_trade_governor_matches_within_tolerance():
    # Boundaries within zone_match_pct (0.5%) are the same zone; further apart are not.
    assert is_zone_already_traded(100.0, (100.3,), C) is True
    assert is_zone_already_traded(100.0, (101.0,), C) is False
    assert is_zone_already_traded(100.0, (), C) is False


def test_long_stop_sits_in_the_adjacent_lvn_below_the_zone():
    # Bimodal profile: VAL = 120, an LVN bin [110, 120] just below, HVN cluster above.
    profile = _profile(profile_bars_from_bin_volumes([5, 5, 450, 20, 10, 5, 10, 90, 90, 5]))
    stop = _long_stop(profile, profile.val, C)
    lvn = profile.nearest_lvn_below(profile.val)
    assert lvn is not None
    assert lvn.low <= stop <= lvn.high  # stop is inside the void bin
    assert stop < profile.val
    assert stop == 114.885  # LVN centre 115 nudged 0.1% into the void


def test_long_take_profit_sits_before_the_opposing_hvn_above():
    profile = _profile(profile_bars_from_bin_volumes([5, 5, 450, 20, 10, 5, 10, 90, 90, 5]))
    hvn = profile.nearest_hvn_above(profile.vah)
    assert hvn is not None
    tp = _long_take_profit(profile, profile.val, _long_stop(profile, profile.val, C), C)
    assert profile.vah < tp < hvn.low  # before the opposing cluster's near edge
    assert tp == 169.66


def test_take_profit_falls_back_to_r_multiple_without_an_opposing_hvn():
    # Single-peak profile has no HVN above the VAH, so TP is the R-multiple fallback.
    profile = _profile(profile_bars_from_bin_volumes([10, 20, 40, 80, 200, 80, 40, 20, 10, 5]))
    assert profile.nearest_hvn_above(profile.vah) is None
    stop = _long_stop(profile, profile.val, C)
    tp = _long_take_profit(profile, profile.val, stop, C)
    expected = profile.val + C.tp_fallback_rr * (profile.val - stop)
    assert tp == expected


def test_stop_falls_back_to_fixed_offset_without_an_adjacent_lvn():
    # A profile whose value area reaches the bottom bin: no LVN below the VAL.
    profile = _profile(profile_bars_from_bin_volumes([300, 180, 90, 40, 20, 10, 10, 10, 10, 5]))
    assert profile.nearest_lvn_below(profile.val) is None
    stop = _long_stop(profile, profile.val, C)
    assert stop == profile.val * (1.0 - C.stop_fallback_pct)
