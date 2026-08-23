"""Rule-defined, self-updating trading universe selector.

The original spec's universe rule is "top 10-15 Bitunix perps by 24h volume above a
liquidity floor (24h volume threshold plus an order-book depth check), rule-defined and
self-updating." Until now the universe was a hand-picked list; this module makes it a rule
run against Bitunix's public market-data endpoints, so the set is reproducible and updates
itself as volumes move.

The rule, in order:

1. Tradability. Keep only symbols the public trading-pairs endpoint reports as
   ``symbolStatus == "OPEN"``, ``isApiSupported == true``, and quoted in USDT. This drops
   previews, delisted, and non-USDT contracts before any liquidity test.
2. 24h volume floor. Keep only symbols whose public 24h ``quoteVol`` (USDT-denominated
   turnover) is at or above ``UniverseConfig.min_quote_volume_24h``. This is the primary
   liquidity gate and is what ranks the survivors.
3. Order-book depth check. For each volume survivor, sum the resting bid and ask notional
   within ``depth_pct`` of the mid price from the public depth endpoint, and keep only
   symbols at or above ``UniverseConfig.min_depth_notional``. This catches the
   high-headline-volume, thin-book case (a churny meme perp) that the volume floor alone
   would wave through.

Two outputs come from the one rule (see AGENTS.md, "Architecture decisions from Build Order
universe expansion"):

- ``live_universe`` is the top ``live_universe_size`` survivors by 24h volume, the
  spec's 10-15 live trading set.
- ``research_universe`` is EVERY survivor, a deliberately larger N used only to widen the
  Gate 1 backtest sample. The larger research N is a backtest-research decision, not a
  change to the live trading universe size, which stays a separate captain decision.

The HTTP surface is behind the ``MarketData`` protocol so the selector is unit-testable
offline with a fixture: tests never touch the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import requests

from crypto_trader.config import BitunixSourceConfig


@dataclass(frozen=True)
class UniverseConfig:
    """The rule's thresholds. Every number here is the documented, overridable rule.

    Defaults were chosen against the live Bitunix distribution characterized for this task
    (see the universe-expansion report): of 625 tradable USDT perps, roughly 61 clear a
    2,000,000 USDT 24h-volume floor, and the depth floor then trims the thin-book outliers.
    """

    # Primary liquidity gate: minimum 24h quote (USDT) volume.
    min_quote_volume_24h: float = 2_000_000.0
    # Order-book depth gate: minimum summed bid+ask notional within depth_pct of mid.
    min_depth_notional: float = 50_000.0
    # Half-width of the price band the depth check sums over (0.5% of mid each side).
    depth_pct: float = 0.005
    # Bitunix depth endpoint accepts only a discrete set of level counts {1, 5, 15, 50};
    # 50 is the deepest and covers the +-0.5% band for every liquid symbol.
    depth_limit: int = 50
    # Live trading universe size (spec's "top 10-15"); does not bound the research set.
    live_universe_size: int = 15
    quote_asset: str = "USDT"


@dataclass(frozen=True)
class SymbolLiquidity:
    """One symbol's measured liquidity and why it did or did not clear the rule."""

    symbol: str
    quote_volume_24h: float
    depth_notional: float | None
    passed_volume: bool
    passed_depth: bool
    reason: str

    @property
    def selected(self) -> bool:
        return self.passed_volume and self.passed_depth


@dataclass(frozen=True)
class UniverseResult:
    """The selector's full output: the ranked survivors plus honest funnel counts."""

    config: UniverseConfig
    # All survivors (volume + depth), ranked by 24h volume descending.
    research_universe: list[SymbolLiquidity]
    # Per-symbol detail for every symbol that cleared the volume floor (depth attempted).
    evaluated: list[SymbolLiquidity]
    n_total_pairs: int
    n_tradable_usdt: int
    n_passed_volume: int
    n_passed_depth: int

    @property
    def research_symbols(self) -> list[str]:
        return [s.symbol for s in self.research_universe]

    def live_symbols(self, size: int | None = None) -> list[str]:
        """Top-N survivors by 24h volume: the live trading universe."""
        n = size if size is not None else self.config.live_universe_size
        return [s.symbol for s in self.research_universe[:n]]


class MarketData(Protocol):
    """The public market-data surface the selector needs. Swappable for offline tests."""

    def fetch_trading_pairs(self) -> list[dict]: ...

    def fetch_tickers(self) -> list[dict]: ...

    def fetch_depth(self, symbol: str, limit: int) -> dict: ...


