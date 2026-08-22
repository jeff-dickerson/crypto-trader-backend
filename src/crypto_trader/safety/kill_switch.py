"""KillSwitch: the armed/triggered state machine (PRD 9.1).

Per PRD 9.1 the kill switch, on any trigger: cancels orders, closes at market, halts signal
generation, and requires a re-arm before trading resumes. This class is the minimal state that
those four obligations share, so KillSwitchMonitor (monitor.py) and PositionManager
(paper/position_manager.py) both check the same object rather than duplicating "are we halted"
logic.

Two separate booleans, deliberately not one:

- `is_armed`: False the instant `trigger()` is called. This is the "halts signal generation" and
  "requires re-arm" obligation, and it takes effect IMMEDIATELY, independent of whether the
  exchange-side flatten below succeeds. A degraded exchange (the open risk PRD 9.1 records,
  resolved here and in monitor.py) must never be able to block the halt.
- `flatten_confirmed`: False from the moment of trigger() until the exchange has genuinely
  confirmed no open positions or orders remain. This is the separate, harder guarantee behind
  "do not report killed until the exchange actually confirms flat" (the degraded-mode fallback
  requirement): a caller must check this, not just `is_armed`, before treating the kill switch's
  "closes at market" obligation as satisfied. Until then, the on-exchange resting stops (placed
  when each position was opened, principle 4) remain the safety floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class KillSwitchReason(str, Enum):
    """Why the kill switch fired. Matches the four PRD 9.1 auto-triggers plus a manual trigger."""

    DAILY_LOSS = "daily_loss"
    MAX_DRAWDOWN = "max_drawdown"
    API_FAILURES = "api_failures"
    WEBSOCKET_DEAD = "websocket_dead"  # not wired for the paper adapter; see monitor.py
    MANUAL = "manual"


@dataclass(frozen=True)
class KillSwitchEvent:
    """One trigger occurrence: the reason, a human-readable message, and structured detail."""

    reason: KillSwitchReason
    message: str
    triggered_at: datetime
    detail: dict[str, object] = field(default_factory=dict)


class KillSwitch:
    """The armed/triggered state every new-entry check and every flatten-retry consults.

    Starts armed. `trigger()` halts immediately; `mark_flatten_confirmed()` is called only once
    exchange truth confirms flat (see monitor.py's degraded-mode retry loop); `rearm()` is the
    operator action that resumes trading (a future Telegram `/rearm` command is out of this
    task's scope, per the brief: this builds the component, not that surface).
    """

    def __init__(self) -> None:
        self._armed = True
        self._last_event: KillSwitchEvent | None = None
        self._flatten_confirmed = True  # nothing to flatten while armed

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def flatten_confirmed(self) -> bool:
        return self._flatten_confirmed

    @property
    def last_event(self) -> KillSwitchEvent | None:
        return self._last_event

    def trigger(
        self,
        reason: KillSwitchReason,
        message: str,
        *,
        now: datetime,
        detail: dict[str, object] | None = None,
    ) -> KillSwitchEvent:
        """Halt immediately. Idempotent: a second trigger while already halted still records the
        newest reason (useful if a different auto-trigger fires during an unresolved degraded
        flatten) but does not re-open a flatten that already confirmed."""
        event = KillSwitchEvent(
            reason=reason, message=message, triggered_at=now, detail=detail or {}
        )
        self._armed = False
        if self._flatten_confirmed:
            self._flatten_confirmed = False
        self._last_event = event
        return event

    def mark_flatten_confirmed(self) -> None:
        """Called only once the exchange has genuinely confirmed no exposure remains."""
        self._flatten_confirmed = True

    def rearm(self) -> None:
        """Resume trading: re-arm, and clear the trigger record."""
        self._armed = True
        self._flatten_confirmed = True
        self._last_event = None
