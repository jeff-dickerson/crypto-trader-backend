"""The outbound-call timeout wrapper (an in-process invariant from the plan).

The plan's in-process invariants: "no blocking I/O in any handler, a timeout wrapper on every
outbound call, backtest sweeps never run in-process." The only outbound calls a handler makes are
to the ExchangeAdapter (equity, positions, orders, rate-limit, order placement). `exchange_call`
is the one seam those go through: it bounds the call with a timeout and normalises both a timeout
and an ExchangeConnectionError into ApiError(EXCHANGE_UNREACHABLE), which the router renders as a
503. For the paper/DryRun adapter every call returns instantly, so the wrapper is effectively
free; for a live venue (step 6) it is what keeps a hung socket from blocking a handler forever.

The call runs in a daemon worker thread joined with the timeout: if the join times out the handler
returns 503 promptly while the worker is left to finish and be reaped, so a stuck exchange never
wedges the API thread. This is a guardrail at the API/adapter boundary, not a general async
runtime: handlers themselves do no other blocking I/O.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from crypto_trader.api.errors import ApiError, ErrorCode
from crypto_trader.exchange.adapter import ExchangeConnectionError

T = TypeVar("T")

DEFAULT_EXCHANGE_TIMEOUT_SECONDS = 5.0


def exchange_call(
    fn: Callable[[], T], *, timeout_s: float = DEFAULT_EXCHANGE_TIMEOUT_SECONDS
) -> T:
    """Run one outbound exchange call with a timeout; map failures to 503 (EXCHANGE_UNREACHABLE).

    Returns fn()'s result on success. Raises ApiError(EXCHANGE_UNREACHABLE) if the call exceeds
    `timeout_s` or raises ExchangeConnectionError. Any other exception propagates unchanged (the
    router maps it to a 500), because it is a bug, not an unreachable venue.
    """
    result: list[T] = []
    error: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 - captured and re-raised on the caller thread
            error.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise ApiError(
            ErrorCode.EXCHANGE_UNREACHABLE,
            f"exchange call timed out after {timeout_s:g}s",
        )
    if error:
        exc = error[0]
        if isinstance(exc, ExchangeConnectionError):
            raise ApiError(
                ErrorCode.EXCHANGE_UNREACHABLE, f"exchange unreachable: {exc}"
            ) from exc
        raise exc
    return result[0]
