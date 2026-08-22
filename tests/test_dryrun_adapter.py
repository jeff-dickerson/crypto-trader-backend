"""Honest-fills tests for the DryRun (paper) ExchangeAdapter.

Each test pins one of the honest-fills rules the DryRun adapter exists to guarantee, mapped to
the Gate 2 critical failures (PRD 6.2): resting limits fill only on a subsequent trade-through,
stops always slip, the worst-of-intrabar rule holds, the native protective stop rests the instant
an entry fills, and partial fills advance the four-state contract. No network, no clock, no bot.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.config import Timeframe
from crypto_trader.exchange.adapter import ExchangeConnectionError
from crypto_trader.exchange.dryrun import (
    DryRunConfig,
    DryRunExchangeAdapter,
    SimEventKind,
)
from crypto_trader.exchange.types import (
    MarketOrderRequest,
    OrderRequest,
    OrderSide,
    OrderStatus,
    SymbolRule,
    TimeInForce,
)
from crypto_trader.ingest.models import Candle

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
# A coarse-precision rule so sizes are whole-ish and prices round cleanly in assertions.
RULE = SymbolRule(symbol=SYMBOL, base_precision=3, quote_precision=1, min_trade_volume=0.001)


def _c(index: int, o: float, h: float, low: float, close: float, symbol: str = SYMBOL) -> Candle:
    open_time = _BASE + index * Timeframe.H4.duration
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.H4,
        open_time=open_time,
        close_time=open_time + Timeframe.H4.duration,
        is_closed=True,
        open=o,
        high=h,
        low=low,
        close=close,
        volume=100.0,
        quote_volume=None,
    )


def _adapter(**overrides) -> DryRunExchangeAdapter:
    cfg = DryRunConfig(symbol_rules={SYMBOL: RULE}, starting_equity=10_000.0, **overrides)
    return DryRunExchangeAdapter(cfg)


def _long_entry(price: float = 100.0, qty: float = 1.0) -> OrderRequest:
    return OrderRequest(
        symbol=SYMBOL,
        side=OrderSide.BUY,
        quantity=qty,
        price=price,
        time_in_force=TimeInForce.POST_ONLY,
        reduce_only=False,
        stop_loss_price=95.0,
        take_profit_price=110.0,
    )


def test_resting_limit_does_not_fill_on_the_placement_candle() -> None:
    a = _adapter()
    # Advance the clock to bar 0, then place the entry: its placed_clock is bar 0's open_time.
    a.on_candle(_c(0, 100.0, 101.0, 99.0, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    # A candle at the SAME clock that trades through must NOT fill it (strictly-later rule).
    events = a.on_candle(_c(0, 100.0, 101.0, 90.0, 95.0))
    assert not [e for e in events if e.kind is SimEventKind.ENTRY_FILL]
    assert a.get_positions(SYMBOL) == []


def test_resting_limit_fills_only_when_a_later_candle_trades_through() -> None:
    a = _adapter()
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    # Next candle stays above the limit: no fill.
    assert not a.on_candle(_c(1, 101.0, 102.0, 100.5, 101.5))
    assert a.get_positions(SYMBOL) == []
    # A later candle dips through the limit: it fills at the limit price (maker, no slippage).
    events = a.on_candle(_c(2, 101.0, 102.0, 99.0, 101.0))
    fills = [e for e in events if e.kind is SimEventKind.ENTRY_FILL]
    assert len(fills) == 1 and fills[0].price == 100.0 and fills[0].is_maker
    positions = a.get_positions(SYMBOL)
    assert len(positions) == 1 and positions[0].entry_price == 100.0


def test_entry_fill_rests_a_native_protective_stop_and_take_profit() -> None:
    a = _adapter()
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # fills the entry
    orders = a.get_open_orders(SYMBOL)
    stops = [o for o in orders if o.reduce_only and o.stop_price is not None]
    tps = [o for o in orders if o.reduce_only and o.stop_price is None and o.price is not None]
    assert len(stops) == 1 and stops[0].stop_price == 95.0  # the floor rests immediately
    assert len(tps) == 1 and tps[0].price == 110.0


def test_stop_always_slips_never_fills_at_the_stop_price() -> None:
    cost = CostConfig(slippage_rate=0.001)
    a = _adapter(cost=cost)
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # fill entry at 100, stop rests at 95
    # A later candle drops to the stop: it fills BELOW 95 by the slippage, never at 95.
    events = a.on_candle(_c(2, 99.0, 99.0, 90.0, 92.0))
    stop_events = [e for e in events if e.kind is SimEventKind.STOP_HIT]
    assert len(stop_events) == 1
    assert stop_events[0].price == pytest.approx(95.0 * (1 - 0.001))
    assert stop_events[0].price < 95.0
    assert a.get_positions(SYMBOL) == []  # position closed


def test_take_profit_fills_at_its_level_with_no_slippage() -> None:
    a = _adapter()
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # fill entry
    events = a.on_candle(_c(2, 101.0, 111.0, 100.0, 110.5))  # rallies through the TP at 110
    tp = [e for e in events if e.kind is SimEventKind.TAKE_PROFIT_HIT]
    assert len(tp) == 1 and tp[0].price == 110.0 and tp[0].is_maker


def test_worst_of_intrabar_stop_wins_ties_with_the_take_profit() -> None:
    a = _adapter()
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # fill entry, stop 95 / tp 110 rest
    # One candle reaches BOTH the stop and the take-profit: the stop must win (worse outcome).
    events = a.on_candle(_c(2, 100.0, 111.0, 90.0, 100.0))
    kinds = [e.kind for e in events]
    assert SimEventKind.STOP_HIT in kinds
    assert SimEventKind.TAKE_PROFIT_HIT not in kinds


def test_same_bar_stop_breach_on_the_fill_candle_closes_conservatively() -> None:
    a = _adapter()
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    # The very candle that fills the entry also reaches the stop: assume stopped out (worst-of).
    events = a.on_candle(_c(1, 101.0, 102.0, 94.0, 96.0))
    kinds = [e.kind for e in events]
    assert SimEventKind.ENTRY_FILL in kinds
    assert SimEventKind.STOP_HIT in kinds
    assert a.get_positions(SYMBOL) == []


def test_partial_fill_advances_to_open_on_a_second_trade_through() -> None:
    a = _adapter(partial_fill_ratio=0.5)
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0, qty=1.0))
    first = a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))
    fill = [e for e in first if e.kind is SimEventKind.ENTRY_FILL][0]
    assert fill.filled_fraction == pytest.approx(0.5)  # partial
    pos = a.get_positions(SYMBOL)[0]
    assert pos.quantity == pytest.approx(0.5)
    entry_order = [o for o in a.get_open_orders(SYMBOL) if not o.reduce_only][0]
    assert entry_order.status is OrderStatus.PARTIALLY_FILLED
    # A second trade-through fills the remainder: now OPEN, entry order gone.
    a.on_candle(_c(2, 101.0, 102.0, 99.0, 101.0))
    assert a.get_positions(SYMBOL)[0].quantity == pytest.approx(1.0)
    assert [o for o in a.get_open_orders(SYMBOL) if not o.reduce_only] == []


def test_stop_out_after_partial_fill_cancels_the_stale_entry_remainder() -> None:
    a = _adapter(partial_fill_ratio=0.5)
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0, qty=1.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # partial fill: 0.5 filled, 0.5 stays resting
    pos = a.get_positions(SYMBOL)[0]
    assert pos.quantity == pytest.approx(0.5)
    # The stop hits: the position closes, and the stale entry remainder must not survive it.
    events = a.on_candle(_c(2, 96.0, 96.0, 90.0, 92.0))
    assert [e.kind for e in events] == [SimEventKind.STOP_HIT]
    assert a.get_positions(SYMBOL) == []
    assert a.get_open_orders(SYMBOL) == []
    # A later candle trades back through the old entry price: it must NOT reopen a position.
    events = a.on_candle(_c(3, 101.0, 102.0, 99.0, 101.0))
    assert not [e for e in events if e.kind is SimEventKind.ENTRY_FILL]
    assert a.get_positions(SYMBOL) == []


def test_market_order_closes_immediately_with_taker_slippage() -> None:
    cost = CostConfig(slippage_rate=0.002)
    a = _adapter(cost=cost)
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0))
    a.on_candle(_c(2, 101.0, 108.0, 99.0, 106.0))  # fill entry at 100, mark now 106
    order = a.place_market_order(
        MarketOrderRequest(symbol=SYMBOL, side=OrderSide.SELL, quantity=1.0, reduce_only=True)
    )
    assert order.status is OrderStatus.FILLED
    assert order.price == pytest.approx(106.0 * (1 - 0.002))  # sells below the mark
    assert a.get_positions(SYMBOL) == []
    # The orphaned protective legs are cancelled when the position flattens.
    assert a.get_open_orders(SYMBOL) == []


def test_balance_reflects_fees_and_realized_pnl() -> None:
    cost = CostConfig(maker_fee_rate=0.0002, taker_fee_rate=0.0006, slippage_rate=0.0)
    a = _adapter(cost=cost)
    start = a.get_balance().total
    a.on_candle(_c(0, 100.0, 101.0, 99.5, 100.0))
    a.place_limit_order(_long_entry(price=100.0, qty=1.0))
    a.on_candle(_c(1, 101.0, 102.0, 99.0, 101.0))  # entry fills, pays maker fee on 100 notional
    a.on_candle(_c(2, 101.0, 111.0, 100.0, 110.5))  # take-profit at 110 (maker)
    # Gross pnl 10 per unit; fees: entry 100*0.0002 + exit 110*0.0002 = 0.02 + 0.022.
    expected = start + 10.0 - (100.0 * 0.0002) - (110.0 * 0.0002)
    assert a.get_balance().total == pytest.approx(expected)


def test_rate_limit_status_is_always_healthy_for_paper() -> None:
    a = _adapter()
    status = a.rate_limit_status()
    assert status.remaining == status.limit_per_window  # nothing consumed
    assert status.remaining > 0


def test_unknown_symbol_rule_raises_rather_than_guessing() -> None:
    a = _adapter()
    with pytest.raises(KeyError):
        a.get_symbol_rule("DOGEUSDT")


# --------------------------------------------------------------------------- kill-switch support


def test_simulate_outage_raises_on_the_next_n_calls_then_recovers() -> None:
    a = _adapter()
    a.simulate_outage(2)
    with pytest.raises(ExchangeConnectionError):
        a.get_balance()
    assert a.consecutive_api_failures() == 1
    with pytest.raises(ExchangeConnectionError):
        a.get_positions()
    assert a.consecutive_api_failures() == 2
    # The outage budget is exhausted: the next call succeeds and resets the failure count.
    a.get_balance()
    assert a.consecutive_api_failures() == 0


def test_consecutive_api_failures_starts_at_zero() -> None:
    a = _adapter()
    assert a.consecutive_api_failures() == 0
