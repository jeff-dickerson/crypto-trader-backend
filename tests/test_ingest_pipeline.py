"""End-to-end tests for the ingest pipeline, using FixtureCandleSource only.

No network access: crypto_trader.ingest.source.BitunixCandleSource is never
instantiated here.
"""

from __future__ import annotations

import sqlite3

from crypto_trader.config import Timeframe
from crypto_trader.ingest.pipeline import ingest_symbol
from crypto_trader.ingest.source import FixtureCandleSource
from tests.fixtures.synthetic_candles import (
    FIXED_NOW,
    SYMBOL,
    clean_4h_sequence,
    still_forming_candle,
    with_duplicate,
)


def test_ingest_symbol_stores_only_accepted_candles(db_conn: sqlite3.Connection):
    candles = clean_4h_sequence(count=5)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    result = ingest_symbol(db_conn, source, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert len(result.accepted) == 5
    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 5


def test_ingest_symbol_never_stores_a_still_forming_candle(
    db_conn: sqlite3.Connection,
):
    candles = clean_4h_sequence(count=3) + [still_forming_candle(now=FIXED_NOW)]
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    ingest_symbol(db_conn, source, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    max_close_time = db_conn.execute(
        "SELECT MAX(close_time) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    now_ms = int(FIXED_NOW.timestamp() * 1000)
    assert max_close_time <= now_ms

    unclosed_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE is_closed = 0"
    ).fetchone()[0]
    assert unclosed_count == 0


def test_re_ingesting_the_same_candles_is_idempotent(db_conn: sqlite3.Connection):
    candles = clean_4h_sequence(count=4)
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    ingest_symbol(db_conn, source, SYMBOL, Timeframe.H4, now=FIXED_NOW)
    ingest_symbol(db_conn, source, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 4


def test_ingest_reports_issues_for_duplicate_input(db_conn: sqlite3.Connection):
    candles = with_duplicate(clean_4h_sequence(count=3))
    source = FixtureCandleSource({(SYMBOL, Timeframe.H4): candles})

    result = ingest_symbol(db_conn, source, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert len(result.issues) == 1
    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM candles_4h WHERE symbol = ?", (SYMBOL,)
    ).fetchone()[0]
    assert row_count == 3
