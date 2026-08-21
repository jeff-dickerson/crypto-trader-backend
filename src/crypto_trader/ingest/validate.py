"""Ugly-data validation for ingested candles.

Ugly data is normal (governing principle 5): exchanges send duplicates, gaps,
out-of-order rows, malformed rows, and still-forming candles.
This module is the single place that turns a raw, untrusted stream of RawCandle
into a clean, ordered list of Candle, and it is deliberately strict: anything it
cannot make sense of is rejected and recorded as an issue rather than guessed at.

No-lookahead is enforced here, not downstream: a candle whose close_time has not
yet passed is never allowed into the accepted list, full stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle, RawCandle, utc_now


class IssueType(str, Enum):
    MALFORMED = "malformed"
    OUT_OF_ORDER = "out_of_order"
    DUPLICATE = "duplicate"
    TIMESTAMP_MISALIGNED = "timestamp_misaligned"
    STILL_FORMING = "still_forming"
    GAP = "gap"


@dataclass(frozen=True)
class ValidationIssue:
    """A record of one ugly-data event encountered during validation.

    Kept alongside the accepted candles so callers can log, alert, or feed a
    future journal table without the validator needing to know about logging.
    """

    type: IssueType
    symbol: str
    detail: str
    open_time_ms: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    accepted: list[Candle]
    issues: list[ValidationIssue]


def _coerce_number(value: object) -> float | None:
    """Best-effort numeric coercion that rejects, rather than guesses at, garbage.

    Accepts int/float directly and numeric strings (Bitunix returns OHLCV as
    strings); rejects None, NaN, inf, and anything else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _validate_shape(raw: RawCandle) -> tuple[dict[str, float | int], str | None]:
    """Checks one RawCandle's fields are present and numerically sane.

    Returns (parsed_fields, error) where error is None on success.
    Does not check ordering, duplication, alignment, or still-forming: those need
    the full sequence and current time, and are handled by validate_candles.
    """
    if not isinstance(raw.symbol, str) or not raw.symbol:
        return {}, "missing or invalid symbol"

    if not isinstance(raw.open_time_ms, int) or isinstance(raw.open_time_ms, bool):
        return {}, f"open_time_ms is not an int: {raw.open_time_ms!r}"
    if raw.open_time_ms <= 0:
        return {}, f"open_time_ms is not positive: {raw.open_time_ms!r}"

    numbers: dict[str, float | int] = {}
    for field_name, raw_value in (
        ("open", raw.open),
        ("high", raw.high),
        ("low", raw.low),
        ("close", raw.close),
        ("volume", raw.volume),
    ):
        parsed = _coerce_number(raw_value)
        if parsed is None:
            return {}, f"{field_name} is not a finite number: {raw_value!r}"
        numbers[field_name] = parsed

    if raw.quote_volume is not None:
        quote_parsed = _coerce_number(raw.quote_volume)
        if quote_parsed is None:
            return {}, f"quote_volume is not a finite number: {raw.quote_volume!r}"
        numbers["quote_volume"] = quote_parsed

    if numbers["high"] < numbers["low"]:
        return {}, f"high {numbers['high']} is below low {numbers['low']}"
    if not (numbers["low"] <= numbers["open"] <= numbers["high"]):
        return {}, f"open {numbers['open']} is outside [low, high]"
    if not (numbers["low"] <= numbers["close"] <= numbers["high"]):
        return {}, f"close {numbers['close']} is outside [low, high]"
    if numbers["volume"] < 0:
        return {}, f"volume is negative: {numbers['volume']}"
    numbers["open_time_ms"] = raw.open_time_ms

    return numbers, None


