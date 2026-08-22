"""The ApprovalChannel ABC and its small vocabulary.

Why an ABC, not a Protocol (same reasoning as ExchangeAdapter, see
crypto_trader.exchange.adapter): this is a money-adjacent, multi-method boundary that
gates whether an order ever reaches the exchange. It is implemented by exactly two named
classes the position manager holds and calls (an in-memory fake and a Telegram surface),
so a nominal base that fails LOUDLY at construction when a method is missing is safer than
a Protocol that would fail silently at the first approval request. An approval bypass (an
order reaching the exchange without a recorded approval) is one of Gate 2's enumerated
critical failures (PRD 6.2), so the seam that prevents it is worth the strictness.

Two responsibilities:

1. request_approval(plan) -> ApprovalDecision: block until the human approves, rejects, or
   modifies a trade plan. The position manager submits an order ONLY on an APPROVE decision,
   and records the decision, so every submitted order traces to an approval.

2. notify(event): tell the human about a terminal or noteworthy outcome (a fill, a position
   exit, a reconciliation-drift freeze, and, reserved for Build Order step 5, a kill-switch
   firing). notify never blocks on a human and never returns a decision; it is one-way.

EventKind already reserves KILL_SWITCH_FIRED and KILL_SWITCH_REARMED so step 5 can emit them
through this same seam without a breaking change (task requirement): adding a member to an
enum and handling it in a channel is additive, whereas widening the method surface later
would not be.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from crypto_trader.paper.plan import TradePlan


class ApprovalVerdict(str, Enum):
    """What the human decided about a trade plan.

    APPROVE: submit the plan as sized.
    REJECT: do not submit; drop the plan.
    MODIFY: re-size the plan with a different risk slider, then ask again. Carries the new
        multiplier on the ApprovalDecision. This is the inline "Modify" button: the human
        adjusts risk rather than accepting or discarding outright.
    """

    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


@dataclass(frozen=True)
class ApprovalDecision:
    """A human's decision on one trade plan.

    `plan_id` ties the decision to the plan it answers, so a stale or mismatched callback
    can be rejected rather than acted on (approval-bypass defense). For a MODIFY verdict,
    `modified_slider` is the new risk multiplier to re-size with. `actor` records who decided
    (for Telegram, the allowlisted chat id as a string) for the audit trail.
    """

    plan_id: str
    verdict: ApprovalVerdict
    modified_slider: float | None = None
    note: str | None = None
    actor: str | None = None

    @property
    def is_approve(self) -> bool:
        return self.verdict is ApprovalVerdict.APPROVE


class EventKind(str, Enum):
    """The kind of a one-way channel notification.

    PLAN_VOIDED and PLAN_REJECTED explain why a signal did not become a live order.
    ORDER_SUBMITTED / ENTRY_FILLED / POSITION_EXITED track a trade through its life.
    RECONCILIATION_DRIFT / SYMBOL_FROZEN / SYMBOL_RESUMED cover the reconciler's alerts.
    KILL_SWITCH_FIRED / KILL_SWITCH_REARMED are RESERVED for Build Order step 5; they are
    declared now so the kill switch can use this same channel with no breaking change.
    """

    PLAN_VOIDED = "plan_voided"
    PLAN_REJECTED = "plan_rejected"
    ORDER_SUBMITTED = "order_submitted"
    ENTRY_FILLED = "entry_filled"
    POSITION_EXITED = "position_exited"
    RECONCILIATION_DRIFT = "reconciliation_drift"
    SYMBOL_FROZEN = "symbol_frozen"
    SYMBOL_RESUMED = "symbol_resumed"
    KILL_SWITCH_FIRED = "kill_switch_fired"  # reserved for step 5
    KILL_SWITCH_REARMED = "kill_switch_rearmed"  # reserved for step 5


@dataclass(frozen=True)
class ChannelEvent:
    """A one-way notification to the human. `detail` carries structured context for tests."""

    kind: EventKind
    symbol: str | None = None
    message: str = ""
    detail: dict[str, object] = field(default_factory=dict)


class ApprovalChannel(ABC):
    """The human-in-the-loop approval and notification seam.

    Implementations must record every approval decision they return (so an order can always
    be traced to an approval) and must never fabricate an APPROVE the human did not make. A
    channel that cannot reach the human must fail closed: return a REJECT-equivalent rather
    than a default APPROVE, because an approval bypass is a Gate 2 critical failure.
    """

    @abstractmethod
    def request_approval(self, plan: TradePlan) -> ApprovalDecision:
        """Present `plan` to the human and block until they approve, reject, or modify.

        Must return a decision whose `plan_id` matches `plan.plan_id`. On any inability to
        obtain a genuine human decision (timeout, transport failure, unauthenticated actor),
        return REJECT, never APPROVE: failing closed is the only safe default for a surface
        that authorizes real orders.
        """

    @abstractmethod
    def notify(self, event: ChannelEvent) -> None:
        """Send a one-way notification. Never blocks on a human, never returns a decision."""
