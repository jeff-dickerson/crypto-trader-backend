"""PersistenceSink: the write seam the paper loop uses to record what it does.

The REST API's read endpoints (/signals, /journal, /approvals, /dashboard, /terminal) need
real rows, so the paper machine must persist as it runs: every entry signal, every trade plan
and its status transitions, every approval decision, every closed trade, every kill-switch
event. Rather than couple the position manager and the loop directly to SQLite or to the API
layer (which would invert the dependency: api depends on paper, never the reverse), they write
through this small seam.

The base class here is a NO-OP: every pre-existing call site that constructs a PositionManager,
KillSwitchMonitor, or PaperTrader without a sink keeps its exact prior behaviour, and every
existing test passes unchanged. The API layer supplies a real implementation
(`crypto_trader.api.store.ApiStore`) that writes to the unified SQLite database through the
single-writer connection. The methods take the domain objects the paper layer already owns
(a Signal, a TradePlan, a ClosedPaperTrade) so a sink implementation reads settled fields rather
than re-deriving them.

Single-writer invariant: the API process and the bot loop are the same process (hosting
decision 5), so writing through this sink introduces no second writer (AGENTS.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle; these are type hints only
    from crypto_trader.paper.plan import PlanStatus, TradePlan
    from crypto_trader.paper.position_manager import ClosedPaperTrade
    from crypto_trader.strategy.signal import Signal


class PersistenceSink:
    """A no-op write seam. Override the methods that matter; unused ones stay no-ops.

    Every method is deliberately total (never raises for the base) so a caller can invoke it
    unconditionally without a `sink is not None` guard at each site.
    """

    def record_signal(self, signal: Signal, *, symbol: str, at: datetime) -> int | None:
        """Record one strategy signal (entry or exit). Returns the new row id, or None."""
        return None

    def insert_plan(
        self, plan: TradePlan, *, at: datetime, signal_id: int | None = None
    ) -> None:
        """Persist a newly created TradePlan (PROPOSED or VOID) linked to its signal."""

    def update_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        at: datetime,
        void_reason: str | None = None,
    ) -> None:
        """Move a persisted plan to a new status (approved/rejected/submitted/failed/etc.)."""

    def record_plan_decision(
        self,
        plan_id: str,
        verdict: str,
        *,
        at: datetime,
        actor: str | None = None,
        note: str | None = None,
    ) -> None:
        """Append one operator approval decision to the audit trail."""

    def record_closed_trade(self, trade: ClosedPaperTrade, *, at: datetime) -> None:
        """Persist one fully-closed paper trade for the journal."""

    def record_kill_switch_event(
        self,
        *,
        source: str,
        outcome: str,
        at: datetime,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append one kill-switch audit row (trigger source, outcome, structured detail)."""


NULL_SINK = PersistenceSink()
