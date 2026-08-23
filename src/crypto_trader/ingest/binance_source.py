"""Binance spot kline source (data-vision mirror): RESEARCH / BACKTEST ONLY.

PRD section 10 permits a deep-history supplement from a major venue "if Bitunix is shallow,
research/backtest only, never live decisions." Bitunix's own kline history is bounded by each
symbol's listing date, so a materially larger, multi-year, multi-regime Gate 1 sample needs a
venue with deeper history. Binance is that supplement here.

Binance's USD-M futures API (fapi.binance.com) is geo-restricted (HTTP 451) from this build
environment, so this source reads Binance's public spot data mirror
(data-api.binance.vision), which serves unauthenticated spot klines with no geo-block and
even deeper history (BTC spot to 2017). Spot OHLCV of the same majors is a sound deep-history
proxy for swing-pattern research; the universe-expansion report's Bitunix-vs-Binance parity
spot-check quantifies the residual difference. See BinanceSourceConfig for the full rationale.

HARD BOUNDARY: this source must never reach any live or paper trading decision. The live
venue is Bitunix, the single source of truth (governing principle 1); mixing a second venue's
prices into a live decision would break that invariant and reintroduce the exact backtest-vs-
live parity risk the design review flagged. This module is deliberately kept out of every
live/paper import path:

- Nothing under ``paper/``, ``exchange/``, ``safety/``, ``approval/``, or ``api/`` imports it
  (enforced by ``tests/test_research_source_isolation.py``, which fails if any of those
  packages references Binance).
- It implements the same ``CandleSource`` protocol as ``BitunixCandleSource`` only so the
  unchanged ingest pipeline can write its candles into a SEPARATE research database; it is
  never registered as the live/paper candle source.
- ``RESEARCH_ONLY = True`` is a machine-readable marker a future guard can assert on.

No credentials: unauthenticated public market data only.
"""

from __future__ import annotations

import requests

from crypto_trader.config import BinanceSourceConfig, Timeframe
from crypto_trader.ingest.models import RawCandle

# Bitunix and Binance happen to share timeframe tokens ("4h", "1d"), but map explicitly so a
# future timeframe divergence is a one-line fix here, not a silent mismatch.
_INTERVAL_BY_TIMEFRAME = {
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
}


class BinanceSpotCandleSource:
    """Fetches candles from Binance's public spot kline mirror (data-api.binance.vision).

    RESEARCH / BACKTEST ONLY (see module docstring). Unauthenticated public data.
    """

    #: Machine-readable marker: this data must never feed a live/paper decision.
    RESEARCH_ONLY = True

    def __init__(self, config: BinanceSourceConfig | None = None) -> None:
        self._config = config or BinanceSourceConfig()

    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 200,
        end_time_ms: int | None = None,
    ) -> list[RawCandle]:
        """Fetch up to ``limit`` candles at or before ``end_time_ms``.

        Matches ``CandleSource``: ``end_time_ms`` maps to Binance's ``endTime`` param so a
        caller pages backward past the per-request row cap to reach deep history. Binance
        returns rows ASCENDING by open time (oldest first), the opposite of Bitunix; the
        ingest validator sorts regardless, so this is not adjusted here beyond the note.
        """
        interval = _INTERVAL_BY_TIMEFRAME[timeframe]
        capped = min(limit, self._config.max_rows_per_request)
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": capped,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        response = requests.get(
            f"{self._config.base_url}{self._config.kline_path}",
            params=params,
            timeout=self._config.request_timeout_seconds,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"Binance kline request for {symbol} returned {rows!r}")
        # Binance kline row layout (public docs, identical for spot and futures):
        # [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...].
        # Only the first eight are used.
        return [
            RawCandle(
                symbol=symbol,
                open_time_ms=row[0],
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
                quote_volume=row[7],
            )
            for row in rows
        ]
