"""Cursor pagination for the append-heavy histories (/signals and /journal only).

The plan restricts pagination to the two append-heavy logs; the live views (dashboard, approvals,
terminal, risk, kill-switch) stay unpaginated. The cursor is opaque to clients: it encodes the id
of the last row returned, and because both `signals` and `closed_trades` use a monotonically
increasing AUTOINCREMENT id, "give me rows with id < cursor, newest first" is a stable keyset scan
that never skips or repeats a row as new rows are appended. `limit` defaults to 50 and is clamped
to at most 200 (the plan's cap).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from crypto_trader.api.errors import ApiError, ErrorCode

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class PageRequest:
    """A validated pagination request: how many rows, and the id to page back from (exclusive)."""

    limit: int
    before_id: int | None


def parse_page_request(
    raw_limit: str | None, raw_cursor: str | None
) -> PageRequest:
    """Parse and validate ``?limit=&cursor=`` query values into a PageRequest.

    Raises ApiError(VALIDATION) on a non-integer limit, a limit outside [1, 200], or a malformed
    cursor, so a client gets a 400 with a clear message rather than a silently ignored parameter.
    """
    limit = DEFAULT_LIMIT
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ApiError(
                ErrorCode.VALIDATION, f"limit must be an integer, got {raw_limit!r}"
            ) from None
        if limit < 1 or limit > MAX_LIMIT:
            raise ApiError(
                ErrorCode.VALIDATION, f"limit must be between 1 and {MAX_LIMIT}, got {limit}"
            )

    before_id = decode_cursor(raw_cursor) if raw_cursor else None
    return PageRequest(limit=limit, before_id=before_id)


def encode_cursor(row_id: int) -> str:
    """Encode a row id as an opaque, URL-safe cursor string."""
    return base64.urlsafe_b64encode(str(row_id).encode("ascii")).decode("ascii")


def decode_cursor(cursor: str) -> int:
    """Decode a cursor back to a row id, raising ApiError(VALIDATION) if it is malformed."""
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, TypeError, binascii.Error):
        raise ApiError(ErrorCode.VALIDATION, f"malformed cursor: {cursor!r}") from None
