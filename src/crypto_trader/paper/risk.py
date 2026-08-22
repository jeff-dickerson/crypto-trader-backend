"""Risk sizing: the tiered schedule, the slider, and the kill-switch clamp.

This is where a strategy signal first becomes a sized, money-denominated trade plan, so
PRD 4.1 (the tiered risk schedule) and captain decision 2 (PRD 4.2, the slider-vs-kill-switch
clamp) are implemented here for the first time. Every number is safety-critical, so the
policy lives in small pure functions with a heavily-tested config rather than inline in the
position manager.

Three rules, in order of application:

1. Tier base (PRD 4.1), auto-adjusting at every plan from current equity:
       equity < $1,000        -> 4%
       $1,000 <= equity < $2,000 -> 3%
       equity >= $2,000        -> 2%
   Read from get_balance() at plan time, so the tier steps down as the account grows without
   any manual change.

2. Slider (PRD 4.2), a 0.25x-2.0x multiplier on the tier base, set by the operator.

3. Kill-switch clamp (captain decision 2, the load-bearing safety rule): the effective
   per-trade risk fraction is capped STRICTLY BELOW the 6% daily kill-switch threshold
   (PRD 9.1), regardless of the slider. Design review found the original spec self-contradictory
   here: at 1.5x on the 4% tier a single ordinary stop-out (6%) trips the 6% daily kill by
   itself, and at 2.0x it exceeds it. The clamp resolves that by making the slider's usable
   ceiling stop below the kill threshold. This is a hard clamp on the computed fraction, not a
   suggestion; the daily kill limit itself is untouched.

The clamp ceiling (`max_effective_risk_fraction`) is a builder-proposed value in the same
sense as the strategy parameters (captain decision 5): a reasoned default, documented and
overridable, that a future Gate 2 run or a captain decision can revise. It is set to 5%, one
whole point below the 6% kill switch. Rationale: a stop ALWAYS slips (PRD 6.2, and the DryRun
adapter models it), so a nominal "1R" stop-out actually loses slightly MORE than the risk
fraction; a 5% cap keeps even a slipped single-trade stop-out under the 6% daily kill with
headroom, which a 5.99% cap would not. The tiers themselves (4/3/2%) sit comfortably under
the cap, so the clamp only ever bites the upper slider range on the small-account tiers, which
is exactly the contradiction it exists to remove.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from crypto_trader.exchange.types import SymbolRule
from crypto_trader.paper.plan import PlanStatus, TradePlan
from crypto_trader.strategy.position import PositionSide

_PLAN_COUNTER = itertools.count(1)


@dataclass(frozen=True)
class RiskTier:
    """One equity tier: applies while equity is strictly below `upper_bound`."""

    upper_bound: float  # exclusive upper equity bound; use float("inf") for the top tier
    risk_fraction: float


@dataclass(frozen=True)
class RiskConfig:
    """Tiered risk, slider bounds, and the kill-switch clamp. All values overridable.

    The defaults encode PRD 4.1 and captain decision 2 exactly. See the module docstring
    for the rationale behind `max_effective_risk_fraction`.
    """

    # PRD 4.1 tiers, ordered ascending by equity. Read low-to-high; the first tier whose
    # upper_bound the equity is strictly below wins.
    tiers: tuple[RiskTier, ...] = (
        RiskTier(upper_bound=1000.0, risk_fraction=0.04),
        RiskTier(upper_bound=2000.0, risk_fraction=0.03),
        RiskTier(upper_bound=float("inf"), risk_fraction=0.02),
    )

    # Slider bounds (PRD 4.2): a multiplier on the tier base.
    slider_min: float = 0.25
    slider_max: float = 2.0

    # The daily kill-switch loss threshold (PRD 9.1). Recorded so the clamp is expressed
    # relative to it and cannot silently drift apart from the real kill limit.
    kill_switch_daily_fraction: float = 0.06

    # Hard clamp: effective per-trade risk is never allowed at or above this. Strictly below
    # kill_switch_daily_fraction (captain decision 2). See module docstring for why 5%.
    max_effective_risk_fraction: float = 0.05

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("at least one risk tier is required")
        bounds = [t.upper_bound for t in self.tiers]
        if bounds != sorted(bounds):
            raise ValueError("risk tiers must be ordered ascending by upper_bound")
        if self.tiers[-1].upper_bound != float("inf"):
            raise ValueError("the top risk tier must have an infinite upper_bound")
        if not 0.0 < self.slider_min <= self.slider_max:
            raise ValueError("require 0 < slider_min <= slider_max")
        if not 0.0 < self.max_effective_risk_fraction < self.kill_switch_daily_fraction:
            raise ValueError(
                "max_effective_risk_fraction must be strictly below the daily kill switch"
            )

    def tier_fraction_for_equity(self, equity: float) -> float:
        """The PRD 4.1 tier base risk fraction for `equity` (auto-adjusting)."""
        if equity <= 0.0:
            raise ValueError("equity must be > 0 to size a trade")
        for tier in self.tiers:
            if equity < tier.upper_bound:
                return tier.risk_fraction
        return self.tiers[-1].risk_fraction

    def clamp_slider(self, slider: float) -> float:
        """Clamp a raw slider multiplier into the allowed [slider_min, slider_max] range."""
        return min(self.slider_max, max(self.slider_min, slider))

    def effective_risk_fraction(self, equity: float, slider: float) -> tuple[float, bool]:
        """The final per-trade risk fraction and whether the kill-switch clamp bit.

        Applies the tier base, then the (bounded) slider, then the hard kill-switch clamp.
        Returns (fraction, clamped) where `clamped` is True when the clamp reduced the
        requested fraction, so callers can explain why a high slider did not raise risk.
        """
        base = self.tier_fraction_for_equity(equity)
        requested = base * self.clamp_slider(slider)
        capped = min(requested, self.max_effective_risk_fraction)
        return capped, capped < requested


DEFAULT_RISK_CONFIG = RiskConfig()


def _next_plan_id(symbol: str) -> str:
    return f"{symbol}-{next(_PLAN_COUNTER):06d}"


def size_trade_plan(
    *,
    symbol: str,
    side: PositionSide,
    entry_price: float,
    stop_price: float,
    take_profit_price: float,
    equity: float,
    symbol_rule: SymbolRule,
    slider: float = 1.0,
    config: RiskConfig = DEFAULT_RISK_CONFIG,
) -> TradePlan:
    """Size one strategy signal into a TradePlan, voiding it visibly if it cannot execute.

    Sizing: risk_amount = equity * effective_risk_fraction; quantity = risk_amount /
    risk_distance, floored to the symbol's base precision so it never exceeds the intended
    size. The plan is VOID (never silently dropped) when:
      - the risk distance is non-positive (a malformed signal), or
      - the precision-floored quantity is zero, or
      - the sized notional is below the exchange minimum order size (PRD 4.2 slider honesty,
        checked at PLAN time before approval so a human never approves an unexecutable plan).

    The min-notional floor is derived as min_trade_volume * entry (SymbolRule.min_notional),
    because Bitunix exposes no standalone minimum-notional field (AGENTS.md, characterization
    spike). The notional checked is the ACTUAL floored order quantity's notional, i.e. what the
    exchange would really receive, not the pre-rounding ideal.
    """
    fraction, capped = config.effective_risk_fraction(equity, slider)
    tier_base = config.tier_fraction_for_equity(equity)
    risk_amount = equity * fraction
    risk_distance = abs(entry_price - stop_price)

    plan_id = _next_plan_id(symbol)
    base_plan = TradePlan(
        plan_id=plan_id,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        quantity=0.0,
        notional=0.0,
        equity=equity,
        tier_risk_fraction=tier_base,
        slider_multiplier=config.clamp_slider(slider),
        effective_risk_fraction=fraction,
        risk_amount=risk_amount,
        slider_capped=capped,
        status=PlanStatus.PROPOSED,
    )

    if risk_distance <= 0.0:
        return base_plan.voided("invalid signal: entry and stop are equal (no risk distance)")

    raw_quantity = risk_amount / risk_distance
    quantity = symbol_rule.round_quantity(raw_quantity)
    if quantity <= 0.0:
        return base_plan.voided(
            "sized quantity rounds to zero at the symbol's precision (risk too small)"
        )

    notional = quantity * entry_price
    min_notional = symbol_rule.min_notional(entry_price)
    if notional < min_notional:
        return base_plan.voided(
            f"below the exchange minimum order size "
            f"(sized ${notional:,.2f} < ${min_notional:,.2f} minimum)"
        )

    from dataclasses import replace

    return replace(base_plan, quantity=quantity, notional=notional)
