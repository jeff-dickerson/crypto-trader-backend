"""Unit tests for the volume profile: POC, VAH, VAL, and HVN/LVN structure.

Profiles are built from zero-range candles placed into known bins, so every derived
quantity is hand-verifiable.
"""

from __future__ import annotations

from crypto_trader.strategy.volume_profile import build_volume_profile
from tests.fixtures.strategy_candles import (
    TEST_STRATEGY_CONFIG,
    make_candle,
    profile_bars_from_bin_volumes,
    zero_range_bar,
)

# 10 bins over [100, 200] (width 10). Single-peak profile with a POC at bin 4.
SINGLE_PEAK = [10, 20, 40, 80, 200, 80, 40, 20, 10, 5]


def test_poc_is_the_highest_volume_bin_centre():
    profile = build_volume_profile(
        profile_bars_from_bin_volumes(SINGLE_PEAK), TEST_STRATEGY_CONFIG
    )
    assert profile is not None
    assert profile.poc_index == 4
    assert profile.poc_price == 145.0


def test_value_area_edges_bracket_the_poc():
    profile = build_volume_profile(
        profile_bars_from_bin_volumes(SINGLE_PEAK), TEST_STRATEGY_CONFIG
    )
    # 70% of 505 total volume is covered by bins {3, 4, 5}: VAL = edge below bin 3,
    # VAH = edge above bin 5.
    assert profile.val == 130.0
    assert profile.vah == 160.0
    assert profile.val < profile.poc_price < profile.vah


def test_hvn_and_lvn_classification():
    profile = build_volume_profile(
        profile_bars_from_bin_volumes(SINGLE_PEAK), TEST_STRATEGY_CONFIG
    )
    # mean bin volume 50.5; HVN >= 1.3x (>= 65.65), LVN <= 0.5x (<= 25.25).
    assert profile.hvn_indices == (3, 4, 5)
    assert profile.lvn_indices == (0, 1, 7, 8, 9)


def test_bimodal_profile_keeps_second_cluster_outside_the_value_area():
    # A dominant POC at bin 2 and a secondary HVN cluster at bins 7-8 above the VAH.
    profile = build_volume_profile(
        profile_bars_from_bin_volumes([5, 5, 450, 20, 10, 5, 10, 90, 90, 5]),
        TEST_STRATEGY_CONFIG,
    )
    assert profile is not None
    assert profile.poc_index == 2
    assert profile.vah == 160.0
    # The upper cluster is an HVN but sits ABOVE the value area (an opposing HVN).
    assert 7 in profile.hvn_indices and 8 in profile.hvn_indices
    hvn_above = profile.nearest_hvn_above(profile.vah)
    assert hvn_above is not None and hvn_above.low == 170.0


def test_volume_is_spread_across_bins_a_wide_candle_overlaps():
    # One wide candle spanning the whole [100, 200] range with volume 100: its volume
    # is distributed across all bins, not dumped into one.
    profile = build_volume_profile(
        [make_candle(0, 150.0, 200.0, 100.0, 150.0, 100.0)], TEST_STRATEGY_CONFIG
    )
    assert profile is not None
    non_empty = [b for b in profile.bins if b.volume > 0]
    assert len(non_empty) == TEST_STRATEGY_CONFIG.profile_bins
    # Uniform spread: every bin holds an equal slice of the volume.
    assert all(abs(b.volume - 10.0) < 1e-9 for b in profile.bins)


def test_profile_is_computed_from_exactly_the_candles_given():
    # No-lookahead: dropping the last candle changes the profile; the function never
    # assumes a fixed complete window, it uses exactly what it is handed.
    full = profile_bars_from_bin_volumes(SINGLE_PEAK)
    fewer = full[:-1]
    assert build_volume_profile(full, TEST_STRATEGY_CONFIG) != build_volume_profile(
        fewer, TEST_STRATEGY_CONFIG
    )


def test_degenerate_inputs_return_none():
    assert build_volume_profile([], TEST_STRATEGY_CONFIG) is None
    # zero total volume
    assert (
        build_volume_profile([zero_range_bar(0, 100.0, 0.0)], TEST_STRATEGY_CONFIG)
        is None
    )
    # zero-width price range (every candle at one price)
    flat = [zero_range_bar(i, 100.0, 10.0) for i in range(5)]
    assert build_volume_profile(flat, TEST_STRATEGY_CONFIG) is None
