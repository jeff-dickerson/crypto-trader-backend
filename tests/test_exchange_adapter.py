"""Unit tests for the exchange-adapter seam.

Only the parts with real behaviour are tested: SymbolRule's precision rounding and
derived minimum-notional (load-bearing for the plan-time affordability check in
PRD 4.2), RateLimitStatus.remaining, the confirmed ExchangeCapabilities defaults,
and the ABC's instantiation-loudness (the reason an ABC was chosen over a Protocol).
The abstract method signatures themselves carry no behaviour and are not tested.
"""

from __future__ import annotations

import pytest

from crypto_trader.exchange import (
    Balance,
    ExchangeAdapter,
    ExchangeCapabilities,
    MarketOrderRequest,
    Order,
    OrderRequest,
    Position,
    PositionMode,
    RateLimitStatus,
    StopOrderRequest,
    SymbolRule,
)

# Approximates the public trading-pairs data for BTCUSDT observed during the spike:
# minTradeVolume 0.0001 BTC, basePrecision 4, quotePrecision 1.
BTC_RULE = SymbolRule(
    symbol="BTCUSDT",
    base_precision=4,
    quote_precision=1,
    min_trade_volume=0.0001,
)


def test_min_notional_is_derived_from_volume_and_price() -> None:
    # Bitunix exposes no minNotional field: it is min_trade_volume * price.
    assert BTC_RULE.min_notional(70000.0) == pytest.approx(7.0)


def test_round_quantity_floors_so_it_never_exceeds_intended_size() -> None:
    # 0.00019 must floor to 0.0001 at 4dp, not round up to 0.0002 (which would be a
    # larger position than the risk plan intended).
    assert BTC_RULE.round_quantity(0.00019) == pytest.approx(0.0001)
    assert BTC_RULE.round_quantity(0.00025) == pytest.approx(0.0002)


def test_round_price_snaps_to_the_quote_tick() -> None:
    assert BTC_RULE.round_price(70000.06) == pytest.approx(70000.1)
    assert BTC_RULE.round_price(70000.04) == pytest.approx(70000.0)


def test_round_quantity_rejects_negative_precision() -> None:
    with pytest.raises(ValueError):
        SymbolRule("X", base_precision=-1, quote_precision=2,
                   min_trade_volume=1.0).round_quantity(1.0)


def test_rate_limit_remaining_never_goes_negative() -> None:
    status = RateLimitStatus(
        scope="ip", limit_per_window=10, used_in_window=12, window_seconds=1.0
    )
    assert status.remaining == 0
    ok = RateLimitStatus(scope="ip", limit_per_window=10, used_in_window=3,
                         window_seconds=1.0)
    assert ok.remaining == 7


def test_capabilities_defaults_match_the_confirmed_bitunix_matrix() -> None:
    caps = ExchangeCapabilities()
    assert caps.native_stop_market is True
    assert caps.native_reduce_only is True
    assert caps.native_tp_sl_bracket is True
    # Not confirmed to exist -> the interface must not assume them.
    assert caps.native_trailing_stop is False
    assert caps.native_arbitrary_oco is False
    assert caps.position_mode_scope == "account"
    assert caps.margin_mode_scope == "symbol"


def test_exchange_adapter_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ExchangeAdapter()  # type: ignore[abstract]


def test_incomplete_adapter_fails_loudly_at_construction() -> None:
    # The whole point of an ABC here: a subclass missing a method raises at
    # instantiation, not silently at first order placement.
    class HalfAdapter(ExchangeAdapter):
        @property
        def capabilities(self) -> ExchangeCapabilities:
            return ExchangeCapabilities()

        # every other abstract method deliberately omitted

    with pytest.raises(TypeError):
        HalfAdapter()  # type: ignore[abstract]


def test_complete_stub_adapter_satisfies_the_contract() -> None:
    class StubAdapter(ExchangeAdapter):
        @property
        def capabilities(self) -> ExchangeCapabilities:
            return ExchangeCapabilities()

        def get_symbol_rule(self, symbol: str) -> SymbolRule:
            return BTC_RULE

        def place_limit_order(self, request: OrderRequest) -> Order:
            raise NotImplementedError

        def place_market_order(self, request: MarketOrderRequest) -> Order:
            raise NotImplementedError

        def place_stop_order(self, request: StopOrderRequest) -> Order:
            raise NotImplementedError

        def cancel_order(self, symbol: str, order_id: str) -> Order:
            raise NotImplementedError

        def get_open_orders(self, symbol: str | None = None) -> list[Order]:
            return []

        def get_positions(self, symbol: str | None = None) -> list[Position]:
            return []

        def get_balance(self) -> Balance:
            return Balance(currency="USDT", total=0.0, available=0.0)

        def rate_limit_status(self) -> RateLimitStatus:
            return RateLimitStatus(scope="ip", limit_per_window=10,
                                   used_in_window=0, window_seconds=1.0)

        def consecutive_api_failures(self) -> int:
            return 0

    adapter = StubAdapter()
    assert isinstance(adapter, ExchangeAdapter)
    assert adapter.capabilities.position_mode is PositionMode.ONE_WAY
    assert adapter.get_symbol_rule("BTCUSDT").symbol == "BTCUSDT"
    assert adapter.get_open_orders() == []
