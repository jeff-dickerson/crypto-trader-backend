"""Tests for the rule-defined universe selector, offline via FixtureMarketData.

No network: BitunixMarketData is never instantiated here (mirroring how the ingest tests
never instantiate BitunixCandleSource).
"""

from __future__ import annotations

from crypto_trader.ingest.universe import (
    FixtureMarketData,
    UniverseConfig,
    depth_notional_within,
    select_universe,
)


def _pair(symbol: str, *, status: str = "OPEN", api: bool = True, quote: str = "USDT") -> dict:
    return {
        "symbol": symbol,
        "quote": quote,
        "symbolStatus": status,
        "isApiSupported": api,
        "basePrecision": 3,
        "quotePrecision": 2,
    }


def _ticker(symbol: str, quote_vol: float) -> dict:
    return {"symbol": symbol, "quoteVol": str(quote_vol)}


def _book(mid: float, notional_each_side: float) -> dict:
    # One level per side exactly at +-0.1% of mid (inside a 0.5% band), each carrying
    # notional_each_side of resting size, so summed depth is 2 * notional_each_side.
    ask_price = mid * 1.001
    bid_price = mid * 0.999
    return {
        "asks": [[str(ask_price), str(notional_each_side / ask_price)]],
        "bids": [[str(bid_price), str(notional_each_side / bid_price)]],
    }


def test_depth_notional_sums_only_within_band():
    depth = {
        "asks": [["100.0", "10"], ["100.4", "5"], ["101.0", "99"]],
        "bids": [["99.9", "10"], ["99.7", "5"], ["98.0", "99"]],
    }
    # mid ~= 99.95, band +-0.5% -> [99.45, 100.45]; the 101.0 ask and 98.0 bid are excluded.
    notional = depth_notional_within(depth, 0.005)
    expected = 100.0 * 10 + 100.4 * 5 + 99.9 * 10 + 99.7 * 5
    assert notional is not None
    assert abs(notional - expected) < 1e-6


def test_depth_notional_none_on_empty_book():
    assert depth_notional_within({"asks": [], "bids": []}, 0.005) is None
    assert depth_notional_within({}, 0.005) is None


def test_selector_applies_volume_then_depth_and_ranks_by_volume():
    pairs = [
        _pair("BTCUSDT"),
        _pair("ETHUSDT"),
        _pair("THINUSDT"),  # high volume, thin book
        _pair("LOWVOLUSDT"),  # below volume floor
        _pair("PREVIEWUSDT", status="PREVIEW"),  # not open
        _pair("NOAPIUSDT", api=False),  # not api-supported
        _pair("BTCUSDC", quote="USDC"),  # wrong quote
    ]
    tickers = [
        _ticker("BTCUSDT", 1_000_000_000),
        _ticker("ETHUSDT", 500_000_000),
        _ticker("THINUSDT", 50_000_000),
        _ticker("LOWVOLUSDT", 100_000),
        _ticker("PREVIEWUSDT", 999_000_000),
        _ticker("NOAPIUSDT", 999_000_000),
        _ticker("BTCUSDC", 999_000_000),
    ]
    depth = {
        "BTCUSDT": _book(70_000, 5_000_000),
        "ETHUSDT": _book(3_000, 4_000_000),
        "THINUSDT": _book(1.0, 10_000),  # summed depth 20k, below 50k floor
    }
    md = FixtureMarketData(trading_pairs=pairs, tickers=tickers, depth=depth)
    cfg = UniverseConfig(min_quote_volume_24h=2_000_000, min_depth_notional=50_000)

    result = select_universe(md, cfg)

    # Only OPEN + api + USDT pairs are candidates.
    assert result.n_tradable_usdt == 4
    # BTC, ETH, THIN clear the 2M volume floor; LOWVOL does not.
    assert result.n_passed_volume == 3
    # THIN fails the depth floor; BTC and ETH pass.
    assert result.research_symbols == ["BTCUSDT", "ETHUSDT"]
    assert result.n_passed_depth == 2
    thin = next(r for r in result.evaluated if r.symbol == "THINUSDT")
    assert thin.passed_volume is True
    assert thin.passed_depth is False
    assert "below" in thin.reason


def test_live_universe_is_top_n_of_research_set():
    pairs = [_pair(f"S{i}USDT") for i in range(5)]
    # Descending volumes so ranking is unambiguous.
    tickers = [_ticker(f"S{i}USDT", 100_000_000 - i * 1_000_000) for i in range(5)]
    depth = {f"S{i}USDT": _book(10.0, 1_000_000) for i in range(5)}
    md = FixtureMarketData(trading_pairs=pairs, tickers=tickers, depth=depth)
    cfg = UniverseConfig(min_quote_volume_24h=2_000_000, min_depth_notional=50_000)

    result = select_universe(md, cfg)

    assert len(result.research_symbols) == 5
    assert result.live_symbols(3) == ["S0USDT", "S1USDT", "S2USDT"]


def test_symbol_with_no_book_fails_depth_with_reason():
    pairs = [_pair("GHOSTUSDT")]
    tickers = [_ticker("GHOSTUSDT", 10_000_000)]
    md = FixtureMarketData(trading_pairs=pairs, tickers=tickers, depth={})  # no book
    result = select_universe(md, UniverseConfig())

    assert result.research_symbols == []
    ghost = result.evaluated[0]
    assert ghost.passed_volume is True
    assert ghost.passed_depth is False
    assert ghost.depth_notional is None
    assert "no order book" in ghost.reason
