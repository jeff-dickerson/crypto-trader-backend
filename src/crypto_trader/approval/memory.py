"""InMemoryApprovalChannel: the fake the whole paper loop is testable against.

This channel needs no network and no bot token, so the entire approve-then-execute loop
(DryRun adapter, position manager, reconciliation) can be exercised end to end in a unit
test. It records every approval request and every notification for assertions, and its
decision policy is fully controllable:

- a default verdict (APPROVE / REJECT), or
- a queue of scripted decisions consumed in order (falling back to the default when empty), or
- a callable policy(plan) -> ApprovalDecision for data-driven decisions.

Fail-closed default: if you construct it with `default_verdict=REJECT`, an un-scripted plan
is rejected, matching the safety posture of a real channel that cannot reach the human.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from crypto_trader.approval.channel import (
    ApprovalChannel,
    ApprovalDecision,
    ApprovalVerdict,
    ChannelEvent,
)
from crypto_trader.paper.plan import TradePlan


class InMemoryApprovalChannel(ApprovalChannel):
    """A scriptable, recording approval channel for tests and offline development."""

    def __init__(
        self,
        *,
        default_verdict: ApprovalVerdict = ApprovalVerdict.APPROVE,
        policy: Callable[[TradePlan], ApprovalDecision] | None = None,
    ) -> None:
        self._default_verdict = default_verdict
        self._policy = policy
        self._scripted: deque[ApprovalDecision] = deque()
        # Public, append-only records for test assertions.
        self.requests: list[TradePlan] = []
        self.decisions: list[ApprovalDecision] = []
        self.events: list[ChannelEvent] = []

    def enqueue(self, *decisions: ApprovalDecision) -> None:
        """Script the next decisions, consumed in order by request_approval."""
        self._scripted.extend(decisions)

    def request_approval(self, plan: TradePlan) -> ApprovalDecision:
        self.requests.append(plan)
        if self._scripted:
            scripted = self._scripted.popleft()
            # Rebind the scripted decision to this plan id so a test can enqueue verdicts
            # without knowing the auto-generated plan id in advance.
            decision = ApprovalDecision(
                plan_id=plan.plan_id,
                verdict=scripted.verdict,
                modified_slider=scripted.modified_slider,
                note=scripted.note,
                actor=scripted.actor or "in-memory",
            )
        elif self._policy is not None:
            decision = self._policy(plan)
        else:
            decision = ApprovalDecision(
                plan_id=plan.plan_id, verdict=self._default_verdict, actor="in-memory"
            )
        self.decisions.append(decision)
        return decision

    def notify(self, event: ChannelEvent) -> None:
        self.events.append(event)

    def events_of(self, *kinds: object) -> list[ChannelEvent]:
        """All recorded events whose kind is one of `kinds` (test convenience)."""
        wanted = set(kinds)
        return [e for e in self.events if e.kind in wanted]
