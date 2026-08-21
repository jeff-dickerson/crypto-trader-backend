"""The position-state contract handed to generate_signal.

The design review flagged that a resting limit order at a zone boundary can PARTIALLY
fill, so position state is not a simple flat/in-position boolean. This module defines
the real contract now, so generate_signal's signature and the tests that validate it
reflect reality instead of a binary that would need a breaking change later.

This step does NOT implement order placement or fill tracking: that is the future
paper-loop and position-manager work. This module only defines the shape those future
components must populate and that generate_signal consumes. generate_signal never
mutates a PositionState (it is frozen); the caller threads an updated state into the
next call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PositionStatus(str, Enum):
    """The four position states generate_signal must distinguish.

    FLAT: no position and no resting entry order. Eligible for a new entry.
    PENDING: an entry limit is resting at the zone boundary but nothing has filled.
        There is no exposure yet, so management rules do not apply; generate_signal
        holds and waits for a fill (order cancel/replace is future paper-loop work).
    PARTIAL: the entry limit has partially filled. There IS exposure, so the
        management rules (trailing stop, momentum-shift exit) apply to the filled
        portion; filled_fraction is exposed for future size-aware decisions.
    OPEN: the entry is fully established. Management rules apply.
    """

    FLAT = "flat"
    PENDING = "pending"
    PARTIAL = "partial"
    OPEN = "open"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class PositionState:
    """Everything generate_signal needs to know about the current position.

    For FLAT the only meaningful field beyond `status` is `traded_zones`, the
    overtrading-governor memory: the entry boundaries of zones already traded. It must
    persist across a closed trade (back to FLAT) so a later retest of the same zone is
    rejected. This is the position/portfolio manager's bookkeeping in future work; the
    contract is defined here.

    For PARTIAL/OPEN the entry/stop/take-profit and the running favourable-excursion
    extreme drive the trailing-stop and momentum-shift management.
    """

    status: PositionStatus
    side: PositionSide | None = None

    # Filled fraction of the intended size: 0.0 while FLAT/PENDING, strictly between 0
    # and 1 while PARTIAL, 1.0 while OPEN. Exposed for future size-aware decisions.
    filled_fraction: float = 0.0

    # Average fill price (or the intended limit price while PENDING).
    entry_price: float | None = None
    # The stop price chosen at entry, before any trailing (the initial risk anchor).
    initial_stop: float | None = None
    take_profit: float | None = None

    # The stop currently resting on the exchange, which trailing may have ratcheted
    # tighter than initial_stop. None means "still at initial_stop".
    current_stop: float | None = None

    # Most-favourable-excursion price reached since entry: the highest high for a long,
    # the lowest low for a short. Drives trailing-stop activation and trail distance.
    extreme_price: float | None = None

    # The entry boundary this position was (or is being) opened against, so the caller
    # can add it to traded_zones once the trade is booked.
    zone_boundary: float | None = None

    # Overtrading-governor memory: boundaries of zones already traded. Persists across
    # a closed trade so the one-zone-one-trade rule survives a return to FLAT.
    traded_zones: tuple[float, ...] = field(default_factory=tuple)

    @classmethod
    def flat(cls, traded_zones: tuple[float, ...] = ()) -> PositionState:
        """A flat position, optionally carrying already-traded-zone memory."""
        return cls(status=PositionStatus.FLAT, traded_zones=tuple(traded_zones))

    @property
    def has_exposure(self) -> bool:
        """True when there is real exposure to manage (PARTIAL or OPEN)."""
        return self.status in (PositionStatus.PARTIAL, PositionStatus.OPEN)

    @property
    def resting_stop(self) -> float | None:
        """The stop actually resting now: the trailed stop if set, else the initial."""
        return self.current_stop if self.current_stop is not None else self.initial_stop
