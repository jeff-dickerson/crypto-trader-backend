"""The exchange-adapter seam: one interface, many venues.

ExchangeAdapter (adapter.py) is the abstract contract that the future DryRun/paper
adapter (Build Order step 4) and the future live Bitunix adapter (step 6) both
implement. The small shared data models it speaks live in types.py. This package
holds no working venue implementation: it is the interface plus its vocabulary,
informed by a read-only characterization of Bitunix's public API (see AGENTS.md).
"""

from __future__ import annotations

from crypto_trader.exchange.adapter import ExchangeAdapter, ExchangeConnectionError
from crypto_trader.exchange.types import (
    Balance,
    ExchangeCapabilities,
    Fill,
    MarginMode,
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
    TimeInForce,
    TriggerPriceType,
)

__all__ = [
    "ExchangeAdapter",
    "ExchangeConnectionError",
    "Balance",
    "ExchangeCapabilities",
    "Fill",
    "MarginMode",
    "MarketOrderRequest",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "PositionMode",
    "RateLimitStatus",
    "StopOrderRequest",
    "SymbolRule",
    "TimeInForce",
    "TriggerPriceType",
]
