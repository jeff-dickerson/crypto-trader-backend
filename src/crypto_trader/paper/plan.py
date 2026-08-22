"""The TradePlan: the unit a human approves before anything reaches the exchange.

A TradePlan is produced by the risk sizer (risk.py) from a strategy entry signal plus
current account equity. It is the single object the approval channel shows the human and
the position manager submits once approved. It is deliberately a plain, frozen record:
the risk sizing has already happened by the time a plan exists, so every downstream
consumer reads settled numbers rather than recomputing tier logic.

Two design points matter here:

1. Plan-time affordability (PRD 4.2 slider honesty). A plan can be VOID before it is ever
   shown for approval, carrying a human-readable reason (for example "below the exchange
   minimum order size"). A voided plan is never silently dropped: it is surfaced with its
   reason so the operator sees why an otherwise-valid signal did not become an order.

2. One clear risk number (conduct rules 5 and 8: UI encapsulates complexity). The approval
   surface shows exactly one risk figure, "Risk: 4% tier, $18.40 on this trade", with no
   tier tables, slider math, or clamp arithmetic leaking into the human's view. The
   `risk_line` property is that single formatted number; the tier/slider/clamp fields exist
   for logs and tests, not for the approval card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from crypto_trader.strategy.position import PositionSide


class PlanStatus(str, Enum):
    """Where a plan sits in its short lifecycle.

    PROPOSED: sized and affordable, awaiting approval.
    VOID: rejected at plan time (min-notional or a sizing failure); never submitted.
    APPROVED: a human approved it; the position manager will submit it.
    REJECTED: a human rejected it; it will not be submitted.
    SUBMITTED: the entry order has been placed on the exchange.
    """

    PROPOSED = "proposed"
    VOID = "void"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"


@dataclass(frozen=True)
class TradePlan:
    """A sized, human-approvable trade proposal.

    Prices are strategy-derived (entry at the value-area edge, stop in the adjacent LVN,
    take-profit before the opposing HVN). `quantity` is the risk-sized, precision-floored
    order size the exchange would actually receive. `risk_amount` is the currency at risk
    if the initial stop is hit (equity * effective_risk_fraction), and it is the one number
    the human sees.

    `tier_risk_fraction` is the PRD 4.1 tier base for the current equity (the "4% tier"
    label), while `effective_risk_fraction` is what was actually used after the slider and
    the kill-switch clamp. `slider_capped` records whether the kill-switch clamp bit, so a
    log or a modify flow can explain why a high slider did not translate into more risk;
    the approval card never shows this.
    """

    plan_id: str
    symbol: str
    side: PositionSide
    entry_price: float
    stop_price: float
    take_profit_price: float
    quantity: float
    notional: float
    equity: float
    tier_risk_fraction: float
    slider_multiplier: float
    effective_risk_fraction: float
    risk_amount: float
    slider_capped: bool = False
    status: PlanStatus = PlanStatus.PROPOSED
    void_reason: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_void(self) -> bool:
        return self.status is PlanStatus.VOID

    @property
    def risk_distance(self) -> float:
        """Currency distance from entry to the initial stop (one R, per unit)."""
        return abs(self.entry_price - self.stop_price)

    @property
    def risk_line(self) -> str:
        """The single human-facing risk number for the approval card.

        Example: "Risk: 4% tier, $18.40 on this trade". No tier table, no slider or clamp
        arithmetic: the complexity is encapsulated (conduct rule 8).
        """
        tier_pct = self.tier_risk_fraction * 100.0
        # Show the tier as a whole percent when it is one (4%, 3%, 2%), else one decimal.
        tier_str = (
            f"{tier_pct:.0f}%" if abs(tier_pct - round(tier_pct)) < 1e-9 else f"{tier_pct:.1f}%"
        )
        return f"Risk: {tier_str} tier, ${self.risk_amount:,.2f} on this trade"

    def voided(self, reason: str) -> "TradePlan":
        """Return a copy marked VOID with a human-readable reason."""
        return _replace(self, status=PlanStatus.VOID, void_reason=reason)

    def approved(self) -> "TradePlan":
        return _replace(self, status=PlanStatus.APPROVED)

    def rejected(self) -> "TradePlan":
        return _replace(self, status=PlanStatus.REJECTED)

    def submitted(self) -> "TradePlan":
        return _replace(self, status=PlanStatus.SUBMITTED)


def _replace(plan: TradePlan, **changes: object) -> TradePlan:
    """Small local dataclasses.replace, kept explicit to avoid an import at call sites."""
    from dataclasses import replace

    return replace(plan, **changes)
