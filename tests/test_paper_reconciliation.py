"""Reconciliation tests: the documented tolerance and the drift conditions.

The tolerance has three parts (module docstring in reconciliation.py): a size epsilon at lot
precision, a price tick, and a funding/fee accrual allowance on monetary comparisons only. These
tests pin that ordinary accrual stays clean while a real size, side, existence, or missing-stop
drift is caught, since undetected drift is a Gate 2 critical failure (PRD 6.2).
"""

from __future__ import annotations

from crypto_trader.exchange.types import (
    Balance,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionMode,
    SymbolRule,
)
from crypto_trader.paper.reconciliation import (
    ExpectedPosition,
    ReconciliationConfig,
    reconcile,
)
from crypto_trader.strategy.position import PositionSide

RULE = SymbolRule(symbol="BTCUSDT", base_precision=3, quote_precision=1, min_trade_volume=0.001)


def _position(qty: float = 1.0, entry: float = 100.0, side: OrderSide = OrderSide.BUY) -> Position:
    return Position(
        symbol="BTCUSDT",
        side=side,
        quantity=qty,
        entry_price=entry,
        position_mode=PositionMode.ONE_WAY,
    )


def _stop(stop_price: float = 95.0) -> Order:
    return Order(
        order_id="s1",
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=1.0,
        filled_quantity=0.0,
        status=OrderStatus.NEW,
        stop_price=stop_price,
        reduce_only=True,
    )


def _expected(**overrides) -> ExpectedPosition:
    base = dict(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=1.0,
        entry_price=100.0,
        expects_resting_stop=True,
        stop_price=95.0,
    )
    base.update(overrides)
    return ExpectedPosition(**base)


def test_exact_match_is_clean() -> None:
    result = reconcile(_expected(), [_position()], [_stop()], RULE)
    assert result.is_clean
    assert result.drifts == ()


def test_within_size_and_price_tolerance_is_clean() -> None:
    # One lot (0.001) of size slack and one tick (0.1) of price slack are inside tolerance.
    result = reconcile(
        _expected(),
        [_position(qty=1.0009, entry=100.05)],
        [_stop(stop_price=95.05)],
        RULE,
    )
    assert result.is_clean


def test_size_drift_beyond_a_lot_is_caught() -> None:
    result = reconcile(_expected(), [_position(qty=1.05)], [_stop()], RULE)
    assert not result.is_clean
    assert any("size mismatch" in d for d in result.drifts)


def test_missing_protective_stop_is_caught() -> None:
    # Exposure with NO reduce-only stop resting is a "stop failed to rest" critical drift.
    result = reconcile(_expected(), [_position()], [], RULE)
    assert not result.is_clean
    assert any("no reduce-only stop" in d for d in result.drifts)


def test_stop_price_drift_beyond_a_tick_is_caught() -> None:
    result = reconcile(_expected(), [_position()], [_stop(stop_price=94.0)], RULE)
    assert not result.is_clean
    assert any("stop-price mismatch" in d for d in result.drifts)


def test_phantom_position_is_caught() -> None:
    flat = _expected(side=None, quantity=0.0, entry_price=None, expects_resting_stop=False)
    result = reconcile(flat, [_position()], [_stop()], RULE)
    assert not result.is_clean
    assert any("expects to be flat" in d for d in result.drifts)


def test_missing_position_is_caught() -> None:
    result = reconcile(_expected(), [], [], RULE)
    assert not result.is_clean
    assert any("expects a position" in d for d in result.drifts)


def test_side_mismatch_is_caught() -> None:
    result = reconcile(_expected(), [_position(side=OrderSide.SELL)], [_stop()], RULE)
    assert not result.is_clean
    assert any("side mismatch" in d for d in result.drifts)


def test_funding_fee_accrual_stays_within_the_equity_allowance() -> None:
    # Equity drifts by ordinary accrued funding/fees: within 0.5% of notional -> clean.
    notional = 1.0 * 100.0
    small_drift = 0.004 * notional  # 0.4% of notional, under the 0.5% allowance
    exp = _expected(expected_equity=2000.0)
    balance = Balance(currency="USDT", total=2000.0 - small_drift, available=1900.0)
    result = reconcile(exp, [_position()], [_stop()], RULE, balance)
    assert result.is_clean


def test_equity_drift_beyond_the_allowance_is_caught() -> None:
    exp = _expected(expected_equity=2000.0)
    # 10% gone, far past the accrual allowance.
    balance = Balance(currency="USDT", total=1800.0, available=1800.0)
    result = reconcile(exp, [_position()], [_stop()], RULE, balance)
    assert not result.is_clean
    assert any("equity drift" in d for d in result.drifts)


def test_tolerance_config_rejects_negatives() -> None:
    import pytest

    with pytest.raises(ValueError):
        ReconciliationConfig(size_tolerance_lots=-1.0)