class BitunixMarketData:
    """Reads Bitunix's public, unauthenticated market-data endpoints over HTTP.

    No credentials: trading-pairs, tickers, and depth are all public. Mirrors
    BitunixCandleSource's transport shape (requests, code==0 envelope check).
    """

    def __init__(self, config: BitunixSourceConfig | None = None) -> None:
        self._config = config or BitunixSourceConfig()

    def _get(self, path: str, params: dict | None = None) -> object:
        response = requests.get(
            f"{self._config.base_url}{path}",
            params=params or {},
            timeout=self._config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Bitunix request to {path} failed: {payload.get('msg', 'unknown error')}"
            )
        return payload.get("data")

    def fetch_trading_pairs(self) -> list[dict]:
        data = self._get(self._config.trading_pairs_path)
        return list(data or [])

    def fetch_tickers(self) -> list[dict]:
        data = self._get(self._config.tickers_path)
        return list(data or [])

    def fetch_depth(self, symbol: str, limit: int) -> dict:
        data = self._get(self._config.depth_path, {"symbol": symbol, "limit": limit})
        return dict(data or {})


@dataclass(frozen=True)
class FixtureMarketData:
    """Serves pre-built market data for tests and offline development."""

    trading_pairs: list[dict] = field(default_factory=list)
    tickers: list[dict] = field(default_factory=list)
    depth: dict[str, dict] = field(default_factory=dict)

    def fetch_trading_pairs(self) -> list[dict]:
        return list(self.trading_pairs)

    def fetch_tickers(self) -> list[dict]:
        return list(self.tickers)

    def fetch_depth(self, symbol: str, limit: int) -> dict:
        return dict(self.depth.get(symbol, {}))


def _to_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def depth_notional_within(depth: dict, pct: float) -> float | None:
    """Sum resting bid+ask notional within +-pct of the mid price.

    ``depth`` is the exchange's raw depth payload: ``{"asks": [[price, qty], ...],
    "bids": [[price, qty], ...]}`` with best levels first. Returns None when the book is
    empty or unparseable, so the caller records "no book" rather than a misleading zero.
    """
    asks_raw = depth.get("asks") or []
    bids_raw = depth.get("bids") or []
    asks = [(_to_float(p), _to_float(q)) for p, q in asks_raw]
    bids = [(_to_float(p), _to_float(q)) for p, q in bids_raw]
    asks = [(p, q) for p, q in asks if p is not None and q is not None]
    bids = [(p, q) for p, q in bids if p is not None and q is not None]
    if not asks or not bids:
        return None
    mid = (asks[0][0] + bids[0][0]) / 2.0
    if mid <= 0:
        return None
    lo, hi = mid * (1.0 - pct), mid * (1.0 + pct)
    bid_notional = sum(p * q for p, q in bids if p >= lo)
    ask_notional = sum(p * q for p, q in asks if p <= hi)
    return bid_notional + ask_notional


def select_universe(
    market_data: MarketData, config: UniverseConfig | None = None
) -> UniverseResult:
    """Run the universe rule against a market-data source and return the ranked result.

    Depth is queried ONLY for symbols that clear the volume floor, so a full run is a
    handful of list calls plus one depth call per volume survivor, not one per listed
    contract.
    """
    config = config or UniverseConfig()

    pairs = market_data.fetch_trading_pairs()
    tradable: dict[str, dict] = {}
    for p in pairs:
        symbol = p.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        if p.get("symbolStatus") != "OPEN":
            continue
        if not p.get("isApiSupported"):
            continue
        if p.get("quote") != config.quote_asset:
            continue
        tradable[symbol] = p

    tickers = market_data.fetch_tickers()
    volume_by_symbol: dict[str, float] = {}
    for t in tickers:
        symbol = t.get("symbol")
        if not isinstance(symbol, str):
            continue
        vol = _to_float(t.get("quoteVol"))
        if vol is not None:
            volume_by_symbol[symbol] = vol

    # Volume floor, ranked by 24h volume descending.
    volume_survivors = sorted(
        (
            (symbol, volume_by_symbol.get(symbol, 0.0))
            for symbol in tradable
            if volume_by_symbol.get(symbol, 0.0) >= config.min_quote_volume_24h
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    evaluated: list[SymbolLiquidity] = []
    research: list[SymbolLiquidity] = []
    for symbol, vol in volume_survivors:
        depth = market_data.fetch_depth(symbol, config.depth_limit)
        notional = depth_notional_within(depth, config.depth_pct)
        passed_depth = notional is not None and notional >= config.min_depth_notional
        if notional is None:
            reason = "no order book returned; depth check failed"
        elif passed_depth:
            reason = "selected"
        else:
            reason = (
                f"depth {notional:,.0f} USDT within +-{config.depth_pct:.1%} of mid is "
                f"below the {config.min_depth_notional:,.0f} USDT floor"
            )
        row = SymbolLiquidity(
            symbol=symbol,
            quote_volume_24h=vol,
            depth_notional=notional,
            passed_volume=True,
            passed_depth=passed_depth,
            reason=reason,
        )
        evaluated.append(row)
        if row.selected:
            research.append(row)

    return UniverseResult(
        config=config,
        research_universe=research,
        evaluated=evaluated,
        n_total_pairs=len(pairs),
        n_tradable_usdt=len(tradable),
        n_passed_volume=len(volume_survivors),
        n_passed_depth=len(research),
    )
