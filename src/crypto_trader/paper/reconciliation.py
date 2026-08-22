"""Reconciliation: keep the bot's model of its own state matched to exchange truth.

Governing principle 1 (the exchange is the single source of truth) and PRD 9.2: the bot never
trusts its own memory of a position over what the exchange reports. Every reconciliation
cycle compares the position manager's EXPECTED state against the adapter's REPORTED state and,
on a drift beyond tolerance, FREEZES the affected symbol (refuse new entries, keep managing any
existing position conservatively), alerts, and requires a clean reconciliation before resuming
that symbol. Undetected reconciliation drift is one of Gate 2's enumerated critical failures
(PRD 6.2), so this check is the machine Gate 2 most directly proves.

The tolerance definition (the design-review requirement, PRD 9.2): a naive equality check
false-positives against ordinary funding and fee accrual between cycles, so "clean match" is
defined numerically, in three parts, all overridable on ReconciliationConfig:

1. Size epsilon, at the symbol's own precision. Two positions match in size when they differ by
   no more than `size_tolerance_lots` lots, where one lot is 10**-base_precision from the
   SymbolRule. Default 1 lot: paper fills are exact, so anything past a lot is genuine drift, but
   one lot of rounding slack keeps precision arithmetic from false-positiving.

2. Price rounding, at the symbol's own tick. Entry and stop prices match when they differ by no
   more than `price_tolerance_ticks` ticks, where one tick is 10**-quote_precision. Default 1
   tick, because a re-placed (trailed) stop is rounded to the tick and may not equal the stored
   float exactly.

3. Funding/fee accrual allowance, on MONETARY comparisons only (equity), never on size. Funding
   and fees accrue on equity between cycles, not on the base quantity, so equity is compared with
   a fractional allowance `equity_accrual_fraction` of the position notional (default 0.5%), which
   comfortably covers a cycle's worth of funding (order 0.01-0.05% per 8h) and fees (order
   0.02-0.06% per side) while staying far below the drift a wrong position size would produce. Size
   and side are compared with NO monetary allowance, because funding never changes them: a size
   drift is always real.

Presence of the protective stop is a hard boolean check, not a tolerance: if the manager expects
exposure, a reduce-only stop MUST be resting on the exchange, or that is a "stop failed to rest"
critical failure (PRD 6.2) and the symbol freezes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crypto_trader.exchange.types import (
    Balance,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    SymbolRule,
)
from crypto_trader.strategy.position import PositionSide


@dataclass(frozen=True)
class ReconciliationConfig:
    """Numeric tolerances for a clean match. See the module docstring for the rationale."""

    size_tolerance_lots: float = 1.0
    price_tolerance_ticks: float = 1.0
    equity_accrual_fraction: float = 0.005

    def __post_init__(self) -> None:
        for name in ("size_tolerance_lots", "price_tolerance_ticks", "equity_accrual_fraction"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be >= 0")


DEFAULT_RECONCILIATION_CONFIG = ReconciliationConfig()


@dataclass(frozen=True)
class ExpectedPosition:
    """What the position manager believes it holds for one symbol.

    `expects_resting_stop` is True whenever there is exposure the on-exchange stop must be
    protecting. `expected_equity` is optional: when the manager supplies its last-known equity,
    the reconciler applies the accrual allowance to it; when omitted, the monetary check is
    skipped (the exchange remains the source of truth for equity either way).
    """

    symbol: str
    side: PositionSide | None  # None means the manager expects to be FLAT
    quantity: float
    entry_price: float | None
    expects_resting_stop: bool
    stop_price: float | None = None
    expected_equity: float | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of one reconciliation cycle for one symbol."""

    symbol: str
    is_clean: bool
    drifts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if self.is_clean:
            return f"{self.symbol}: clean"
        return f"{self.symbol}: DRIFT - " + "; ".join(self.drifts)


def _position_for(symbol: str, positions: list[Position]) -> Position | None:
    for pos in positions:
        if pos.symbol == symbol:
            return pos
    return None


