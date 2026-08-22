"""The position manager: signal consumption, approval, submission, and the position lifecycle.

This is the brain of the paper loop. Per bar it:

1. Observes exchange-reported truth (get_positions / get_open_orders / get_balance) and moves its
   own position state along the four-state contract (FLAT -> PENDING -> PARTIAL/OPEN -> FLAT) from
   what the adapter ACTUALLY reports, never from assuming a placed order executed as intended
   (governing principle 1). A close it did not itself command is attributed from the adapter's
   SimEvents for this bar (a stop or take-profit that fired), so the exit reason is exchange truth.

2. Decides with the unmodified pure core. FLAT seeks an entry via generate_signal; a filled
   position is managed via generate_signal (trailing stop, momentum-shift exit). The manager never
   reimplements the decision, so paper and backtest and live cannot diverge on it.

3. Sizes, gets approval, and submits. A strategy entry becomes a risk-sized TradePlan (risk.py);
   the plan is voided visibly at plan time if it cannot meet the exchange minimum (PRD 4.2); an
   affordable plan is sent to the ApprovalChannel; and an order reaches the exchange ONLY on an
   APPROVE decision, which is recorded, so no order can bypass approval (a Gate 2 critical failure,
   PRD 6.2). The entry is placed with its native attached stop/take-profit bracket, so the
   on-exchange stop rests the instant the entry fills (principle 4).

Trailing is emulated by cancel-and-replace of the resting stop, because native_trailing_stop is
False on the venue (AGENTS.md); the manager honours capabilities rather than assuming an idealized
trailing stop. A frozen symbol (set by reconciliation on a drift) refuses NEW entries but still
manages an existing position conservatively (its stop and take-profit keep resting, and it may
tighten the stop or exit), because abandoning a live position would be less safe, not more.

An optional `crypto_trader.safety.kill_switch.KillSwitch` (Build Order step 5) is the same kind of
gate, but account-wide rather than per-symbol: while triggered, `_maybe_seek_entry` refuses every
new entry. The kill switch's own flatten (crypto_trader.safety.monitor) acts directly against the
adapter, not through this manager, so an exchange-side close it causes is picked up on the next
bar's `_sync_from_exchange`, exactly like any other exchange-attributed close (see AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from crypto_trader.approval.channel import (
    ApprovalChannel,
    ApprovalVerdict,
    ChannelEvent,
    EventKind,
)
from crypto_trader.exchange.adapter import ExchangeAdapter
from crypto_trader.exchange.dryrun import SimEvent, SimEventKind
from crypto_trader.exchange.types import (
    MarketOrderRequest,
    OrderRequest,
    OrderSide,
    StopOrderRequest,
    TimeInForce,
)
from crypto_trader.ingest.models import Candle
from crypto_trader.paper.plan import TradePlan
from crypto_trader.paper.reconciliation import ExpectedPosition
from crypto_trader.paper.risk import DEFAULT_RISK_CONFIG, RiskConfig, size_trade_plan
from crypto_trader.safety.kill_switch import KillSwitch
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from crypto_trader.strategy.position import PositionSide, PositionState, PositionStatus
from crypto_trader.strategy.signal import SignalAction, generate_signal

# A resting entry limit is left to rest for this many bars, then cancelled unfilled (mirrors the
# backtest's entry_valid_bars: 6 bars is one calendar day of 4H candles).
DEFAULT_ENTRY_VALID_BARS = 6


class PaperExitReason(str, Enum):
    STOP = "stop"
    TRAILING_STOP = "trailing_stop"
    TAKE_PROFIT = "take_profit"
    MOMENTUM_SHIFT = "momentum_shift"


@dataclass(frozen=True)
class ClosedPaperTrade:
    """One fully-closed paper trade, recorded when the position returns to FLAT."""

    symbol: str
    side: PositionSide
    plan_id: str
    entry_price: float
    exit_price: float
    initial_stop: float
    take_profit: float
    quantity: float
    exit_reason: PaperExitReason
    realized_pnl: float
    entry_time: datetime
    exit_time: datetime


@dataclass
class _ManagedSymbol:
    """Mutable per-symbol bookkeeping. The frozen PositionState handed to generate_signal is
    rebuilt from this each bar; the manager threads the update, since generate_signal is pure."""

    symbol: str
    status: PositionStatus = PositionStatus.FLAT
    side: PositionSide | None = None
    plan: TradePlan | None = None
    entry_order_id: str | None = None
    intended_quantity: float = 0.0
    entry_price: float | None = None
    initial_stop: float | None = None
    take_profit: float | None = None
    current_stop: float | None = None
    stop_was_trailed: bool = False
    extreme_price: float | None = None
    zone_boundary: float | None = None
    entry_time: datetime | None = None
    pending_bars: int = 0
    entered_notified: bool = False
    traded_zones: list[float] = field(default_factory=list)
    frozen: bool = False

    def reset_to_flat(self) -> None:
        self.status = PositionStatus.FLAT
        self.side = None
        self.plan = None
        self.entry_order_id = None
        self.intended_quantity = 0.0
        self.entry_price = None
        self.initial_stop = None
        self.take_profit = None
        self.current_stop = None
        self.stop_was_trailed = False
        self.extreme_price = None
        self.zone_boundary = None
        self.entry_time = None
        self.pending_bars = 0
        self.entered_notified = False


class PositionManager:
    """Drives one venue: sizing, approval, submission, and the four-state lifecycle."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        approval: ApprovalChannel,
        *,
        strategy_config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
        risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
        slider: float = 1.0,
        entry_valid_bars: int = DEFAULT_ENTRY_VALID_BARS,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._adapter = adapter
        self._approval = approval
        self._strategy_config = strategy_config
        self._risk_config = risk_config
        self._slider = slider
        self._entry_valid_bars = entry_valid_bars
        # Build Order step 5 (AGENTS.md): None preserves every pre-step-5 call site's behaviour
        # (no gate at all). When supplied, a triggered (not-armed) kill switch halts signal
        # generation account-wide, the same "no new entries" obligation the per-symbol `frozen`
        # flag already enforces for a reconciliation drift.
        self._kill_switch = kill_switch
        self._symbols: dict[str, _ManagedSymbol] = {}
        # Audit trails for the Gate 2 critical-failure checks.
        self.closed_trades: list[ClosedPaperTrade] = []
        self.approvals: list[tuple[str, ApprovalVerdict]] = []
        self.submitted_order_plan_ids: list[str] = []
        self.voided_plans: list[TradePlan] = []

    # --------------------------------------------------------------------------- public API

    @property
    def approval(self) -> ApprovalChannel:
        """The approval/notification channel, so the loop can send reconciliation alerts."""
        return self._approval

    def managed(self, symbol: str) -> _ManagedSymbol:
        return self._symbols.setdefault(symbol, _ManagedSymbol(symbol=symbol))

    def set_frozen(self, symbol: str, frozen: bool) -> None:
        ms = self.managed(symbol)
        was = ms.frozen
        ms.frozen = frozen
        if frozen and not was:
            self._approval.notify(
                ChannelEvent(
                    kind=EventKind.SYMBOL_FROZEN,
                    symbol=symbol,
                    message=f"{symbol} frozen: reconciliation drift, refusing new entries",
                )
            )
        elif not frozen and was:
            self._approval.notify(
                ChannelEvent(
                    kind=EventKind.SYMBOL_RESUMED,
                    symbol=symbol,
                    message=f"{symbol} resumed after a clean reconciliation",
                )
            )

    def is_frozen(self, symbol: str) -> bool:
        return self.managed(symbol).frozen

    def expected_state(self, symbol: str) -> ExpectedPosition:
        """The manager's belief about `symbol`, for the reconciler to compare with truth."""
        ms = self.managed(symbol)
        has_exposure = ms.status in (PositionStatus.PARTIAL, PositionStatus.OPEN)
        return ExpectedPosition(
            symbol=symbol,
            side=ms.side if has_exposure else None,
            quantity=ms.intended_quantity if has_exposure else 0.0,
            entry_price=ms.entry_price if has_exposure else None,
            expects_resting_stop=has_exposure,
            stop_price=ms.current_stop if has_exposure else None,
        )

    def on_bar(
        self,
        symbol: str,
        bar_index: int,
        candle: Candle,
        h4_window: list[Candle],
        d1_window: list[Candle],
        sim_events: list[SimEvent],
    ) -> None:
        """Advance one symbol by one closed 4H bar. The adapter has already processed fills."""
        ms = self.managed(symbol)
        self._sync_from_exchange(ms, candle, sim_events)

        if ms.status is PositionStatus.FLAT:
            self._maybe_seek_entry(ms, candle, h4_window, d1_window, bar_index)
        elif ms.status is PositionStatus.PENDING:
            self._handle_pending(ms, candle)
        else:  # PARTIAL or OPEN
            self._manage_open(ms, candle, h4_window, d1_window)
            self._advance_extreme(ms, candle)

    # ---------------------------------------------------------------- lifecycle from truth

    def _sync_from_exchange(
        self, ms: _ManagedSymbol, candle: Candle, sim_events: list[SimEvent]
    ) -> None:
        positions = self._adapter.get_positions(ms.symbol)
        orders = self._adapter.get_open_orders(ms.symbol)
        position = positions[0] if positions else None
        entry_resting = any(
            o.order_id == ms.entry_order_id for o in orders
        ) if ms.entry_order_id else False

        if ms.status in (PositionStatus.PARTIAL, PositionStatus.OPEN) and position is None:
            # The exchange closed the position (a stop or take-profit fired this bar).
            self._record_exchange_close(ms, candle, sim_events)
            return

        if position is not None and ms.status in (
            PositionStatus.PENDING,
            PositionStatus.PARTIAL,
            PositionStatus.OPEN,
        ):
            # A fill (partial or full). Adopt exchange truth for entry price and quantity.
            ms.entry_price = position.entry_price
            if ms.entry_time is None:
                ms.entry_time = candle.open_time
            fraction = position.quantity / ms.intended_quantity if ms.intended_quantity else 1.0
            ms.status = PositionStatus.OPEN if fraction >= 1.0 - 1e-9 else PositionStatus.PARTIAL
            if ms.extreme_price is None:
                ms.extreme_price = position.entry_price
            if not ms.entered_notified:
                ms.entered_notified = True
                self._approval.notify(
                    ChannelEvent(
                        kind=EventKind.ENTRY_FILLED,
                        symbol=ms.symbol,
                        message=(
                            f"{ms.symbol} entry filled ({ms.status.value}) at "
                            f"{position.entry_price} qty {position.quantity}"
                        ),
                        detail={"filled_fraction": fraction},
                    )
                )
            return

        if ms.status is PositionStatus.PENDING and position is None and not entry_resting:
            # The resting entry left the book without a fill (cancelled/expired): back to FLAT.
            ms.reset_to_flat()

    def _record_exchange_close(
        self, ms: _ManagedSymbol, candle: Candle, sim_events: list[SimEvent]
    ) -> None:
        event = self._match_close_event(ms.symbol, sim_events)
        if event is not None and event.kind is SimEventKind.STOP_HIT:
            reason = (
                PaperExitReason.TRAILING_STOP if ms.stop_was_trailed else PaperExitReason.STOP
            )
            exit_price = event.price
            realized = event.realized_pnl if event.realized_pnl is not None else 0.0
        elif event is not None and event.kind is SimEventKind.TAKE_PROFIT_HIT:
            reason = PaperExitReason.TAKE_PROFIT
            exit_price = event.price
            realized = event.realized_pnl if event.realized_pnl is not None else 0.0
        else:
            reason = PaperExitReason.STOP
            exit_price = candle.close
            realized = 0.0
        self._finalize_close(ms, candle, exit_price, realized, reason)

    def _finalize_close(
        self,
        ms: _ManagedSymbol,
        candle: Candle,
        exit_price: float,
        realized_pnl: float,
        reason: PaperExitReason,
    ) -> None:
        if (
            ms.side is None
            or ms.entry_price is None
            or ms.initial_stop is None
            or ms.take_profit is None
            or ms.plan is None
        ):
            ms.reset_to_flat()
            return
        self.closed_trades.append(
            ClosedPaperTrade(
                symbol=ms.symbol,
                side=ms.side,
                plan_id=ms.plan.plan_id,
                entry_price=ms.entry_price,
                exit_price=exit_price,
                initial_stop=ms.initial_stop,
                take_profit=ms.take_profit,
                quantity=ms.intended_quantity,
                exit_reason=reason,
                realized_pnl=realized_pnl,
                entry_time=ms.entry_time or candle.open_time,
                exit_time=candle.close_time,
            )
        )
        # One zone, one trade: remember the boundary so a later retest is rejected even after FLAT.
        if ms.zone_boundary is not None:
            ms.traded_zones.append(ms.zone_boundary)
        self._approval.notify(
            ChannelEvent(
                kind=EventKind.POSITION_EXITED,
                symbol=ms.symbol,
                message=f"{ms.symbol} closed via {reason.value} at {exit_price}",
                detail={"realized_pnl": realized_pnl, "reason": reason.value},
            )
        )
        traded = ms.traded_zones
        ms.reset_to_flat()
        ms.traded_zones = traded

    @staticmethod
    def _match_close_event(symbol: str, sim_events: list[SimEvent]) -> SimEvent | None:
        for event in sim_events:
            if event.symbol == symbol and event.fully_closed:
                return event
        return None

    # ---------------------------------------------------------------------- FLAT: seek entry

    def _maybe_seek_entry(
        self,
        ms: _ManagedSymbol,
        candle: Candle,
        h4_window: list[Candle],
        d1_window: list[Candle],
        bar_index: int,
    ) -> None:
        if ms.frozen:
            return  # refuse new entries while frozen; existing position (none here) still managed
        if self._kill_switch is not None and not self._kill_switch.is_armed:
            return  # kill switch triggered: halts signal generation account-wide (PRD 9.1)
        state = PositionState.flat(traded_zones=tuple(ms.traded_zones))
        signal = generate_signal(h4_window, d1_window, state, self._strategy_config)
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return
        side = (
            PositionSide.LONG if signal.action is SignalAction.ENTER_LONG else PositionSide.SHORT
        )
        plan = self._size(ms.symbol, side, signal, slider=self._slider)
        if plan.is_void:
            self.voided_plans.append(plan)
            self._approval.notify(
                ChannelEvent(
                    kind=EventKind.PLAN_VOIDED,
                    symbol=ms.symbol,
                    message=f"{ms.symbol} plan voided: {plan.void_reason}",
                )
            )
            # Record the zone so a voided plan does not re-fire every bar on the same setup.
            if signal.zone_boundary is not None:
                ms.traded_zones.append(signal.zone_boundary)
            return
        approved = self._seek_approval(plan)
        if approved is None:
            return
        self._submit_entry(ms, approved, side, signal)

    def _size(
        self, symbol: str, side: PositionSide, signal, slider: float
    ) -> TradePlan:
        balance = self._adapter.get_balance()
        rule = self._adapter.get_symbol_rule(symbol)
        return size_trade_plan(
            symbol=symbol,
            side=side,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            equity=balance.total,
            symbol_rule=rule,
            slider=slider,
            config=self._risk_config,
        )

    def _seek_approval(self, plan: TradePlan) -> TradePlan | None:
        """Request approval, honouring one MODIFY round. Returns the APPROVED plan, or None.

        Fails closed: any non-APPROVE terminal decision drops the plan. A MODIFY re-sizes once
        with the new slider and asks again; a second non-approve ends it (no infinite loop).
        """
        current = plan
        for _ in range(2):  # at most the original plus one modify round
            decision = self._approval.request_approval(current)
            self.approvals.append((current.plan_id, decision.verdict))
            if decision.verdict is ApprovalVerdict.APPROVE:
                return current.approved()
            if decision.verdict is ApprovalVerdict.REJECT:
                self._approval.notify(
                    ChannelEvent(
                        kind=EventKind.PLAN_REJECTED,
                        symbol=current.symbol,
                        message=f"{current.symbol} plan {current.plan_id} rejected",
                    )
                )
                return None
            # MODIFY: re-size with the supplied slider, then loop once more.
            new_slider = (
                decision.modified_slider
                if decision.modified_slider is not None
                else self._slider
            )
            resized = self._size(
                current.symbol, current.side,
                _signal_view(current), slider=new_slider,
            )
            if resized.is_void:
                self.voided_plans.append(resized)
                self._approval.notify(
                    ChannelEvent(
                        kind=EventKind.PLAN_VOIDED,
                        symbol=current.symbol,
                        message=f"{current.symbol} modified plan voided: {resized.void_reason}",
                    )
                )
                return None
            current = resized
        return None

    def _submit_entry(
        self, ms: _ManagedSymbol, plan: TradePlan, side: PositionSide, signal
    ) -> None:
        rule = self._adapter.get_symbol_rule(ms.symbol)
        order_side = OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL
        request = OrderRequest(
            symbol=ms.symbol,
            side=order_side,
            quantity=plan.quantity,
            price=rule.round_price(plan.entry_price),
            time_in_force=TimeInForce.POST_ONLY,
            reduce_only=False,
            stop_loss_price=rule.round_price(plan.stop_price),
            take_profit_price=rule.round_price(plan.take_profit_price),
        )
        order = self._adapter.place_limit_order(request)
        self.submitted_order_plan_ids.append(plan.plan_id)
        self._approval.notify(
            ChannelEvent(
                kind=EventKind.ORDER_SUBMITTED,
                symbol=ms.symbol,
                message=f"{ms.symbol} entry order {order.order_id} placed ({plan.risk_line})",
            )
        )
        ms.status = PositionStatus.PENDING
        ms.side = side
        ms.plan = plan.submitted()
        ms.entry_order_id = order.order_id
        ms.intended_quantity = plan.quantity
        ms.initial_stop = request.stop_loss_price
        ms.take_profit = request.take_profit_price
        ms.current_stop = request.stop_loss_price
        ms.zone_boundary = signal.zone_boundary
        ms.pending_bars = 0

    # ------------------------------------------------------------------------- PENDING

    def _handle_pending(self, ms: _ManagedSymbol, candle: Candle) -> None:
        ms.pending_bars += 1
        if ms.pending_bars > self._entry_valid_bars and ms.entry_order_id is not None:
            try:
                self._adapter.cancel_order(ms.symbol, ms.entry_order_id)
            except KeyError:
                pass  # already filled or gone; the next sync will settle the state
            # Zone stays recorded as attempted so a voided/expired setup does not re-fire.
            if ms.zone_boundary is not None:
                ms.traded_zones.append(ms.zone_boundary)
            traded = ms.traded_zones
            ms.reset_to_flat()
            ms.traded_zones = traded

    # --------------------------------------------------------------------- PARTIAL / OPEN

    def _manage_open(
        self,
        ms: _ManagedSymbol,
        candle: Candle,
        h4_window: list[Candle],
        d1_window: list[Candle],
    ) -> None:
        state = self._open_state(ms)
        signal = generate_signal(h4_window, d1_window, state, self._strategy_config)
        if signal.action is SignalAction.EXIT_MOMENTUM_SHIFT:
            self._exit_at_market(ms, candle, PaperExitReason.MOMENTUM_SHIFT)
            return
        if signal.action is SignalAction.UPDATE_TRAILING_STOP and signal.stop_price is not None:
            self._retighten_stop(ms, signal.stop_price)

    def _open_state(self, ms: _ManagedSymbol) -> PositionState:
        fraction = 1.0 if ms.status is PositionStatus.OPEN else 0.5
        return PositionState(
            status=ms.status,
            side=ms.side,
            filled_fraction=fraction,
            entry_price=ms.entry_price,
            initial_stop=ms.initial_stop,
            take_profit=ms.take_profit,
            current_stop=ms.current_stop,
            extreme_price=ms.extreme_price,
        )

    def _retighten_stop(self, ms: _ManagedSymbol, new_stop: float) -> None:
        """Emulate a trailing stop by cancel-and-replace (native_trailing_stop is False)."""
        rule = self._adapter.get_symbol_rule(ms.symbol)
        rounded = rule.round_price(new_stop)
        exit_side = OrderSide.SELL if ms.side is PositionSide.LONG else OrderSide.BUY
        # Cancel the current resting stop, then rest the tighter one, so a native stop always
        # protects the position (there is never a window with no stop resting for long).
        current = self._resting_stop_order_id(ms.symbol)
        self._adapter.place_stop_order(
            StopOrderRequest(
                symbol=ms.symbol,
                side=exit_side,
                quantity=ms.intended_quantity,
                stop_price=rounded,
                reduce_only=True,
            )
        )
        if current is not None:
            try:
                self._adapter.cancel_order(ms.symbol, current)
            except KeyError:
                pass
        ms.current_stop = rounded
        ms.stop_was_trailed = True

    def _resting_stop_order_id(self, symbol: str) -> str | None:
        for order in self._adapter.get_open_orders(symbol):
            if order.reduce_only and order.stop_price is not None:
                return order.order_id
        return None

    def _exit_at_market(
        self, ms: _ManagedSymbol, candle: Candle, reason: PaperExitReason
    ) -> None:
        exit_side = OrderSide.SELL if ms.side is PositionSide.LONG else OrderSide.BUY
        positions = self._adapter.get_positions(ms.symbol)
        if not positions:
            return
        quantity = positions[0].quantity
        equity_before = self._adapter.get_balance().total
        order = self._adapter.place_market_order(
            MarketOrderRequest(
                symbol=ms.symbol, side=exit_side, quantity=quantity, reduce_only=True
            )
        )
        realized = self._adapter.get_balance().total - equity_before
        exit_price = order.price if order.price is not None else candle.close
        self._finalize_close(ms, candle, exit_price, realized, reason)

    def _advance_extreme(self, ms: _ManagedSymbol, candle: Candle) -> None:
        if ms.side is PositionSide.LONG:
            ms.extreme_price = max(ms.extreme_price or candle.high, candle.high)
        elif ms.side is PositionSide.SHORT:
            ms.extreme_price = min(ms.extreme_price or candle.low, candle.low)


def _signal_view(plan: TradePlan):
    """A minimal object exposing the entry/stop/tp a re-size needs, from an existing plan."""

    class _View:
        entry_price = plan.entry_price
        stop_price = plan.stop_price
        take_profit_price = plan.take_profit_price
        zone_boundary = None

    return _View()
