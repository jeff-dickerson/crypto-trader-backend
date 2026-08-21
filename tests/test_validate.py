"""Unit tests for crypto_trader.ingest.validate: one test per ugly-data path.

Each test engineers exactly the pathology it names, per the task's definition of
done: duplicate, out-of-order, gap, still-forming, malformed/garbage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import RawCandle
from crypto_trader.ingest.validate import IssueType, validate_candles
from tests.fixtures.synthetic_candles import (
    FIXED_NOW,
    SYMBOL,
    clean_4h_sequence,
    malformed_candles,
    misaligned_candle,
    still_forming_candle,
    with_duplicate,
    with_gap,
    with_out_of_order,
)


def test_clean_sequence_is_fully_accepted_with_no_issues():
    candles = clean_4h_sequence(count=4)
    result = validate_candles(candles, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert len(result.accepted) == 4
    assert result.issues == []
    # ascending, no gaps
    for previous, current in zip(result.accepted, result.accepted[1:]):
        assert current.open_time_ms == previous.close_time_ms


def test_duplicate_candle_is_deduped_and_flagged():
    candles = with_duplicate(clean_4h_sequence(count=4))
    result = validate_candles(candles, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert len(result.accepted) == 4
    duplicate_issues = [i for i in result.issues if i.type == IssueType.DUPLICATE]
    assert len(duplicate_issues) == 1
    assert duplicate_issues[0].open_time_ms == candles[0].open_time_ms


def test_out_of_order_candle_is_reordered_and_flagged():
    candles = with_out_of_order(clean_4h_sequence(count=4))
    result = validate_candles(candles, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    out_of_order_issues = [i for i in result.issues if i.type == IssueType.OUT_OF_ORDER]
    assert len(out_of_order_issues) == 1
    # still ends up sorted ascending and fully accepted
    assert len(result.accepted) == 4
    for previous, current in zip(result.accepted, result.accepted[1:]):
        assert current.open_time_ms > previous.open_time_ms


def test_gap_between_candles_is_detected():
    candles = with_gap(clean_4h_sequence(count=5))
    result = validate_candles(candles, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    gap_issues = [i for i in result.issues if i.type == IssueType.GAP]
    assert len(gap_issues) == 1
    assert len(result.accepted) == 4


def test_still_forming_candle_is_excluded_never_accepted():
    forming = still_forming_candle(now=FIXED_NOW)
    result = validate_candles([forming], SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert result.accepted == []
    still_forming_issues = [i for i in result.issues if i.type == IssueType.STILL_FORMING]
    assert len(still_forming_issues) == 1


def test_still_forming_boundary_candle_that_just_closed_is_accepted():
    # A candle opening exactly 4h before FIXED_NOW closes exactly at FIXED_NOW:
    # not still forming, must be accepted (no-lookahead excludes only strictly
    # not-yet-closed candles, not the one that just closed).
    start = FIXED_NOW.replace(hour=0)
    just_closed = clean_4h_sequence(count=1, start=start)[0]
    result = validate_candles([just_closed], SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert len(result.accepted) == 1
    assert result.accepted[0].is_closed is True


def test_malformed_candles_are_all_rejected_and_flagged():
    candles = malformed_candles()
    result = validate_candles(candles, SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert result.accepted == []
    malformed_issues = [i for i in result.issues if i.type == IssueType.MALFORMED]
    assert len(malformed_issues) == len(candles)


def test_misaligned_timestamp_is_rejected_and_flagged():
    candle = misaligned_candle()
    result = validate_candles([candle], SYMBOL, Timeframe.H4, now=FIXED_NOW)

    assert result.accepted == []
    misaligned_issues = [
        i for i in result.issues if i.type == IssueType.TIMESTAMP_MISALIGNED
    ]
    assert len(misaligned_issues) == 1


def test_daily_timeframe_uses_daily_boundary_and_duration():
    open_time = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    open_time_ms = int(open_time.timestamp() * 1000)

    candle = RawCandle(
        symbol=SYMBOL,
        open_time_ms=open_time_ms,
        open=100,
        high=110,
        low=90,
        close=105,
        volume=10,
    )
    result = validate_candles(
        [candle],
        SYMBOL,
        Timeframe.D1,
        now=datetime(2026, 1, 6, 0, 0, 1, tzinfo=timezone.utc),
    )

    assert len(result.accepted) == 1
    accepted = result.accepted[0]
    assert accepted.close_time_ms - accepted.open_time_ms == Timeframe.D1.duration_ms
