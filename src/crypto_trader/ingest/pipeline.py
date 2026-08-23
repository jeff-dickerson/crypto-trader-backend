"""Orchestrates fetch, validate, and store for one symbol/timeframe.

This is the only place that writes candle rows to the database, keeping the
single-writer invariant easy to reason about: nothing else in this codebase
issues an INSERT against candles_4h or candles_1d.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle, utc_now
from crypto_trader.ingest.source import CandleSource
from crypto_trader.ingest.validate import ValidationResult, validate_candles

_TABLE_BY_TIMEFRAME = {
    Timeframe.H4: "candles_4h",
    Timeframe.D1: "candles_1d",
}


@dataclass(frozen=True)
class HistoryIngestResult:
    """Summary of a multi-page backward-history ingest for one symbol/timeframe."""

    symbol: str
    timeframe: Timeframe
    pages_fetched: int
    total_accepted: int
    total_issues: int
    oldest_open_time_ms: int | None
    newest_open_time_ms: int | None
    reached_listing_start: bool


def _coerce_ms(value: object) -> int | None:
    """Coerce a raw open_time to int ms, tolerating the string form Bitunix/Binance send."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


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


def ingest_history(
    conn: sqlite3.Connection,
    source: CandleSource,
    symbol: str,
    timeframe: Timeframe,
    *,
    page_limit: int = 200,
    earliest_ms: int | None = None,
    max_pages: int = 200,
    now: datetime | None = None,
) -> HistoryIngestResult:
    """Page backward through a source to ingest deep history for one symbol/timeframe.

    Repeatedly calls ``ingest_symbol`` with a walking ``end_time_ms``, newest page first,
    stepping back to just before the oldest raw row on each page. It stops when a page comes
    back shorter than ``page_limit`` (the source has reached the symbol's listing start),
    when the oldest row reaches ``earliest_ms``, or when ``max_pages`` is hit (a safety
    bound against an endpoint that never signals the start).

    Pagination is driven by the RAW page size and the raw oldest timestamp, never by the
    post-validation accepted count: a malformed or duplicate row can shrink the accepted
    count below a full page, and treating that as "reached the start" would truncate history
    early (see AGENTS.md, Gate 1 real-data-run learnings). Storage stays idempotent, so a
    re-run is safe.
    """
    now = now or utc_now()
    end_time_ms: int | None = None  # first page: most recent, no endTime
    pages = 0
    total_accepted = 0
    total_issues = 0
    oldest: int | None = None
    newest: int | None = None
    reached_start = False

    while pages < max_pages:
        raw = source.fetch_candles(
            symbol, timeframe, limit=page_limit, end_time_ms=end_time_ms
        )
        if not raw:
            reached_start = True
            break

        result = validate_candles(raw, symbol, timeframe, now=now)
        store_candles(conn, result.accepted, timeframe)
        total_accepted += len(result.accepted)
        total_issues += len(result.issues)
        pages += 1

        raw_times = [t for t in (_coerce_ms(r.open_time_ms) for r in raw) if t is not None]
        if not raw_times:
            # No usable timestamp on this page: cannot advance safely, so stop rather
            # than risk an endless loop on a garbage page.
            break
        page_oldest = min(raw_times)
        page_newest = max(raw_times)
        oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        newest = page_newest if newest is None else max(newest, page_newest)

        if len(raw) < page_limit:
            reached_start = True
            break
        if earliest_ms is not None and page_oldest <= earliest_ms:
            break
        end_time_ms = page_oldest - 1

    return HistoryIngestResult(
        symbol=symbol,
        timeframe=timeframe,
        pages_fetched=pages,
        total_accepted=total_accepted,
        total_issues=total_issues,
        oldest_open_time_ms=oldest,
        newest_open_time_ms=newest,
        reached_listing_start=reached_start,
    )


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
