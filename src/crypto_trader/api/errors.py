"""The API error envelope and its machine-readable codes.

Every non-2xx response is the same shape (the plan's contract):

    {"error": {"code": "PLAN_NOT_PROPOSABLE", "message": "...", "detail": {...}}}

`code` is a machine-readable enum a UI can branch on; `message` is human text; `detail` carries
structured context (for example the current plan status on a 409). The HTTP mapping stays small:
400 validation, 404 not found, 409 state conflict, 500 internal, 503 exchange unreachable.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Machine-readable error codes, each pinned to one HTTP status via ``HTTP_STATUS`` below."""

    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PLAN_NOT_PROPOSABLE = "PLAN_NOT_PROPOSABLE"
    KILL_SWITCH_CONFLICT = "KILL_SWITCH_CONFLICT"
    INTERNAL = "INTERNAL"
    EXCHANGE_UNREACHABLE = "EXCHANGE_UNREACHABLE"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PLAN_NOT_FOUND: 404,
    ErrorCode.PLAN_NOT_PROPOSABLE: 409,
    ErrorCode.KILL_SWITCH_CONFLICT: 409,
    ErrorCode.INTERNAL: 500,
    ErrorCode.EXCHANGE_UNREACHABLE: 503,
}


class ApiError(Exception):
    """An error the router renders as the standard envelope with the code's HTTP status."""

    def __init__(
        self, code: ErrorCode, message: str, *, detail: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_envelope(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "detail": self.detail,
            }
        }
