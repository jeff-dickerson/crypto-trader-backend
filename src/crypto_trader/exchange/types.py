"""Small shared data models for the ExchangeAdapter seam.

These types are the vocabulary the abstract ExchangeAdapter (adapter.py) speaks:
the future DryRun/paper adapter (Build Order step 4) and the future live Bitunix
adapter (step 6) both produce and consume them. They are deliberately minimal and
map onto capabilities that were actually confirmed against Bitunix's public API
during the read-only characterization spike (see AGENTS.md, "Architecture decisions
from the Bitunix characterization spike"); nothing here models a capability that
could not be confirmed to exist.

Enum string values match Bitunix's documented request vocabulary where a mapping
exists (BUY/SELL, LIMIT/MARKET, GTC/IOC/FOK/POST_ONLY, MARK_PRICE/LAST_PRICE,
ONE_WAY/HEDGE, ISOLATION/CROSS), so a live adapter can serialise them directly and a
paper adapter reads the same values. The exchange remains the single source of truth
(governing principle 1): these are the shapes the adapter returns from the exchange,
never the bot's private idea of state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderSide(str, Enum):
    """Direction of an order. Matches Bitunix `side`."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Base order type. Bitunix `orderType` supports only these two natively.

    There is no native standalone stop order type: a server-side stop is a MARKET
    (or LIMIT) order gated on a trigger price, expressed through StopOrderRequest.
    """

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(str, Enum):
    """Order time-in-force. Matches Bitunix `effect` (required for limit orders).

    POST_ONLY is the maker-only mode used for a resting first-touch limit entry: it
    rejects rather than crossing the book, so the entry stays a maker limit at the
    value-area boundary.
    """

    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "POST_ONLY"


class TriggerPriceType(str, Enum):
    """Which price a stop/take-profit trigger watches. Matches Bitunix `slStopType`.

    MARK_PRICE is generally preferred for a protective stop because it is harder to
    wick-hunt than the last traded price; the choice is left to the caller.
    """

    MARK_PRICE = "MARK_PRICE"
    LAST_PRICE = "LAST_PRICE"


class PositionMode(str, Enum):
    """Account position mode. Matches Bitunix `positionMode`.

    CONFIRMED account-global on Bitunix (one setting for all symbols), and it cannot
    be changed while any position or order is open. The live account's actual mode
    could not be read without credentials during the characterization spike, so the
    adapter must REPORT this (see ExchangeCapabilities.position_mode and Position),
    never assume it: the four-state PositionState contract in the strategy core
    assumes one-way netting, whereas HEDGE mode can hold a simultaneous long and
    short and routes closes via a trade-side flag instead of reduce-only.
    """

    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"


class MarginMode(str, Enum):
    """Per-symbol margin mode. Matches Bitunix `marginMode` (change_margin_mode).

    CONFIRMED per-symbol on Bitunix (unlike position mode, which is account-global).
    Governing principle 4 makes ISOLATION the safety floor, so the live adapter
    (step 6) must set this explicitly per traded symbol rather than trust the
    account default (BTCUSDT/ETHUSDT report a non-isolated default in public
    trading-pairs data).
    """

    ISOLATION = "ISOLATION"
    CROSS = "CROSS"


class OrderStatus(str, Enum):
    """Lifecycle state of an order as reported by the exchange.

    Intentionally coarse: the exact Bitunix status vocabulary is an authenticated,
    step-6 mapping concern. These are the states the position manager and reconciler
    branch on. PARTIALLY_FILLED exists because a resting limit at a zone boundary can
    partially fill (mirrors PositionStatus.PARTIAL in the strategy core).
    """

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    TRIGGERED = "triggered"


@dataclass(frozen=True)
class SymbolRule:
    """Trading rules for one symbol, from the public trading-pairs endpoint.

    CONFIRMED per-symbol and available unauthenticated
    (GET /api/v1/futures/market/trading_pairs). `min_trade_volume` is the minimum
    order size in BASE units; `base_precision`/`quote_precision` are the decimal
    places for quantity and price. Bitunix exposes NO explicit USD minimum-notional
    field, so a notional floor must be DERIVED as min_trade_volume * price (see
    min_notional): this is what PRD 4.2 needs to void an unexecutable plan at plan
    time rather than after approval.
    """

    symbol: str
    base_precision: int
    quote_precision: int
    min_trade_volume: float
    max_limit_order_volume: float | None = None
    max_market_order_volume: float | None = None
    min_leverage: int | None = None
    max_leverage: int | None = None
    # Per-symbol funding-rate clamp from trading_pairs (maxFundingRate/minFundingRate).
    max_funding_rate: float | None = None
    min_funding_rate: float | None = None

    def min_notional(self, price: float) -> float:
        """Derived USD notional floor at `price`: min_trade_volume * price.

        Bitunix has no standalone minimum-notional field; this is the honest
        derivation for the plan-time affordability check.
        """
        return self.min_trade_volume * price

    def round_quantity(self, quantity: float) -> float:
        """Round a quantity DOWN to base_precision, so it never exceeds intended size."""
        return _floor_to_precision(quantity, self.base_precision)

    def round_price(self, price: float) -> float:
        """Round a price to quote_precision (nearest tick)."""
        factor = 10**self.quote_precision
        return round(price * factor) / factor


def _floor_to_precision(value: float, precision: int) -> float:
    """Floor `value` to `precision` decimal places without float-format surprises."""
    if precision < 0:
        raise ValueError("precision must be >= 0")
    factor = 10**precision
    # int() truncates toward zero; sizes are non-negative here, so this floors.
    return int(value * factor) / factor


@dataclass(frozen=True)
class OrderRequest:
    """A resting limit order to place (typically the first-touch entry).

    `stop_loss_price` and `take_profit_price` are OPTIONAL protective legs attached
    atomically to the entry. This maps to Bitunix's CONFIRMED native attached-TP/SL
    on place_order (slPrice/tpPrice with a MARKET stop type), which lets the
    safety-floor stop rest server-side the instant the entry is placed, satisfying
    "a stop before entry" (PRD 5.1) without a second round trip. Leaving them None
    places a bare limit whose stop is placed separately via StopOrderRequest.
    """

    symbol: str
    side: OrderSide
    quantity: float
    price: float
    time_in_force: TimeInForce = TimeInForce.POST_ONLY
    reduce_only: bool = False
    client_order_id: str | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    trigger_price_type: TriggerPriceType = TriggerPriceType.MARK_PRICE


@dataclass(frozen=True)
class StopOrderRequest:
    """A native server-side stop order: the on-exchange safety floor (principle 4).

    CONFIRMED native on Bitunix: a stop is a MARKET order gated on `stop_price` via
    the TP/SL surface (slPrice with slOrderType=MARKET), reduce-only by intent. This
    is NOT a bot-emulated stop; it rests on the exchange. `reduce_only` defaults True
    because a protective stop must only ever close exposure, never open new exposure.

    A native TRAILING stop was NOT found in the documented REST endpoints, so
    trailing is emulated bot-side by re-placing this stop at a tighter `stop_price`
    (the strategy core already computes the trail); see ExchangeCapabilities.
    """

    symbol: str
    side: OrderSide
    quantity: float
    stop_price: float
    trigger_price_type: TriggerPriceType = TriggerPriceType.MARK_PRICE
    reduce_only: bool = True
    client_order_id: str | None = None


@dataclass(frozen=True)
class Order:
    """An order as reported by the exchange (the source of truth), not bot memory."""

    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    filled_quantity: float
    status: OrderStatus
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    created_at_ms: int | None = None


@dataclass(frozen=True)
class Position:
    """An open position as reported by the exchange.

    `position_mode` is carried per-position because it is queried from the exchange,
    never assumed: in HEDGE mode a symbol can have both a long and a short position
    simultaneously, so consumers must not collapse to a single netted position
    without checking this.
    """

    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    position_mode: PositionMode
    margin_mode: MarginMode | None = None
    leverage: int | None = None
    unrealized_pnl: float | None = None
    liquidation_price: float | None = None


@dataclass(frozen=True)
class Fill:
    """A single execution against an order. Fees are venue truth, not estimates."""

    order_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    fee: float
    fee_currency: str
    timestamp_ms: int
    trade_id: str | None = None
    is_maker: bool | None = None


@dataclass(frozen=True)
class Balance:
    """Account balance snapshot from the exchange."""

    currency: str
    total: float
    available: float
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class RateLimitStatus:
    """A snapshot of request-budget headroom, so it is "always visible" (spec).

    Bitunix publishes simple fixed caps (REST market data 10 req/sec/IP, private
    trade endpoints 10 req/sec/UID, WebSocket 5 inbound msgs/sec) and returns NO
    weight or remaining-quota headers in its public responses, so the adapter tracks
    its own request budget locally and reports it through this shape. `scope`
    distinguishes an IP-based public limit from a UID-based private one.
    """

    scope: str
    limit_per_window: int
    used_in_window: int
    window_seconds: float

    @property
    def remaining(self) -> int:
        """Requests still allowed in the current window (never negative)."""
        return max(0, self.limit_per_window - self.used_in_window)


@dataclass(frozen=True)
class ExchangeCapabilities:
    """What an adapter's venue can and cannot do natively.

    This is the machine-readable form of the characterization spike's findings, so
    the position manager and reconciler branch on FACTS rather than on the original
    spec's prose. The paper adapter (step 4) should mirror the live venue's
    capabilities so Gate 2 tests the same branches Gate 3 will hit. Defaults reflect
    Bitunix as CONFIRMED during the spike.
    """

    # A server-side stop-market rests on the exchange (the safety floor is literal).
    native_stop_market: bool = True
    # Reduce-only is a native order flag (one-way mode).
    native_reduce_only: bool = True
    # A paired take-profit + stop-loss bracket on a position is placed in one native
    # call; either leg firing closes the position. This is the specific "OCO" the
    # strategy needs, and it is NATIVE (not the same as arbitrary OCO between two
    # unrelated orders, which is not offered).
    native_tp_sl_bracket: bool = True
    # No native trailing stop was found in the documented REST endpoints: trailing is
    # emulated bot-side by re-placing the stop tighter.
    native_trailing_stop: bool = False
    # No arbitrary one-cancels-other between two independent orders.
    native_arbitrary_oco: bool = False
    # Position mode is account-global on Bitunix and must be reported, not assumed.
    position_mode: PositionMode = PositionMode.ONE_WAY
    position_mode_scope: str = "account"
    # Margin mode is per-symbol; ISOLATION is the required floor (principle 4).
    margin_mode_scope: str = "symbol"

    # Notes for humans reading a capabilities dump.
    notes: tuple[str, ...] = field(default_factory=tuple)
