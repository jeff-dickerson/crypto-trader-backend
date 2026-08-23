"""Tests for the backward-pagination history ingest, offline via FixtureCandleSource."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from crypto_trader.config import Timeframe
from crypto_trader.ingest.pipeline import ingest_history
from crypto_trader.ingest.source import FixtureCandleSource
from tests.fixtures.synthetic_candles import FIXED_NOW, SYMBOL, clean_4h_sequence

# Start early enough that every candle in these sequences is closed before FIXED_NOW,
# so none is dropped as still-forming and the pagination counts are exact.
_EARLY_START = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_ingest_history_pages_back_to_listing_start(db_conn: sqlite3.Connection):
    # 10 candles, pages of 4: expect pages of 4, 4, 2 -> reached listing start.
    candles = clean_4h_sequence(count=10, start=_EARLY_START)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    result = ingest_history(
        db_conn, source, SYMBOL, Timeframe.H4, page_limit=4, now=FIXED_NOW
    )

    assert result.total_accepted == 10
    assert result.pages_fetched == 3
    assert result.reached_listing_start is True
    assert result.oldest_open_time_ms == candles[0].open_time_ms
    assert result.newest_open_time_ms == candles[-1].open_time_ms
    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 10


def test_ingest_history_stops_at_earliest_ms(db_conn: sqlite3.Connection):
    candles = clean_4h_sequence(count=12, start=_EARLY_START)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})
    # Stop once we have paged back to at least candle index 6.
    earliest = candles[6].open_time_ms

    result = ingest_history(
        db_conn,
        source,
        SYMBOL,
        Timeframe.H4,
        page_limit=3,
        earliest_ms=earliest,
        now=FIXED_NOW,
    )

    assert result.oldest_open_time_ms is not None
    assert result.oldest_open_time_ms <= earliest
    # It should not have walked all the way to the very first candle.
    assert result.oldest_open_time_ms >= candles[0].open_time_ms


def test_ingest_history_respects_max_pages(db_conn: sqlite3.Connection):
    candles = clean_4h_sequence(count=20, start=_EARLY_START)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    result = ingest_history(
        db_conn, source, SYMBOL, Timeframe.H4, page_limit=2, max_pages=3, now=FIXED_NOW
    )

    assert result.pages_fetched == 3
    assert result.reached_listing_start is False
    # Only 3 pages of 2 stored, not the full 20.
    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 6


def test_ingest_history_idempotent_on_rerun(db_conn: sqlite3.Connection):
    candles = clean_4h_sequence(count=8, start=_EARLY_START)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    ingest_history(db_conn, source, SYMBOL, Timeframe.H4, page_limit=3, now=FIXED_NOW)
    ingest_history(db_conn, source, SYMBOL, Timeframe.H4, page_limit=3, now=FIXED_NOW)

    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 8