def validate_candles(
    raw_candles: list[RawCandle],
    symbol: str,
    timeframe: Timeframe,
    *,
    now: datetime | None = None,
) -> ValidationResult:
    """Validates a batch of RawCandle for one symbol/timeframe.

    Order of checks, applied in this sequence:
    1. Shape/malformed: reject rows with missing, non-numeric, or internally
       inconsistent OHLCV data.
    2. Out-of-order: flag when the raw feed did not hand rows back in ascending
       open_time order (a real ugly-data event worth recording, even though the
       validator still sorts before proceeding).
    3. Sort ascending by open_time.
    4. Duplicate: same open_time seen more than once, first occurrence wins.
    5. Timestamp sanity: open_time must land on the timeframe's UTC boundary and
       close_time is derived as open_time + timeframe duration.
    6. Still-forming exclusion: any candle whose close_time has not yet passed is
       dropped, never accepted, regardless of what the source claims about it.
    7. Gap detection: after the accepted list is built, any break between one
       candle's close_time and the next candle's open_time is recorded.
    """
    now = now or utc_now()
    issues: list[ValidationIssue] = []

    shaped: list[dict[str, float | int]] = []
    for raw in raw_candles:
        parsed, error = _validate_shape(raw)
        if error is not None:
            issues.append(
                ValidationIssue(
                    type=IssueType.MALFORMED,
                    symbol=symbol,
                    detail=error,
                    open_time_ms=raw.open_time_ms
                    if isinstance(raw.open_time_ms, int)
                    and not isinstance(raw.open_time_ms, bool)
                    else None,
                )
            )
            continue
        shaped.append(parsed)

    prev_open_time_ms: int | None = None
    for row in shaped:
        open_time_ms = int(row["open_time_ms"])
        if prev_open_time_ms is not None and open_time_ms < prev_open_time_ms:
            issues.append(
                ValidationIssue(
                    type=IssueType.OUT_OF_ORDER,
                    symbol=symbol,
                    detail=(
                        f"candle at {open_time_ms} arrived after candle at "
                        f"{prev_open_time_ms} in the raw feed"
                    ),
                    open_time_ms=open_time_ms,
                )
            )
        prev_open_time_ms = open_time_ms

    shaped.sort(key=lambda row: row["open_time_ms"])

    deduped: list[dict[str, float | int]] = []
    seen_open_times: set[int] = set()
    for row in shaped:
        open_time_ms = int(row["open_time_ms"])
        if open_time_ms in seen_open_times:
            issues.append(
                ValidationIssue(
                    type=IssueType.DUPLICATE,
                    symbol=symbol,
                    detail=f"duplicate open_time {open_time_ms}",
                    open_time_ms=open_time_ms,
                )
            )
            continue
        seen_open_times.add(open_time_ms)
        deduped.append(row)

    duration_ms = timeframe.duration_ms
    accepted: list[Candle] = []
    for row in deduped:
        open_time_ms = int(row["open_time_ms"])

        if open_time_ms % duration_ms != 0:
            issues.append(
                ValidationIssue(
                    type=IssueType.TIMESTAMP_MISALIGNED,
                    symbol=symbol,
                    detail=(
                        f"open_time {open_time_ms} is not aligned to a "
                        f"{timeframe.value} boundary anchored at UTC 00:00"
                    ),
                    open_time_ms=open_time_ms,
                )
            )
            continue

        close_time_ms = open_time_ms + duration_ms
        open_time = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
        close_time = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)

        if close_time > now:
            issues.append(
                ValidationIssue(
                    type=IssueType.STILL_FORMING,
                    symbol=symbol,
                    detail=(
                        f"candle open_time {open_time_ms} closes at "
                        f"{close_time.isoformat()}, which is after now "
                        f"({now.isoformat()}): excluded, never exposed"
                    ),
                    open_time_ms=open_time_ms,
                )
            )
            continue

        accepted.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                is_closed=True,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                quote_volume=(
                    float(row["quote_volume"]) if "quote_volume" in row else None
                ),
            )
        )

    for previous, current in zip(accepted, accepted[1:]):
        if current.open_time_ms != previous.close_time_ms:
            gap_ms = current.open_time_ms - previous.close_time_ms
            issues.append(
                ValidationIssue(
                    type=IssueType.GAP,
                    symbol=symbol,
                    detail=(
                        f"gap of {gap_ms} ms between close of candle at "
                        f"{previous.open_time_ms} and open of candle at "
                        f"{current.open_time_ms}"
                    ),
                    open_time_ms=current.open_time_ms,
                )
            )

    return ValidationResult(accepted=accepted, issues=issues)
