"""Cost-model unit tests: fees, slippage direction, funding sign, and minimum notional."""

from __future__ import annotations

import pytest

from crypto_trader.backtest.costs import (
    CostConfig,
    compute_costs,
    min_notional_ok,
    position_notional,
)
from crypto_trader.strategy.position import PositionSide

CC = CostConfig()


def test_long_market_exit_costs_reduce_the_gross_r():
    b = compute_costs(
        PositionSide.LONG, 100.0, 110.0, 90.0,
        exit_is_maker=False, funding_fraction=0.0, config=CC,
    )
    assert b.risk_distance == 10.0
    assert b.gross_r == pytest.approx(1.0)
    # maker entry 0.02, taker exit 0.066, slippage 0.055 -> net price 9.859 -> 0.9859 R.
    assert b.net_r == pytest.approx(0.9859, abs=1e-6)
    assert b.net_r < b.gross_r


def test_maker_take_profit_exit_takes_no_slippage():
    market = compute_costs(
        PositionSide.LONG, 100.0, 110.0, 90.0,
        exit_is_maker=False, funding_fraction=0.0, config=CC,
    )
    maker = compute_costs(
        PositionSide.LONG, 100.0, 110.0, 90.0,
        exit_is_maker=True, funding_fraction=0.0, config=CC,
    )
    assert maker.slippage_price == 0.0
    assert market.slippage_price > 0.0
    assert maker.net_r > market.net_r  # a limit exit keeps more of the move


def test_short_slippage_pushes_the_exit_the_adverse_way():
    b = compute_costs(
        PositionSide.SHORT, 100.0, 90.0, 110.0,
        exit_is_maker=False, funding_fraction=0.0, config=CC,
    )
    # A short profits as price falls; slippage makes it exit HIGHER (worse), cutting net R.
    assert b.gross_r == pytest.approx(1.0)
    assert b.net_r < b.gross_r


def test_positive_funding_is_a_cost_negative_funding_is_a_rebate():
    paid = compute_costs(
        PositionSide.LONG, 100.0, 110.0, 90.0,
        exit_is_maker=True, funding_fraction=0.001, config=CC,
    )
    earned = compute_costs(
        PositionSide.LONG, 100.0, 110.0, 90.0,
        exit_is_maker=True, funding_fraction=-0.001, config=CC,
    )
    # 0.001 of a 100 notional is 0.1 price, i.e. 0.01 R either way.
    assert earned.net_r - paid.net_r == pytest.approx(0.02, abs=1e-9)


def test_zero_risk_distance_is_rejected():
    with pytest.raises(ValueError):
        compute_costs(
            PositionSide.LONG, 100.0, 110.0, 100.0,
            exit_is_maker=False, funding_fraction=0.0, config=CC,
        )


def test_minimum_notional_gate():
    # 2% of 2000 = 40 risk; entry 100, stop 90 -> qty 4 -> notional 400.
    assert position_notional(100.0, 90.0, CC) == pytest.approx(400.0)
    assert min_notional_ok(100.0, 90.0, CostConfig(min_notional=100.0))
    assert not min_notional_ok(100.0, 90.0, CostConfig(min_notional=1000.0))
