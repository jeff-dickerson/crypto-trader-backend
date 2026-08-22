"""Orchestrates fetch, validate, and store for one symbol/timeframe.

This is the only place that writes candle rows to the database, keeping the
single-writer invariant easy to reason about: nothing else in this codebase
issues an INSERT against candles_4h or candles_1d.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle, utc_now
from crypto_trader.ingest.source import CandleSource
from crypto_trader.ingest.validate import ValidationResult, validate_candles

_TABLE_BY_TIMEFRAME = {
    Timeframe.H4: "candles_4h",
    Timeframe.D1: "candles_1d",
}


def ingest_symbol(
    conn: sqlite3.Connection,
    source: CandleSource,
    symbol: str,
    timeframe: Timeframe,
    *,
    limit: int = 200,
    end_time_ms: int | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """Fetches, validates, and stores candles for one symbol/timeframe.

    Storage is idempotent: re-ingesting a candle already on disk with an
    identical (symbol, open_time) is a no-op via INSERT OR IGNORE, so re-running
    ingest is always safe.
    `end_time_ms`, when given, pages backward from that point (see
    CandleSource.fetch_candles) instead of fetching the most recent candles; a
    caller pulling deep history calls this repeatedly, walking end_time_ms back.
    Returns the ValidationResult (accepted candles plus every ugly-data issue
    found) so a caller can log or journal it.
    """
    raw_candles = source.fetch_candles(
        symbol, timeframe, limit=limit, end_time_ms=end_time_ms
    )
    result = validate_candles(raw_candles, symbol, timeframe, now=now or utc_now())
    store_candles(conn, result.accepted, timeframe)
    return result


def store_candles(
    conn: sqlite3.Connection, candles: list[Candle], timeframe: Timeframe
) -> int:
    """Writes validated, closed candles to the appropriate table.

    Only ever called with the output of validate_candles: never call this with
    unvalidated data.
    Returns the number of rows actually inserted (duplicates already on disk are
    silently skipped, not counted as an error: that is expected on re-ingest).
    """
    table = _TABLE_BY_TIMEFRAME[timeframe]
    ingested_at_ms = int(utc_now().timestamp() * 1000)

    cursor = conn.executemany(
        f"""
        INSERT OR IGNORE INTO {table}
            (symbol, open_time, close_time, is_closed, open, high, low, close,
             volume, quote_volume, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                candle.symbol,
                candle.open_time_ms,
                candle.close_time_ms,
                int(candle.is_closed),
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.quote_volume,
                ingested_at_ms,
            )
            for candle in candles
        ],
    )
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