def _has_resting_stop(symbol: str, orders: list[Order]) -> bool:
    for order in orders:
        if (
            order.symbol == symbol
            and order.reduce_only
            and order.stop_price is not None
            and order.status
            in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.TRIGGERED)
        ):
            return True
    return False


def _resting_stop_price(symbol: str, orders: list[Order]) -> float | None:
    for order in orders:
        if order.symbol == symbol and order.reduce_only and order.stop_price is not None:
            return order.stop_price
    return None


def _side_of(position: Position) -> PositionSide:
    return PositionSide.LONG if position.side is OrderSide.BUY else PositionSide.SHORT


def reconcile(
    expected: ExpectedPosition,
    reported_positions: list[Position],
    reported_orders: list[Order],
    symbol_rule: SymbolRule,
    reported_balance: Balance | None = None,
    config: ReconciliationConfig = DEFAULT_RECONCILIATION_CONFIG,
) -> ReconciliationResult:
    """Compare expected vs reported state for one symbol; report every drift beyond tolerance."""
    drifts: list[str] = []
    symbol = expected.symbol
    reported = _position_for(symbol, reported_positions)

    size_epsilon = config.size_tolerance_lots * (10 ** -symbol_rule.base_precision)
    price_epsilon = config.price_tolerance_ticks * (10 ** -symbol_rule.quote_precision)

    expects_position = expected.side is not None
    has_position = reported is not None

    # 1) Existence and side: no monetary allowance applies; funding never creates or removes a
    #    position, so any mismatch here is real drift.
    if expects_position != has_position:
        if expects_position:
            drifts.append("bot expects a position but the exchange reports none")
        else:
            drifts.append("bot expects to be flat but the exchange reports a position")
    elif expects_position and reported is not None:
        reported_side = _side_of(reported)
        if reported_side is not expected.side:
            drifts.append(
                f"side mismatch: bot {expected.side.value}, exchange {reported_side.value}"
            )
        # 2) Size, at lot precision.
        if abs(reported.quantity - expected.quantity) > size_epsilon:
            drifts.append(
                f"size mismatch beyond {config.size_tolerance_lots} lot(s): "
                f"bot {expected.quantity}, exchange {reported.quantity}"
            )
        # 3) Entry price, at tick precision.
        if expected.entry_price is not None:
            if abs(reported.entry_price - expected.entry_price) > price_epsilon:
                drifts.append(
                    f"entry-price mismatch beyond {config.price_tolerance_ticks} tick(s): "
                    f"bot {expected.entry_price}, exchange {reported.entry_price}"
                )

    # 4) Protective stop presence: a hard check, the "stop failed to rest" critical guard.
    if expected.expects_resting_stop:
        if not _has_resting_stop(symbol, reported_orders):
            drifts.append("no reduce-only stop resting on the exchange while exposed")
        elif expected.stop_price is not None:
            reported_stop = _resting_stop_price(symbol, reported_orders)
            if (
                reported_stop is not None
                and abs(reported_stop - expected.stop_price) > price_epsilon
            ):
                drifts.append(
                    f"stop-price mismatch beyond {config.price_tolerance_ticks} tick(s): "
                    f"bot {expected.stop_price}, exchange {reported_stop}"
                )

    # 5) Equity, with the funding/fee accrual allowance (monetary only).
    if expected.expected_equity is not None and reported_balance is not None:
        notional = expected.quantity * (expected.entry_price or 0.0)
        allowance = config.equity_accrual_fraction * max(notional, expected.expected_equity)
        if abs(reported_balance.total - expected.expected_equity) > allowance:
            drifts.append(
                f"equity drift beyond the accrual allowance (${allowance:,.2f}): "
                f"bot ${expected.expected_equity:,.2f}, exchange ${reported_balance.total:,.2f}"
            )

    return ReconciliationResult(symbol=symbol, is_clean=not drifts, drifts=tuple(drifts))
