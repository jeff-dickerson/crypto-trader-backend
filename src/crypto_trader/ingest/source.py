"""Swappable candle data-source interface.

CandleSource is the boundary the rest of the ingest pipeline depends on.
BitunixCandleSource talks to Bitunix's public, unauthenticated futures kline
endpoint over HTTP.
FixtureCandleSource serves pre-built candle lists, for tests and for development
when the network is unavailable.
No implementation here uses or stores exchange credentials: only public
market-data endpoints are in scope for this task.
"""

from __future__ import annotations

from typing import Protocol

import requests

from crypto_trader.config import BitunixSourceConfig, Timeframe
from crypto_trader.ingest.models import RawCandle


class CandleSource(Protocol):
    """Anything that can hand back raw candles for a symbol/timeframe."""

    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 200,
        end_time_ms: int | None = None,
    ) -> list[RawCandle]: ...


class BitunixCandleSource:
    """Fetches candles from Bitunix's public futures kline endpoint.

    Unauthenticated, public market data only.
    """

    def __init__(self, config: BitunixSourceConfig | None = None) -> None:
        self._config = config or BitunixSourceConfig()

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._config.symbols

    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 200,
        end_time_ms: int | None = None,
    ) -> list[RawCandle]:
        """Fetches up to `limit` candles at or before `end_time_ms`.

        `end_time_ms` is Bitunix's `endTime` kline param, confirmed working
        against the live public endpoint: passing the oldest open_time_ms seen so
        far lets a caller page backward past the endpoint's 200-row cap to reach
        deep history (see AGENTS.md's characterization-spike and Gate 1
        real-data-run entries). Omitted, it returns the most recent candles.
        """
        url = f"{self._config.base_url}{self._config.kline_path}"
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": timeframe.value,
            "limit": limit,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        response = requests.get(
            url, params=params, timeout=self._config.request_timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Bitunix kline request failed: {payload.get('msg', 'unknown error')}"
            )
        raw_rows = payload.get("data", [])
        return [
            RawCandle(
                symbol=symbol,
                open_time_ms=row.get("time"),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
                close=row.get("close"),
                volume=row.get("baseVol"),
                quote_volume=row.get("quoteVol"),
            )
            for row in raw_rows
        ]


class FixtureCandleSource:
    """Serves a fixed, pre-built list of RawCandle for tests and offline dev.

    The candles dict maps (symbol, timeframe) to the list of RawCandle that
    fetch_candles returns for that pair, in whatever order the fixture author
    chose: fixtures are expected to exercise out-of-order and malformed input on
    purpose.
    """

    def __init__(self, candles: dict[tuple[str, Timeframe], list[RawCandle]]) -> None:
        self._candles = candles

    def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 200,
        end_time_ms: int | None = None,
    ) -> list[RawCandle]:
        rows = self._candles.get((symbol, timeframe), [])
        if end_time_ms is not None:
            rows = [r for r in rows if r.open_time_ms <= end_time_ms]
        return rows[-limit:] if limit else list(rows)
