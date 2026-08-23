"""Row-parsing tests for the research-only Binance source, network stubbed out.

No real network: crypto_trader.ingest.binance_source.requests.get is monkeypatched to a
canned response, so the only thing under test is the Binance kline row layout -> RawCandle
mapping (the real correctness risk when adding a second venue).
"""

from __future__ import annotations

import crypto_trader.ingest.binance_source as bsrc
from crypto_trader.config import Timeframe
from crypto_trader.ingest.binance_source import BinanceSpotCandleSource


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # noqa: D401 - stub
        return None

    def json(self) -> object:
        return self._payload


def test_binance_row_layout_maps_to_rawcandle(monkeypatch):
    # Binance kline row: [openTime, open, high, low, close, volume, closeTime,
    # quoteAssetVolume, ...]; values arrive as strings except the two timestamps.
    rows = [
        [1704067200000, "40000.0", "40500.0", "39800.0", "40250.0", "1234.5",
         1704081599999, "49876543.2", 100, "1", "2", "0"],
        [1704081600000, "40250.0", "40600.0", "40100.0", "40550.0", "2222.2",
         1704095999999, "89765432.1", 120, "3", "4", "0"],
    ]
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(rows)

    monkeypatch.setattr(bsrc.requests, "get", fake_get)

    source = BinanceSpotCandleSource()
    out = source.fetch_candles("BTCUSDT", Timeframe.H4, limit=500, end_time_ms=1704095999999)

    assert captured["params"]["symbol"] == "BTCUSDT"
    assert captured["params"]["interval"] == "4h"
    assert captured["params"]["endTime"] == 1704095999999
    assert len(out) == 2
    first = out[0]
    assert first.open_time_ms == 1704067200000
    assert first.open == "40000.0"
    assert first.high == "40500.0"
    assert first.low == "39800.0"
    assert first.close == "40250.0"
    assert first.volume == "1234.5"
    assert first.quote_volume == "49876543.2"


def test_binance_caps_limit_at_configured_max(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _FakeResponse([])

    monkeypatch.setattr(bsrc.requests, "get", fake_get)

    source = BinanceSpotCandleSource()
    source.fetch_candles("ETHUSDT", Timeframe.D1, limit=100_000)

    assert captured["params"]["limit"] == 1000
    assert captured["params"]["interval"] == "1d"


def test_research_only_marker_is_set():
    assert BinanceSpotCandleSource.RESEARCH_ONLY is True
