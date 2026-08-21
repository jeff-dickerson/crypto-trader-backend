"""Funding tests: settlement enumeration, signed cost, and the captain-decision-4 gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from crypto_trader.backtest.funding import (
    FundingSchedule,
    constant_schedule,
    funding_is_prohibitive,
    signed_adverse_rate,
)
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG
from crypto_trader.strategy.position import PositionSide

T0 = datetime(2021, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_settlement_times_are_the_8h_marks_strictly_after_start_through_end():
    sched = constant_schedule(["X"], 0.0001, T0)
    end = datetime(2021, 1, 1, 20, 0, tzinfo=timezone.utc)
    marks = sched.settlement_times(T0, end)
    assert marks == [
        datetime(2021, 1, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2021, 1, 1, 16, 0, tzinfo=timezone.utc),
    ]


def test_funding_cost_fraction_sign_follows_the_side():
    sched = constant_schedule(["X"], 0.001, T0)
    end = datetime(2021, 1, 1, 20, 0, tzinfo=timezone.utc)  # two settlements
    long_cost = sched.funding_cost_fraction("X", PositionSide.LONG, T0, end)
    short_cost = sched.funding_cost_fraction("X", PositionSide.SHORT, T0, end)
    assert long_cost == pytest.approx(0.002)  # a long PAYS a positive rate
    assert short_cost == pytest.approx(-0.002)  # a short EARNS it


def test_rate_at_is_a_step_function():
    sched = FundingSchedule(
        rates_by_symbol={
            "X": [
                (T0, 0.0001),
                (datetime(2021, 1, 2, tzinfo=timezone.utc), 0.0009),
            ]
        }
    )
    assert sched.rate_at("X", datetime(2021, 1, 1, 12, tzinfo=timezone.utc)) == 0.0001
    assert sched.rate_at("X", datetime(2021, 1, 3, tzinfo=timezone.utc)) == 0.0009
    assert sched.rate_at("X", datetime(2020, 12, 31, tzinfo=timezone.utc)) == 0.0
    assert sched.rate_at("MISSING", T0) == 0.0


def test_signed_adverse_rate_flips_for_a_short():
    assert signed_adverse_rate(0.001, PositionSide.LONG) == 0.001
    assert signed_adverse_rate(0.001, PositionSide.SHORT) == -0.001


def test_filter_disabled_never_blocks():
    cfg = DEFAULT_STRATEGY_CONFIG  # funding_filter_enabled defaults to False
    assert not funding_is_prohibitive(0.01, PositionSide.LONG, cfg)


def test_filter_blocks_only_adverse_funding_past_the_threshold():
    cfg = replace(DEFAULT_STRATEGY_CONFIG, funding_filter_enabled=True)
    thr = cfg.funding_filter_max_adverse_rate
    # Long: a positive rate is adverse (the long pays).
    assert funding_is_prohibitive(thr + 1e-6, PositionSide.LONG, cfg)
    assert not funding_is_prohibitive(thr - 1e-6, PositionSide.LONG, cfg)
    assert not funding_is_prohibitive(-0.01, PositionSide.LONG, cfg)  # favourable, never blocks
    # Short: a negative rate is adverse (the short pays).
    assert funding_is_prohibitive(-(thr + 1e-6), PositionSide.SHORT, cfg)
    assert not funding_is_prohibitive(0.01, PositionSide.SHORT, cfg)
