"""KillSwitch and KillSwitchMonitor tests (Build Order step 5, PRD 9.1).

Each auto-trigger fires, the degraded-mode fallback keeps retrying with backoff and never
reports flat until the exchange confirms it, and rearm restores normal operation. A small
FakeAdapter (a full ExchangeAdapter implementation with directly-settable equity, positions, and
orders, plus a fail-the-next-N-calls hook) drives the scenarios precisely and deterministically,
the same pattern test_exchange_adapter.py already uses for its StubAdapter. No network, no clock,
no bot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crypto_trader.approval.channel import EventKind
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.exchange.adapter import ExchangeAdapter, ExchangeConnectionError
from crypto_trader.exchange.types import (
    Balance,
    ExchangeCapabilities,
    MarketOrderRequest,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionMode,
    RateLimitStatus,
    StopOrderRequest,
    SymbolRule,
)
from crypto_trader.safety.kill_switch import KillSwitch, KillSwitchReason
from crypto_trader.safety.monitor import (
    KillSwitchConfig,
    KillSwitchMonitor,
    websocket_dead_trigger,
)

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


class FakeAdapter(ExchangeAdapter):
    """A directly-controllable ExchangeAdapter for kill-switch scenarios.

    `equity` and `positions`/`orders` are set by the test; `fail_calls` makes the next N guarded
    calls raise ExchangeConnectionError (mirroring DryRunExchangeAdapter.simulate_outage), and
    `consecutive_api_failures()` tracks the same running count a real transport would.
    """

    def __init__(self, equity: float = 10_000.0) -> None:
        self.equity = equity
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.fail_calls = 0
        self._failures = 0
        self.cancelled_order_ids: list[str] = []
        self.market_orders: list[MarketOrderRequest] = []

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return ExchangeCapabilities()

    def get_symbol_rule(self, symbol: str) -> SymbolRule:
        return SymbolRule(
            symbol=symbol, base_precision=4, quote_precision=1, min_trade_volume=0.0001
        )

    def _guard(self) -> None:
        if self.fail_calls > 0:
            self.fail_calls -= 1
            self._failures += 1
            raise ExchangeConnectionError("simulated outage")
        self._failures = 0

    def place_limit_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError

    def place_stop_order(self, request: StopOrderRequest) -> Order:
        raise NotImplementedError

    def place_market_order(self, request: MarketOrderRequest) -> Order:
        self._guard()
        self.market_orders.append(request)
        self.positions = [p for p in self.positions if p.symbol != request.symbol]
        return Order(
            order_id=f"mkt-{len(self.market_orders)}",
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            status=OrderStatus.FILLED,
            price=100.0,
        )

    def cancel_order(self, symbol: str, order_id: str) -> Order:
        self._guard()
        order = next(o for o in self.orders if o.order_id == order_id)
        self.orders = [o for o in self.orders if o.order_id != order_id]
        self.cancelled_order_ids.append(order_id)
        return order

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._guard()
        return [o for o in self.orders if symbol is None or o.symbol == symbol]

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        self._guard()
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def get_balance(self) -> Balance:
        self._guard()
        return Balance(currency="USDT", total=self.equity, available=self.equity)

    def rate_limit_status(self) -> RateLimitStatus:
        return RateLimitStatus(
            scope="test", limit_per_window=10, used_in_window=0, window_seconds=1.0
        )

    def consecutive_api_failures(self) -> int:
        return self._failures


def _position(symbol: str = "BTCUSDT", qty: float = 1.0) -> Position:
    return Position(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=qty,
        entry_price=100.0,
        position_mode=PositionMode.ONE_WAY,
    )


def _order(order_id: str, symbol: str = "BTCUSDT") -> Order:
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=1.0,
        filled_quantity=0.0,
        status=OrderStatus.NEW,
        reduce_only=True,
        stop_price=95.0,
    )


def _monitor(fake: FakeAdapter, approval, **overrides) -> tuple[KillSwitchMonitor, KillSwitch]:
    kill_switch = KillSwitch()
    config = KillSwitchConfig(**overrides) if overrides else KillSwitchConfig()
    monitor = KillSwitchMonitor(fake, approval, kill_switch, config=config, sleep_fn=lambda s: None)
    return monitor, kill_switch


# --------------------------------------------------------------------------- KillSwitch itself


def test_kill_switch_starts_armed() -> None:
    ks = KillSwitch()
    assert ks.is_armed
    assert ks.flatten_confirmed
    assert ks.last_event is None


def test_trigger_halts_immediately_and_marks_flatten_unconfirmed() -> None:
    ks = KillSwitch()
    event = ks.trigger(KillSwitchReason.MANUAL, "operator halt", now=_BASE)
    assert not ks.is_armed
    assert not ks.flatten_confirmed
    assert ks.last_event is event
    assert event.reason is KillSwitchReason.MANUAL


def test_rearm_restores_armed_state_and_clears_the_event() -> None:
    ks = KillSwitch()
    ks.trigger(KillSwitchReason.MANUAL, "halt", now=_BASE)
    ks.rearm()
    assert ks.is_armed
    assert ks.flatten_confirmed
    assert ks.last_event is None


# --------------------------------------------------------------------------- auto-triggers


def test_daily_loss_trigger_fires_and_flattens_cleanly() -> None:
    fake = FakeAdapter(equity=10_000.0)
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval)

    monitor.check_cycle(now=_BASE)  # baseline: day_start = 10_000
    fake.equity = 9_300.0  # a 7% daily loss, past the 6% threshold
    monitor.check_cycle(now=_BASE + timedelta(hours=4))

    assert not kill_switch.is_armed
    assert kill_switch.last_event is not None
    assert kill_switch.last_event.reason is KillSwitchReason.DAILY_LOSS
    # No exposure on this fake, so the flatten has nothing to do and confirms immediately.
    assert kill_switch.flatten_confirmed
    assert approval.events_of(EventKind.KILL_SWITCH_FIRED)


def test_max_drawdown_trigger_fires_when_no_single_day_breaches_the_daily_loss_bar() -> None:
    # Equity decays 3%/day (under the 6% daily-loss bar every day) until cumulative drawdown
    # from the peak passes the 15% max-drawdown bar, isolating the drawdown trigger.
    fake = FakeAdapter(equity=12_000.0)
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval)

    monitor.check_cycle(now=_BASE)  # day 0: peak = day_start = 12_000
    equity = 12_000.0
    day = 1
    while kill_switch.is_armed and day < 20:
        fake.equity = equity  # start of this day
        monitor.check_cycle(now=_BASE + timedelta(days=day))
        equity *= 0.97  # a 3% loss within the day
        fake.equity = equity
        monitor.check_cycle(now=_BASE + timedelta(days=day, hours=4))
        day += 1

    assert not kill_switch.is_armed
    assert kill_switch.last_event.reason is KillSwitchReason.MAX_DRAWDOWN


def test_consecutive_api_failures_trigger_fires_after_the_threshold() -> None:
    fake = FakeAdapter(equity=10_000.0)
    fake.fail_calls = 5
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval, max_consecutive_api_failures=5)

    for i in range(5):
        monitor.check_cycle(now=_BASE + timedelta(hours=4 * i))
        if not kill_switch.is_armed:
            break

    assert not kill_switch.is_armed
    assert kill_switch.last_event.reason is KillSwitchReason.API_FAILURES
    assert kill_switch.last_event.detail["consecutive_failures"] >= 5


def test_fewer_than_threshold_failures_do_not_trigger() -> None:
    fake = FakeAdapter(equity=10_000.0)
    fake.fail_calls = 4  # one short of the default 5
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval, max_consecutive_api_failures=5)

    for i in range(4):
        monitor.check_cycle(now=_BASE + timedelta(hours=4 * i))

    assert kill_switch.is_armed  # recovered before the threshold


# --------------------------------------------------------------------------- degraded mode


def test_degraded_mode_retries_within_backoff_then_recovers() -> None:
    """Flatten fails a few times in a row but succeeds within the same cycle's attempt budget."""
    fake = FakeAdapter(equity=5_000.0)
    fake.positions = [_position()]
    fake.orders = [_order("stop-1")]
    fake.fail_calls = 3  # fewer than flatten_max_attempts (5): recovers within this cycle
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval)

    kill_switch.trigger(KillSwitchReason.MANUAL, "test halt", now=_BASE)
    monitor._attempt_flatten(_BASE)

    assert kill_switch.flatten_confirmed
    assert fake.positions == []
    assert fake.orders == []
    degraded_events = approval.events_of(EventKind.KILL_SWITCH_DEGRADED)
    assert len(degraded_events) == 3  # one alert per failed attempt
    assert approval.events_of(EventKind.KILL_SWITCH_FLATTENED)


