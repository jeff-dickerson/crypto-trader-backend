"""End-to-end paper-loop tests: the Gate 2 machine (PRD 6.2).

These prove the machine, not the edge: the approve-then-execute loop runs end to end over candle
data with honest fills, every submitted order traces to an approval, a protective stop always
rests while exposed, reconciliation catches an injected drift and freezes the symbol, and the
four-state lifecycle really reaches PARTIAL then OPEN. No network, no bot token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.approval.channel import ApprovalVerdict, EventKind
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.backtest.synthetic import generate_dataset
from crypto_trader.config import Timeframe
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.exchange.types import SymbolRule
from crypto_trader.ingest.models import Candle
from crypto_trader.paper.loop import PaperTrader
from crypto_trader.paper.position_manager import PositionManager
from crypto_trader.paper.risk import size_trade_plan
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG
from crypto_trader.strategy.position import PositionSide, PositionStatus

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
RULE = SymbolRule(symbol=SYMBOL, base_precision=4, quote_precision=1, min_trade_volume=0.0001)


def _trader(approval, adapter=None):
    adapter = adapter or DryRunExchangeAdapter(
        DryRunConfig(symbol_rules={SYMBOL: RULE, "ETHUSDT": RULE})
    )
    manager = PositionManager(adapter, approval, strategy_config=DEFAULT_STRATEGY_CONFIG)
    trader = PaperTrader(adapter, manager, strategy_config=DEFAULT_STRATEGY_CONFIG)
    return trader, manager, adapter


# --------------------------------------------------------------------------- full-run behaviour


def test_end_to_end_runs_clean_and_produces_trades() -> None:
    approval = InMemoryApprovalChannel(default_verdict=ApprovalVerdict.APPROVE)
    trader, manager, _ = _trader(approval)
    dataset = generate_dataset(["BTCUSDT", "ETHUSDT"], years=1.5)
    result = trader.run(dataset)

    assert result.closed_trades, "the machine should complete at least one trade"
    assert result.gate2_clean, f"critical failures: {result.critical_failures}"
    # Funnel: approvals requested >= approved >= submitted, and every submit had an approval.
    assert manager.submitted_order_plan_ids
    assert set(manager.submitted_order_plan_ids).issubset(
        pid for pid, v in manager.approvals if v is ApprovalVerdict.APPROVE
    )


def test_rejecting_every_plan_submits_no_orders() -> None:
    approval = InMemoryApprovalChannel(default_verdict=ApprovalVerdict.REJECT)
    trader, manager, _ = _trader(approval)
    dataset = generate_dataset(["BTCUSDT"], years=1.5)
    result = trader.run(dataset)

    # Plans were proposed and rejected, but nothing reached the exchange: no approval bypass.
    assert approval.requests, "the strategy should still have proposed plans"
    assert manager.submitted_order_plan_ids == []
    assert result.closed_trades == []
    assert result.gate2_clean  # zero orders means zero critical failures


def test_no_order_is_submitted_without_a_recorded_approval() -> None:
    approval = InMemoryApprovalChannel(default_verdict=ApprovalVerdict.APPROVE)
    trader, manager, _ = _trader(approval)
    trader.run(generate_dataset(["BTCUSDT"], years=1.2))
    approved = {pid for pid, v in manager.approvals if v is ApprovalVerdict.APPROVE}
    for plan_id in manager.submitted_order_plan_ids:
        assert plan_id in approved  # the approval-bypass guarantee, per trade


# --------------------------------------------------------------------------- lifecycle helpers


def _c(index: int, o: float, h: float, low: float, c: float) -> Candle:
    open_time = _BASE + index * Timeframe.H4.duration
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.H4,
        open_time=open_time,
        close_time=open_time + Timeframe.H4.duration,
        is_closed=True,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=100.0,
        quote_volume=None,
    )


class _Sig:
    """A minimal signal view for the white-box submit path."""

    entry_price = 100.0
    stop_price = 95.0
    take_profit_price = 110.0
    zone_boundary = 100.0


def _open_a_position(adapter, manager, *, partial: bool = False):
    """Drive one long position to a held-open state through the real submit/fill path."""
    ms = manager.managed(SYMBOL)
    plan = size_trade_plan(
        symbol=SYMBOL,
        side=PositionSide.LONG,
        entry_price=100.0,
        stop_price=95.0,
        take_profit_price=110.0,
        equity=adapter.get_balance().total,
        symbol_rule=RULE,
    )
    adapter.on_candle(_c(0, 100.0, 101.0, 99.6, 100.0))  # set the clock before placing
    manager._submit_entry(ms, plan.approved(), PositionSide.LONG, _Sig())
    assert ms.status is PositionStatus.PENDING
    # A later candle dips to the limit (99 > stop 95, so no same-bar stop breach).
    events = adapter.on_candle(_c(1, 100.0, 101.0, 99.0, 100.0))
    # A short window keeps the 10/20/50 MA ensemble uncomputable, so management HOLDs.
    fill_bar = _c(1, 100.0, 101.0, 99.0, 100.0)
    manager.on_bar(SYMBOL, 1, fill_bar, [fill_bar], [], events)
    return ms


# --------------------------------------------------------------------------- partial fills


def test_partial_fill_reaches_partial_then_open() -> None:
    adapter = DryRunExchangeAdapter(
        DryRunConfig(symbol_rules={SYMBOL: RULE}, partial_fill_ratio=0.5)
    )
    approval = InMemoryApprovalChannel()
    manager = PositionManager(adapter, approval, strategy_config=DEFAULT_STRATEGY_CONFIG)
    ms = manager.managed(SYMBOL)
    plan = size_trade_plan(
        symbol=SYMBOL, side=PositionSide.LONG, entry_price=100.0, stop_price=95.0,
        take_profit_price=110.0, equity=adapter.get_balance().total, symbol_rule=RULE,
    )
    adapter.on_candle(_c(0, 100.0, 101.0, 99.6, 100.0))
    manager._submit_entry(ms, plan.approved(), PositionSide.LONG, _Sig())

    win = [_c(1, 100.0, 101.0, 99.0, 100.0)]
    events = adapter.on_candle(_c(1, 100.0, 101.0, 99.0, 100.0))
    manager.on_bar(SYMBOL, 1, win[0], win, [], events)
    assert ms.status is PositionStatus.PARTIAL  # only half filled so far

    events = adapter.on_candle(_c(2, 100.0, 101.0, 99.0, 100.0))
    manager.on_bar(SYMBOL, 2, _c(2, 100.0, 101.0, 99.0, 100.0), win, [], events)
    assert ms.status is PositionStatus.OPEN  # remainder filled


# --------------------------------------------------------------------------- reconciliation


def test_reconciliation_freezes_on_a_vanished_stop_and_resumes_when_restored() -> None:
    adapter = DryRunExchangeAdapter(DryRunConfig(symbol_rules={SYMBOL: RULE}))
    approval = InMemoryApprovalChannel()
    manager = PositionManager(adapter, approval, strategy_config=DEFAULT_STRATEGY_CONFIG)
    trader = PaperTrader(adapter, manager, strategy_config=DEFAULT_STRATEGY_CONFIG)
    ms = _open_a_position(adapter, manager)
    assert ms.status is PositionStatus.OPEN

    # Baseline reconciliation is clean.
    assert trader._reconcile_symbol(SYMBOL) is False
    assert not manager.is_frozen(SYMBOL)

    # Inject a drift: the protective stop vanishes from the exchange while still exposed.
    stop_id = manager._resting_stop_order_id(SYMBOL)
    adapter.cancel_order(SYMBOL, stop_id)
    assert trader._reconcile_symbol(SYMBOL) is True
    assert manager.is_frozen(SYMBOL)
    assert approval.events_of(EventKind.RECONCILIATION_DRIFT)
    assert approval.events_of(EventKind.SYMBOL_FROZEN)

    # Restore the stop: the next clean reconciliation resumes the symbol.
    from crypto_trader.exchange.types import OrderSide, StopOrderRequest

    adapter.place_stop_order(
        StopOrderRequest(symbol=SYMBOL, side=OrderSide.SELL, quantity=ms.intended_quantity,
                         stop_price=ms.current_stop, reduce_only=True)
    )
    assert trader._reconcile_symbol(SYMBOL) is False
    assert not manager.is_frozen(SYMBOL)
    assert approval.events_of(EventKind.SYMBOL_RESUMED)


def test_frozen_symbol_refuses_new_entries() -> None:
    # Driven at the manager directly (the loop deliberately auto-resumes a flat+clean symbol):
    # while frozen, a bar that WOULD fire an entry proposes and submits nothing.
    from tests.fixtures.strategy_candles import (
        TEST_STRATEGY_CONFIG,
        long_entry_window,
        rising_daily,
    )

    adapter = DryRunExchangeAdapter(DryRunConfig(symbol_rules={SYMBOL: RULE}))
    approval = InMemoryApprovalChannel(default_verdict=ApprovalVerdict.APPROVE)
    manager = PositionManager(adapter, approval, strategy_config=TEST_STRATEGY_CONFIG)
    h4 = long_entry_window()
    d1 = rising_daily(count=12)
    last = len(h4) - 1

    manager.set_frozen(SYMBOL, True)
    adapter.on_candle(h4[last])
    manager.on_bar(SYMBOL, last, h4[last], h4, d1, [])
    assert approval.requests == []  # frozen: no plan even proposed
    assert manager.submitted_order_plan_ids == []

    # Unfreezing lets the very same setup propose a plan, proving the freeze was the blocker.
    manager.set_frozen(SYMBOL, False)
    manager.on_bar(SYMBOL, last, h4[last], h4, d1, [])
    assert approval.requests, "the same bar should propose an entry once unfrozen"
