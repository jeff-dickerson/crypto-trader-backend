"""Candle data models.

RawCandle is the loosely-typed shape a data source hands to the validator: fields
may be missing, wrong-typed, or otherwise garbage, and the validator's job is to
reject or repair that before anything becomes a Candle.
Candle is the strict, validated shape that is the only thing ever written to the
database or handed to a downstream consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from crypto_trader.config import Timeframe


@dataclass(frozen=True)
class RawCandle:
    """An unvalidated candle as received from a data source.

    open_time_ms is the candle's open-time in epoch milliseconds, expected to be an
    int but not yet checked.
    All other fields are Any: a source may hand back strings, None, or garbage, and
    validation is exactly the job of crypto_trader.ingest.validate.
    """

    symbol: str
    open_time_ms: Any
    open: Any
    high: Any
    low: Any
    close: Any
    volume: Any
    quote_volume: Any = None


@dataclass(frozen=True)
class Candle:
    """A validated, closed candle: the only shape ever persisted or consumed.

    open_time and close_time are timezone-aware UTC datetimes.
    is_closed is always True for a Candle that made it through validation: a
    still-forming candle is rejected before it ever becomes one of these, per the
    no-lookahead principle.
    The column still exists in storage so a consumer can never accidentally
    misinterpret a row's completeness, but no row with is_closed = 0 is ever
    written by this ingest pipeline.
    """

    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    is_closed: bool
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float | None

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("Candle open_time and close_time must be timezone-aware")

    @property
    def open_time_ms(self) -> int:
        return int(self.open_time.timestamp() * 1000)

    @property
    def close_time_ms(self) -> int:
        return int(self.close_time.timestamp() * 1000)


def utc_now() -> datetime:
    """Single choke point for "now", so tests can monkeypatch it cleanly."""
    return datetime.now(timezone.utc)