def test_degraded_mode_persists_across_cycles_until_the_exchange_confirms_flat() -> None:
    """Flatten cannot reach the exchange at all this cycle; it must NOT report killed, and the
    next cycle's check_cycle must keep retrying (not re-evaluate auto-triggers) until it can."""
    fake = FakeAdapter(equity=5_000.0)
    fake.positions = [_position()]
    fake.orders = [_order("stop-1")]
    fake.fail_calls = 100  # exceeds flatten_max_attempts: nothing succeeds this cycle
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval)

    kill_switch.trigger(KillSwitchReason.MANUAL, "test halt", now=_BASE)
    monitor._attempt_flatten(_BASE)

    # Not reported killed: the on-exchange resting stop is what still protects the account.
    assert not kill_switch.flatten_confirmed
    assert fake.positions  # never force-cleared without exchange confirmation
    assert not kill_switch.is_armed  # halt still took effect immediately regardless
    assert approval.events_of(EventKind.KILL_SWITCH_DEGRADED)
    assert not approval.events_of(EventKind.KILL_SWITCH_FLATTENED)

    # The exchange "recovers": a later cycle keeps retrying (the early-return branch) and now
    # succeeds, confirming flat and alerting once.
    fake.fail_calls = 0
    monitor.check_cycle(now=_BASE + timedelta(hours=4))

    assert kill_switch.flatten_confirmed
    assert fake.positions == []
    assert approval.events_of(EventKind.KILL_SWITCH_FLATTENED)


