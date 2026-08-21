"""Synthetic RawCandle builders for tests.

Every builder here produces deliberately engineered candle data: normal clean
sequences, and specific ugly-data pathologies (duplicate, out-of-order, gap,
still-forming, malformed).
Nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import RawCandle

SYMBOL = "BTCUSDT"

# A fixed "now" used across tests so still-forming behavior is deterministic.
# 2026-01-08T04:00:00Z: exactly on a 4H boundary, so the candle opening at
# 2026-01-08T00:00:00Z (closing at 04:00:00Z) is exactly closed, and the candle
# opening at 04:00:00Z (closing at 08:00:00Z) is still forming.
FIXED_NOW = datetime(2026, 1, 8, 4, 0, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def clean_4h_sequence(count: int = 6, start: datetime | None = None) -> list[RawCandle]:
    """A clean, ascending, gap-free run of 4H candles ending before FIXED_NOW."""
    start = start or datetime(2026, 1, 7, 4, 0, 0, tzinfo=timezone.utc)
    candles = []
    price = 50000.0
    for i in range(count):
        open_time_ms = _ms(start) + i * Timeframe.H4.duration_ms
        candles.append(
            RawCandle(
                symbol=SYMBOL,
                open_time_ms=open_time_ms,
                open=price + i,
                high=price + i + 10,
                low=price + i - 10,
                close=price + i + 5,
                volume=100.0 + i,
                quote_volume=1_000_000.0 + i,
            )
        )
    return candles


def with_duplicate(candles: list[RawCandle]) -> list[RawCandle]:
    """Repeats the first candle to trigger duplicate detection."""
    if not candles:
        return candles
    return [candles[0], *candles]


def with_out_of_order(candles: list[RawCandle]) -> list[RawCandle]:
    """Swaps the first two candles so the raw feed is not ascending."""
    if len(candles) < 2:
        return candles
    swapped = list(candles)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    return swapped


def with_gap(candles: list[RawCandle]) -> list[RawCandle]:
    """Removes the middle candle to leave a detectable gap."""
    if len(candles) < 3:
        return candles
    mid = len(candles) // 2
    return candles[:mid] + candles[mid + 1 :]


def still_forming_candle(now: datetime = FIXED_NOW) -> RawCandle:
    """A single 4H candle whose close_time is after `now`: must be excluded."""
    open_time_ms = _ms(now)  # opens exactly at now, so it closes 4h after now
    return RawCandle(
        symbol=SYMBOL,
        open_time_ms=open_time_ms,
        open=51000.0,
        high=51100.0,
        low=50900.0,
        close=51050.0,
        volume=42.0,
        quote_volume=2_100_000.0,
    )


def malformed_candles() -> list[RawCandle]:
    """A grab-bag of garbage rows, one pathology each."""
    base_open_time_ms = _ms(datetime(2026, 1, 7, 8, 0, 0, tzinfo=timezone.utc))
    return [
        # non-numeric open_time
        RawCandle(
            symbol=SYMBOL,
            open_time_ms="not-a-timestamp",
            open=1,
            high=2,
            low=0,
            close=1,
            volume=1,
        ),
        # None price
        RawCandle(
            symbol=SYMBOL,
            open_time_ms=base_open_time_ms,
            open=None,
            high=2,
            low=0,
            close=1,
            volume=1,
        ),
        # high below low
        RawCandle(
            symbol=SYMBOL,
            open_time_ms=base_open_time_ms + Timeframe.H4.duration_ms,
            open=1,
            high=0,
            low=5,
            close=1,
            volume=1,
        ),
        # negative volume
        RawCandle(
            symbol=SYMBOL,
            open_time_ms=base_open_time_ms + 2 * Timeframe.H4.duration_ms,
            open=1,
            high=5,
            low=0,
            close=1,
            volume=-1,
        ),
        # non-numeric close (garbage string)
        RawCandle(
            symbol=SYMBOL,
            open_time_ms=base_open_time_ms + 3 * Timeframe.H4.duration_ms,
            open=1,
            high=5,
            low=0,
            close="garbage",
            volume=1,
        ),
        # empty symbol
        RawCandle(
            symbol="",
            open_time_ms=base_open_time_ms + 4 * Timeframe.H4.duration_ms,
            open=1,
            high=5,
            low=0,
            close=1,
            volume=1,
        ),
    ]


def misaligned_candle() -> RawCandle:
    """A candle whose open_time does not fall on a 4H UTC boundary."""
    misaligned_dt = datetime(2026, 1, 7, 5, 17, 0, tzinfo=timezone.utc)
    return RawCandle(
        symbol=SYMBOL,
        open_time_ms=_ms(misaligned_dt),
        open=1,
        high=2,
        low=0,
        close=1,
        volume=1,
    )
