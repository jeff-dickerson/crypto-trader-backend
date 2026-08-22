"""The ExchangeAdapter interface: the one seam every venue implements.

This is the contract the future DryRun/paper adapter (Build Order step 4) and the
future live Bitunix adapter (step 6) both implement. This task does NOT implement a
working Bitunix adapter: it defines the interface, informed by a read-only
characterization spike against Bitunix's public API (no orders placed, no
credentials used). The full findings, with what was confirmed versus assumed, live
in AGENTS.md under "Architecture decisions from the Bitunix characterization spike";
this module's docstrings cite the load-bearing ones inline.

Why an ABC, not a Protocol
--------------------------
The candle-source seam (crypto_trader.ingest.source.CandleSource) is a Protocol: a
single-method, duck-typed boundary where structural typing fits, and where tests
swap a fixture object freely. The exchange seam is different in kind. It is
money-adjacent, multi-method, and deliberately implemented by exactly two named
classes that the position manager and reconciler will isinstance-check and share
behaviour with. An abc.ABC gives a nominal base those classes explicitly subclass,
fails LOUDLY at instantiation if a method is missing (rather than silently at first
call, as a Protocol would), and lets the contract live in one place as
docstrings + the capabilities property. For a surface that places real orders, a
missing-method error at construction time is safer than one discovered mid-trade.

Governing principles this interface encodes
-------------------------------------------
1. Exchange is the single source of truth: every query returns the venue's reported
   state (Order/Position/Balance), never the bot's private idea of it.
4. On-exchange stops are the safety floor: place_stop_order places a NATIVE
   server-side stop (CONFIRMED on Bitunix), so the floor is literal, not emulated.
The spec's "rate-limit status always visible" requirement is met by
rate_limit_status(); the confirmed-versus-emulated capability matrix is exposed as
capabilities so callers branch on facts.

Scope notes
-----------
- No streaming methods. A public WebSocket is not needed to characterize the venue
  or to define this contract, and its documented reconnect/resync semantics were
  thin; a future live-adapter task (step 6) adds any streaming surface it needs.
- No margin-mode / position-mode SETTERS. Reading position_mode (account-global) and
  margin_mode (per-symbol) is enough to design against; the live adapter sets
  ISOLATION per symbol (principle 4) as step-6 work. The interface reports these so
  no assumption is hardcoded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from crypto_trader.exchange.types import (
    Balance,
    ExchangeCapabilities,
    MarketOrderRequest,
    Order,
    OrderRequest,
    Position,
    RateLimitStatus,
    StopOrderRequest,
    SymbolRule,
)


class ExchangeAdapter(ABC):
    """A venue the bot can trade through: paper (DryRun) or live Bitunix.

    Implementations must be honest about the venue: report the real capabilities via
    `capabilities`, return exchange-reported state from the query methods, and raise
    on any operation the venue does not support rather than silently faking it. No
    method here assumes a capability the characterization spike could not confirm.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ExchangeCapabilities:
        """What this venue can and cannot do natively (see ExchangeCapabilities).

        Callers branch on this instead of the original spec's prose: for example, the
        position manager emulates a trailing stop only when
        `capabilities.native_trailing_stop` is False, and treats a stop as the
        literal safety floor when `capabilities.native_stop_market` is True.
        """

    @abstractmethod
    def get_symbol_rule(self, symbol: str) -> SymbolRule:
        """Trading rules for `symbol`: precision, min size, leverage, funding caps.

        Sourced from the public trading-pairs endpoint on Bitunix. The risk system
        (PRD 4.2) calls SymbolRule.min_notional at plan time to void an unexecutable
        plan BEFORE approval, since Bitunix exposes no standalone minimum-notional.
        """

    @abstractmethod
    def place_limit_order(self, request: OrderRequest) -> Order:
        """Place a resting limit order (the first-touch entry) and return it.

        With POST_ONLY (the OrderRequest default) the entry stays a maker limit at the
        value-area boundary. Optional stop_loss_price/take_profit_price on the request
        attach Bitunix's native protective legs atomically, so the safety-floor stop
        rests the moment the entry is placed (principle 4, PRD 5.1). The returned
        Order carries the venue's order_id and status; a partial fill later surfaces
        as OrderStatus.PARTIALLY_FILLED via get_open_orders/get_positions.
        """

    @abstractmethod
    def place_market_order(self, request: MarketOrderRequest) -> Order:
        """Place a MARKET order that fills at once: the on-demand close/reduce.

        Added between the characterization spike and Build Order step 4: the spike's
        interface exposed resting limits and native stops but no way to close a position
        at market, which the strategy's momentum-shift exit requires and step 5's kill
        switch will reuse. MARKET is a CONFIRMED native Bitunix order type, so this assumes
        no unconfirmed capability. Reduce-only by intent (see MarketOrderRequest). The
        returned Order reports the exchange's actual fill price and quantity.
        """

    @abstractmethod
    def place_stop_order(self, request: StopOrderRequest) -> Order:
        """Place a NATIVE server-side stop order: the on-exchange safety floor.

        CONFIRMED native on Bitunix (a MARKET order gated on a trigger price via the
        TP/SL surface), so this is a real resting stop on the exchange, not a
        bot-side simulation. Reduce-only by intent. A trailing stop is achieved by
        cancelling and re-placing this at a tighter stop_price, because no native
        trailing stop was found (capabilities.native_trailing_stop is False).
        """

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> Order:
        """Cancel a resting order and return its final exchange-reported state."""

    @abstractmethod
    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Open orders as reported by the exchange, optionally filtered to `symbol`."""

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Open positions as reported by the exchange, optionally filtered to `symbol`.

        In HEDGE mode a symbol may report both a long and a short position, so callers
        must not assume a single netted position without checking Position.position_mode.
        """

    @abstractmethod
    def get_balance(self) -> Balance:
        """Account balance as reported by the exchange (the truth for risk sizing)."""

    @abstractmethod
    def rate_limit_status(self) -> RateLimitStatus:
        """Current request-budget headroom, so it is always visible (spec).

        Bitunix returns no weight/quota headers, so an implementation tracks its own
        request budget against the venue's fixed caps and reports it here.
        """