def test_repeat_trigger_while_halted_does_not_reopen_a_confirmed_flatten() -> None:
    """A second, unrelated trigger arriving after the exchange already confirmed flat must not
    re-mark flatten_confirmed False: there is nothing new to flatten, only a newer reason to
    record (regression test for the reset-guard bug fixed alongside flatten ordering)."""
    ks = KillSwitch()
    ks.trigger(KillSwitchReason.MANUAL, "first halt", now=_BASE)
    ks.mark_flatten_confirmed()
    assert ks.flatten_confirmed

    ks.trigger(KillSwitchReason.DAILY_LOSS, "second halt", now=_BASE + timedelta(hours=1))

    assert not ks.is_armed
    assert ks.flatten_confirmed  # must not be reopened by the repeat trigger
    assert ks.last_event.reason is KillSwitchReason.DAILY_LOSS  # newest reason still recorded


def test_cancel_and_close_all_closes_positions_before_cancelling_their_stops() -> None:
    """Regression test for the flatten-ordering fix: an interrupted flatten must never leave a
    position both open and unprotected, so positions are closed at market before their resting
    stop orders are cancelled."""
    fake = FakeAdapter(equity=5_000.0)
    fake.positions = [_position()]
    fake.orders = [_order("stop-1")]
    approval = InMemoryApprovalChannel()
    monitor, _ = _monitor(fake, approval)

    call_log: list[str] = []
    orig_place_market_order = fake.place_market_order
    orig_cancel_order = fake.cancel_order
    fake.place_market_order = lambda request: (
        call_log.append("close_position"),
        orig_place_market_order(request),
    )[1]
    fake.cancel_order = lambda symbol, order_id: (
        call_log.append("cancel_order"),
        orig_cancel_order(symbol, order_id),
    )[1]

    monitor._cancel_and_close_all()

    assert call_log == ["close_position", "cancel_order"]


def test_a_triggered_kill_switch_stops_evaluating_auto_triggers() -> None:
    """Once halted, check_cycle takes the retry-flatten branch, never re-checking equity."""
    fake = FakeAdapter(equity=10_000.0)
    approval = InMemoryApprovalChannel()
    monitor, kill_switch = _monitor(fake, approval)
    kill_switch.trigger(KillSwitchReason.MANUAL, "already halted", now=_BASE)

    fake.equity = 1.0  # would trip every equity-based trigger if re-evaluated
    monitor.check_cycle(now=_BASE + timedelta(hours=4))

    assert kill_switch.last_event.reason is KillSwitchReason.MANUAL  # unchanged
    assert kill_switch.flatten_confirmed  # nothing was open, so the retry confirmed flat


# --------------------------------------------------------------------------- websocket trigger
# (documented N/A for the paper adapter; this is the pure condition, ready for step 6)


def test_websocket_dead_trigger_fires_only_with_open_positions_and_a_stale_heartbeat() -> None:
    stale = _BASE - timedelta(minutes=10)
    assert websocket_dead_trigger(stale, _BASE, has_open_positions=True) is True


def test_websocket_dead_trigger_is_false_without_open_positions() -> None:
    stale = _BASE - timedelta(minutes=10)
    assert websocket_dead_trigger(stale, _BASE, has_open_positions=False) is False


def test_websocket_dead_trigger_is_false_with_a_fresh_heartbeat() -> None:
    fresh = _BASE - timedelta(seconds=30)
    assert websocket_dead_trigger(fresh, _BASE, has_open_positions=True) is False


def test_websocket_dead_trigger_treats_unknown_as_not_dead() -> None:
    # No heartbeat data at all (the paper adapter's actual case) must never be treated as dead.
    assert websocket_dead_trigger(None, _BASE, has_open_positions=True) is False
