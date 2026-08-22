"""DryRun (paper) ExchangeAdapter: honest fills against real candle data.

This is the paper venue Gate 2 runs against (PRD 6.2, "proves the machine, not the edge").
It implements the full ExchangeAdapter contract and simulates the exchange by advancing one
CLOSED candle at a time through `on_candle`. It is deliberately faithful to the specific
Bitunix behaviour the characterization spike confirmed (AGENTS.md), not to an idealized
exchange, so the branches Gate 2 exercises are the same ones the live Bitunix adapter
(step 6) will hit:

Honest fills (the load-bearing rules; each avoids one of PRD 6.2's enumerated critical
failures):

1. A resting limit fills only when a SUBSEQUENT candle's range actually trades through it,
   never on the candle during which it was placed. An order carries the clock value at
   placement and is eligible only on a strictly later candle. This is what prevents "a fill
   modeled as filled when the market did not trade through it."

2. A stop ALWAYS slips: it fills at the trigger price moved adversely by the shared cost
   model's slippage_rate (crypto_trader.backtest.costs.CostConfig, so paper and backtest use
   ONE slippage number, never two inconsistent ones), never at the exact stop price. A
   take-profit is a resting maker limit and takes no slippage.

3. Worst-of-intrabar ordering: 4H/daily candles cannot resolve the true within-bar path
   (AGENTS.md "Data plan"), so when a single candle's range contains BOTH the protective stop
   and the take-profit, the STOP is assumed to fill first (the worse outcome). On the candle
   that fills an entry, only a same-bar stop breach can close it (never a same-bar take-profit
   win), matching the backtest engine's conservatism.

4. Native stops rest server-side (capabilities.native_stop_market is True), so a protective
   stop is a tracked resting order the instant the entry fills, satisfying "on-exchange stops
   are the floor" (principle 4) in the paper venue. Trailing is emulated by cancel-and-replace
   (capabilities.native_trailing_stop is False), exactly as the live adapter must, because no
   native trailing stop exists on Bitunix.

`place_market_order` fills immediately at the current mark (the last seen candle's close)
with taker fee and adverse slippage: it is the momentum-shift exit and the step-5 kill-switch
flatten. A market order does not rest, so the "subsequent candle" rule does not apply to it.

This adapter touches no network and no exchange credentials: it is pure simulation over
candle data supplied by the caller.

`simulate_outage` and `consecutive_api_failures` (Build Order step 5, AGENTS.md) exist so the
kill switch's "5 consecutive API failures" auto-trigger and its degraded-mode flatten-retry
fallback can be exercised without a live venue: every method that represents a real venue round
trip calls the shared `_guard()` helper, which raises ExchangeConnectionError while a simulated
outage is in effect and otherwise resets the tracked failure count.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from crypto_trader.backtest.costs import CostConfig
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
    RateLimitStatus,
    StopOrderRequest,
    SymbolRule,
)
from crypto_trader.ingest.models import Candle

# Per-symbol trading rules approximating the public trading-pairs data observed during the
# characterization spike (AGENTS.md). Provisional, like every spike figure; overridable via
# DryRunConfig.symbol_rules. BTCUSDT matches the value the adapter spike's test pinned.
DEFAULT_SYMBOL_RULES: dict[str, SymbolRule] = {
    "BTCUSDT": SymbolRule(
        symbol="BTCUSDT", base_precision=4, quote_precision=1, min_trade_volume=0.0001
    ),
    "ETHUSDT": SymbolRule(
        symbol="ETHUSDT", base_precision=3, quote_precision=2, min_trade_volume=0.001
    ),
}


class _Role(str, Enum):
    """Internal classification of a resting order, inferred at placement."""

    ENTRY = "entry"  # a non-reduce-only limit: the first-touch opener
    STOP = "stop"  # a native reduce-only stop (the protective floor)
    TAKE_PROFIT = "take_profit"  # a reduce-only limit exit


class SimEventKind(str, Enum):
    """What happened to a position during a simulation step, for the caller to record."""

    ENTRY_FILL = "entry_fill"  # an entry limit filled (partially or fully)
    STOP_HIT = "stop_hit"  # the protective stop triggered and closed the position
    TAKE_PROFIT_HIT = "take_profit_hit"  # the take-profit limit filled and closed
    MARKET_CLOSE = "market_close"  # a reduce-only market order closed/reduced the position


@dataclass(frozen=True)
class SimEvent:
    """A fill or exit the adapter produced, reported so the caller need not diff state.

    `fully_closed` is True when the event flattened the position. `realized_pnl` is set for
    closing events. `filled_fraction` is the position's filled fraction after an entry fill
    (so the caller can distinguish a PARTIAL from an OPEN without re-deriving it).
    """

    kind: SimEventKind
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    order_id: str
    is_maker: bool
    fully_closed: bool = False
    realized_pnl: float | None = None
    filled_fraction: float | None = None


@dataclass
class _SimOrder:
    order_id: str
    symbol: str
    side: OrderSide
    role: _Role
    quantity: float
    filled_quantity: float
    status: OrderStatus
    placed_clock: int
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    # Attached bracket legs for an ENTRY, materialized as real STOP/TAKE_PROFIT orders on fill.
    attached_stop_price: float | None = None
    attached_take_profit_price: float | None = None

    def to_public(self) -> Order:
        order_type = OrderType.MARKET if self.role is _Role.STOP else OrderType.LIMIT
        return Order(
            order_id=self.order_id,
            symbol=self.symbol,
            side=self.side,
            order_type=order_type,
            quantity=self.quantity,
            filled_quantity=self.filled_quantity,
            status=self.status,
            price=self.price,
            stop_price=self.stop_price,
            reduce_only=self.reduce_only,
            client_order_id=self.client_order_id,
        )


@dataclass
class _SimPosition:
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    intended_quantity: float  # the entry order's full size, so PARTIAL vs OPEN is knowable

    @property
    def filled_fraction(self) -> float:
        if self.intended_quantity <= 0.0:
            return 0.0
        return min(1.0, self.quantity / self.intended_quantity)


@dataclass(frozen=True)
class DryRunConfig:
    """Paper-venue parameters. The cost model is SHARED with the backtest (one slippage number).

    `partial_fill_ratio`, when set in (0, 1), makes the FIRST trade-through of an entry limit
    fill only that fraction (a PARTIAL), with the remainder filling on a later candle that
    trades through again. It is off by default and is an explicit paper affordance to exercise
    the four-state position contract end to end: faithful partial-fill microstructure needs
    order-book depth the 4H/daily data plan does not carry (AGENTS.md "Data plan"), so this is
    a deterministic stand-in, never a claim about real fill sizes.
    """

    cost: CostConfig = field(default_factory=CostConfig)
    starting_equity: float = 2000.0
    currency: str = "USDT"
    capabilities: ExchangeCapabilities = field(default_factory=ExchangeCapabilities)
    symbol_rules: dict[str, SymbolRule] = field(default_factory=lambda: dict(DEFAULT_SYMBOL_RULES))
    partial_fill_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.starting_equity <= 0.0:
            raise ValueError("starting_equity must be > 0")
        if self.partial_fill_ratio is not None and not 0.0 < self.partial_fill_ratio < 1.0:
            raise ValueError("partial_fill_ratio must be in (0, 1) when set")


class DryRunExchangeAdapter(ExchangeAdapter):
    """A paper exchange that fills honestly against candle data advanced via on_candle."""

    def __init__(self, config: DryRunConfig | None = None) -> None:
        self._config = config or DryRunConfig()
        self._cash = self._config.starting_equity
        self._orders: dict[str, _SimOrder] = {}
        self._positions: dict[str, _SimPosition] = {}
        self._marks: dict[str, float] = {}
        # Monotonic clock: the open_time (epoch ms) of the most recent candle seen, so the
        # "fills only on a subsequent candle" rule has a total order to compare against.
        self._clock: int = -1
        self._ids = itertools.count(1)
        # Kill-switch support (Build Order step 5, AGENTS.md): DryRun has no real network, so a
        # transport failure is injected explicitly via simulate_outage rather than occurring
        # spontaneously. _consecutive_failures mirrors what the live adapter (step 6) would track
        # from real transport failures.
        self._simulated_outage_calls = 0
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ ExchangeAdapter API

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return self._config.capabilities

    def get_symbol_rule(self, symbol: str) -> SymbolRule:
        try:
            return self._config.symbol_rules[symbol]
        except KeyError:
            raise KeyError(
                f"no symbol rule configured for {symbol!r} in the DryRun adapter"
            ) from None

    def simulate_outage(self, calls: int) -> None:
        """Test/simulation hook: makes the next `calls` guarded adapter calls raise
        ExchangeConnectionError instead of executing.

        DryRun has no real network, so a transport failure must be injected explicitly to
        exercise the kill switch's "5 consecutive API failures" auto-trigger and its
        degraded-mode flatten-retry fallback (PRD 9.1, AGENTS.md) without a live venue. Each
        simulated failure also increments consecutive_api_failures(); any guarded call that
        succeeds resets it to 0.
        """
        self._simulated_outage_calls = calls

    def consecutive_api_failures(self) -> int:
        return self._consecutive_failures

    def _guard(self) -> None:
        """Raise ExchangeConnectionError while a simulated outage is in effect, else record
        success. Called at the top of every method that represents a real venue round trip."""
        if self._simulated_outage_calls > 0:
            self._simulated_outage_calls -= 1
            self._consecutive_failures += 1
            raise ExchangeConnectionError(
                "simulated exchange outage (DryRunExchangeAdapter.simulate_outage)"
            )
        self._consecutive_failures = 0

    def place_limit_order(self, request: OrderRequest) -> Order:
        """Rest a limit order. A non-reduce-only limit is the entry; reduce-only is a TP.

        An entry may carry attached stop_loss_price/take_profit_price (Bitunix's native
        atomic bracket); those become real resting STOP/TAKE_PROFIT orders the instant the
        entry fills, so the protective stop rests server-side without a second round trip.
        """
        self._guard()
        role = _Role.TAKE_PROFIT if request.reduce_only else _Role.ENTRY
        order = _SimOrder(
            order_id=self._new_id(),
            symbol=request.symbol,
            side=request.side,
            role=role,
            quantity=request.quantity,
            filled_quantity=0.0,
            status=OrderStatus.NEW,
            placed_clock=self._clock,
            price=request.price,
            reduce_only=request.reduce_only,
            client_order_id=request.client_order_id,
            attached_stop_price=request.stop_loss_price,
            attached_take_profit_price=request.take_profit_price,
        )
        self._orders[order.order_id] = order
        return order.to_public()

    def place_stop_order(self, request: StopOrderRequest) -> Order:
        """Rest a native server-side stop (a MARKET gated on the trigger). The safety floor."""
        self._guard()
        order = _SimOrder(
            order_id=self._new_id(),
            symbol=request.symbol,
            side=request.side,
            role=_Role.STOP,
            quantity=request.quantity,
            filled_quantity=0.0,
            status=OrderStatus.NEW,
            placed_clock=self._clock,
            stop_price=request.stop_price,
            reduce_only=request.reduce_only,
            client_order_id=request.client_order_id,
        )
        self._orders[order.order_id] = order
        return order.to_public()

    def place_market_order(self, request: MarketOrderRequest) -> Order:
        """Fill a reduce-only market order immediately at the current mark, with slippage.

        Used for the momentum-shift exit and (step 5) the kill-switch flatten. Does not rest,
        so the subsequent-candle rule does not apply. Raises if there is no mark yet (no candle
        seen) or no position to reduce, because a paper market order must be honest about not
        being fillable rather than inventing a price.
        """
        self._guard()
        symbol = request.symbol
        mark = self._marks.get(symbol)
        if mark is None:
            raise RuntimeError(f"cannot fill a market order for {symbol}: no mark price yet")
        position = self._positions.get(symbol)
        if position is None:
            raise RuntimeError(f"cannot fill a reduce-only market order for {symbol}: flat")

        fill_qty = min(request.quantity, position.quantity)
        fill_price = self._slipped_exit_price(request.side, mark)
        realized = self._realized_pnl(position.side, position.entry_price, fill_price, fill_qty)
        fee = fill_price * fill_qty * self._config.cost.taker_fee_rate
        self._cash += realized - fee
        self._reduce_position(symbol, fill_qty)
        order_id = self._new_id()
        return Order(
            order_id=order_id,
            symbol=symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            quantity=request.quantity,
            filled_quantity=fill_qty,
            status=OrderStatus.FILLED,
            price=fill_price,
            reduce_only=request.reduce_only,
            client_order_id=request.client_order_id,
        )

    def cancel_order(self, symbol: str, order_id: str) -> Order:
        self._guard()
        order = self._orders.get(order_id)
        if order is None or order.symbol != symbol:
            raise KeyError(f"no open order {order_id!r} for {symbol}")
        del self._orders[order_id]
        order.status = OrderStatus.CANCELED
        return order.to_public()

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._guard()
        return [
            o.to_public()
            for o in self._orders.values()
            if symbol is None or o.symbol == symbol
        ]

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        self._guard()
        out: list[Position] = []
        for pos in self._positions.values():
            if symbol is not None and pos.symbol != symbol:
                continue
            out.append(
                Position(
                    symbol=pos.symbol,
                    side=pos.side,
                    quantity=pos.quantity,
                    entry_price=pos.entry_price,
                    position_mode=self._config.capabilities.position_mode,
                    margin_mode=None,
                    unrealized_pnl=self._unrealized(pos),
                )
            )
        return out

    def get_balance(self) -> Balance:
        self._guard()
        unrealized = sum(self._unrealized(p) for p in self._positions.values())
        return Balance(
            currency=self._config.currency,
            total=self._cash + unrealized,
            available=self._cash,
            unrealized_pnl=unrealized,
        )

    def rate_limit_status(self) -> RateLimitStatus:
        """A paper venue is not meaningfully rate-limited; report a permanently-healthy budget.

        The method exists on the interface for the live adapter's sake (Bitunix returns no
        quota headers, so the live adapter tracks its own budget); DryRun always has headroom.
        """
        return RateLimitStatus(
            scope="paper", limit_per_window=10, used_in_window=0, window_seconds=1.0
        )

    # ------------------------------------------------------------------ simulation driver

    def on_candle(self, candle: Candle) -> list[SimEvent]:
        """Advance the exchange clock to `candle` and process resting orders against its range.

        Returns the fills/exits that occurred, oldest first. Existing positions' stops and
        take-profits are checked first (they rest from prior bars), then resting entry limits;
        a stop always wins a same-bar tie with a take-profit (worst-of-intrabar).
        """
        self._clock = candle.open_time_ms
        self._marks[candle.symbol] = candle.close
        events: list[SimEvent] = []

        # 1) Manage an existing position's protective legs against this candle.
        position = self._positions.get(candle.symbol)
        if position is not None:
            exit_event = self._check_protective_exit(position, candle)
            if exit_event is not None:
                events.append(exit_event)

        # 2) Try to fill resting entry limits that are now eligible (placed on an earlier bar).
        for order in self._eligible_entries(candle):
            fill_event = self._try_fill_entry(order, candle)
            if fill_event is not None:
                events.append(fill_event)
                # Worst-of-intrabar on the fill bar: only a same-bar stop breach can close it.
                pos = self._positions.get(candle.symbol)
                if pos is not None:
                    breach = self._same_bar_stop_breach(pos, candle)
                    if breach is not None:
                        events.append(breach)
        return events

    # ------------------------------------------------------------------ internals

    def _new_id(self) -> str:
        return f"dry-{next(self._ids):08d}"

    def _eligible_entries(self, candle: Candle) -> list[_SimOrder]:
        # Strictly-later rule: an order placed at clock c can fill only on a candle whose
        # open_time is > c. Orders placed while flat (clock -1) are eligible on any candle.
        return [
            o
            for o in list(self._orders.values())
            if o.role is _Role.ENTRY
            and o.symbol == candle.symbol
            and o.placed_clock < candle.open_time_ms
        ]

    def _try_fill_entry(self, order: _SimOrder, candle: Candle) -> SimEvent | None:
        if order.price is None or not self._trades_through_limit(order.side, order.price, candle):
            return None
        rule = self.get_symbol_rule(order.symbol)
        remaining = order.quantity - order.filled_quantity
        fill_qty = self._entry_fill_quantity(order, remaining, rule)
        if fill_qty <= 0.0:
            return None

        order.filled_quantity += fill_qty
        fully = order.filled_quantity >= order.quantity - 1e-12
        order.status = OrderStatus.FILLED if fully else OrderStatus.PARTIALLY_FILLED

        self._apply_entry_fill(order, fill_qty)
        position = self._positions[order.symbol]
        self._sync_bracket_legs(order, position)

        if fully:
            del self._orders[order.order_id]
        return SimEvent(
            kind=SimEventKind.ENTRY_FILL,
            symbol=order.symbol,
            side=order.side,
            price=order.price,
            quantity=fill_qty,
            order_id=order.order_id,
            is_maker=True,
            filled_fraction=position.filled_fraction,
        )

    def _entry_fill_quantity(self, order: _SimOrder, remaining: float, rule: SymbolRule) -> float:
        ratio = self._config.partial_fill_ratio
        if ratio is not None and order.filled_quantity <= 0.0:
            partial = rule.round_quantity(order.quantity * ratio)
            if 0.0 < partial < remaining:
                return partial
        return remaining

    def _apply_entry_fill(self, order: _SimOrder, fill_qty: float) -> None:
        fee = order.price * fill_qty * self._config.cost.maker_fee_rate
        self._cash -= fee
        existing = self._positions.get(order.symbol)
        if existing is None:
            self._positions[order.symbol] = _SimPosition(
                symbol=order.symbol,
                side=order.side,
                quantity=fill_qty,
                entry_price=order.price,
                intended_quantity=order.quantity,
            )
        else:
            # Average in the additional fill (same side; the manager never flips via an entry).
            total = existing.quantity + fill_qty
            existing.entry_price = (
                existing.entry_price * existing.quantity + order.price * fill_qty
            ) / total
            existing.quantity = total

    def _sync_bracket_legs(self, entry: _SimOrder, position: _SimPosition) -> None:
        """Create or resize the attached stop/take-profit to cover the filled quantity.

        Mirrors Bitunix's native attached TP/SL: the protective legs rest server-side as soon
        as the entry has exposure. On a partial fill the legs cover only the filled quantity
        and grow as more fills land, so the stop always protects exactly the live exposure.
        """
        exit_side = _opposite(position.side)
        if entry.attached_stop_price is not None:
            self._upsert_protective(
                position.symbol, exit_side, _Role.STOP, position.quantity,
                stop_price=entry.attached_stop_price,
            )
        if entry.attached_take_profit_price is not None:
            self._upsert_protective(
                position.symbol, exit_side, _Role.TAKE_PROFIT, position.quantity,
                price=entry.attached_take_profit_price,
            )

    def _upsert_protective(
        self,
        symbol: str,
        side: OrderSide,
        role: _Role,
        quantity: float,
        *,
        price: float | None = None,
        stop_price: float | None = None,
    ) -> None:
        for order in self._orders.values():
            if order.symbol == symbol and order.role is role and order.reduce_only:
                order.quantity = quantity
                if price is not None:
                    order.price = price
                if stop_price is not None:
                    order.stop_price = stop_price
                return
        order_id = self._new_id()
        self._orders[order_id] = _SimOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            role=role,
            quantity=quantity,
            filled_quantity=0.0,
            status=OrderStatus.NEW,
            placed_clock=self._clock,
            price=price,
            stop_price=stop_price,
            reduce_only=True,
        )

    def _check_protective_exit(self, position: _SimPosition, candle: Candle) -> SimEvent | None:
        stop = self._protective_order(position.symbol, _Role.STOP)
        tp = self._protective_order(position.symbol, _Role.TAKE_PROFIT)
        stop_hit = stop is not None and self._stop_triggered(
            position.side, stop.stop_price, candle
        )
        tp_hit = tp is not None and self._trades_through_limit(
            _opposite(position.side), tp.price, candle
        )

        if stop_hit:  # worst-of-intrabar: the stop wins a tie with the take-profit
            return self._close_via_stop(position, stop, candle)
        if tp_hit:
            return self._close_via_take_profit(position, tp, candle)
        return None

    def _same_bar_stop_breach(self, position: _SimPosition, candle: Candle) -> SimEvent | None:
        stop = self._protective_order(position.symbol, _Role.STOP)
        if stop is None or not self._stop_triggered(position.side, stop.stop_price, candle):
            return None
        return self._close_via_stop(position, stop, candle)

    def _close_via_stop(self, position: _SimPosition, stop: _SimOrder, candle: Candle) -> SimEvent:
        exit_side = _opposite(position.side)
        fill_price = self._slipped_exit_price(exit_side, stop.stop_price)
        qty = position.quantity
        realized = self._realized_pnl(position.side, position.entry_price, fill_price, qty)
        fee = fill_price * qty * self._config.cost.taker_fee_rate
        self._cash += realized - fee
        self._flatten(position.symbol)
        return SimEvent(
            kind=SimEventKind.STOP_HIT,
            symbol=position.symbol,
            side=exit_side,
            price=fill_price,
            quantity=qty,
            order_id=stop.order_id,
            is_maker=False,
            fully_closed=True,
            realized_pnl=realized - fee,
        )

    def _close_via_take_profit(
        self, position: _SimPosition, tp: _SimOrder, candle: Candle
    ) -> SimEvent:
        exit_side = _opposite(position.side)
        fill_price = tp.price  # a resting maker limit fills at its level, no slippage
        qty = position.quantity
        realized = self._realized_pnl(position.side, position.entry_price, fill_price, qty)
        fee = fill_price * qty * self._config.cost.maker_fee_rate
        self._cash += realized - fee
        self._flatten(position.symbol)
        return SimEvent(
            kind=SimEventKind.TAKE_PROFIT_HIT,
            symbol=position.symbol,
            side=exit_side,
            price=fill_price,
            quantity=qty,
            order_id=tp.order_id,
            is_maker=True,
            fully_closed=True,
            realized_pnl=realized - fee,
        )

    def _protective_order(self, symbol: str, role: _Role) -> _SimOrder | None:
        for order in self._orders.values():
            if order.symbol == symbol and order.role is role and order.reduce_only:
                return order
        return None

    def _reduce_position(self, symbol: str, quantity: float) -> None:
        position = self._positions[symbol]
        remaining = position.quantity - quantity
        if remaining <= 1e-12:
            self._flatten(symbol)
        else:
            position.quantity = remaining

    def _flatten(self, symbol: str) -> None:
        """Remove the position and cancel every now-orphaned resting order for it.

        This includes the ENTRY order, not only the protective STOP/TAKE_PROFIT legs: a
        partially-filled entry (see DryRunConfig.partial_fill_ratio) can still be resting when
        the position closes, and a flat position has no working order that makes sense to
        leave open. Leaving it resting would let a later trade-through refill it into a
        phantom, unapproved position (see AGENTS.md paper-loop notes).
        """
        self._positions.pop(symbol, None)
        for order_id in [oid for oid, o in self._orders.items() if o.symbol == symbol]:
            del self._orders[order_id]

    def _unrealized(self, position: _SimPosition) -> float:
        mark = self._marks.get(position.symbol, position.entry_price)
        return self._realized_pnl(position.side, position.entry_price, mark, position.quantity)

    def _slipped_exit_price(self, exit_side: OrderSide, reference: float) -> float:
        """Adverse slippage on a taker exit: a sell fills lower, a buy fills higher."""
        slip = reference * self._config.cost.slippage_rate
        return reference - slip if exit_side is OrderSide.SELL else reference + slip

    @staticmethod
    def _realized_pnl(
        position_side: OrderSide, entry: float, exit_price: float, qty: float
    ) -> float:
        direction = 1.0 if position_side is OrderSide.BUY else -1.0
        return (exit_price - entry) * qty * direction

    @staticmethod
    def _trades_through_limit(side: OrderSide, price: float | None, candle: Candle) -> bool:
        """A resting limit fills when the candle trades through it. Side is the LIMIT's side."""
        if price is None:
            return False
        if side is OrderSide.BUY:
            return candle.low <= price
        return candle.high >= price

    @staticmethod
    def _stop_triggered(position_side: OrderSide, stop_price: float | None, candle: Candle) -> bool:
        if stop_price is None:
            return False
        if position_side is OrderSide.BUY:  # long: protective stop is below, sells on a drop
            return candle.low <= stop_price
        return candle.high >= stop_price  # short: protective stop is above, buys on a rally


def _opposite(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY
