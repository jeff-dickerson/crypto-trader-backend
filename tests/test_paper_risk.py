"""Risk-sizing tests: the tiered schedule, the slider, and the kill-switch clamp.

The clamp (captain decision 2) is the safety-critical rule here: effective per-trade risk must
stay strictly below the 6% daily kill switch no matter the slider, so a single ordinary stop-out
can never trip the daily kill by itself. The plan-time min-notional voiding (PRD 4.2) is the
other load-bearing behaviour: an unexecutable plan is surfaced with a reason, never silently
dropped.
"""

from __future__ import annotations

import pytest

from crypto_trader.exchange.types import SymbolRule
from crypto_trader.paper.plan import PlanStatus
from crypto_trader.paper.risk import DEFAULT_RISK_CONFIG, RiskConfig, size_trade_plan
from crypto_trader.strategy.position import PositionSide

RULE = SymbolRule(symbol="BTCUSDT", base_precision=4, quote_precision=1, min_trade_volume=0.0001)


def _plan(equity: float, slider: float = 1.0, entry: float = 100.0, stop: float = 95.0):
    return size_trade_plan(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=115.0,
        equity=equity,
        symbol_rule=RULE,
        slider=slider,
    )


def test_tier_fractions_follow_the_prd_schedule() -> None:
    cfg = DEFAULT_RISK_CONFIG
    assert cfg.tier_fraction_for_equity(500.0) == 0.04  # < $1,000
    assert cfg.tier_fraction_for_equity(999.99) == 0.04
    assert cfg.tier_fraction_for_equity(1000.0) == 0.03  # $1,000-2,000
    assert cfg.tier_fraction_for_equity(1999.99) == 0.03
    assert cfg.tier_fraction_for_equity(2000.0) == 0.02  # >= $2,000
    assert cfg.tier_fraction_for_equity(50_000.0) == 0.02


def test_tier_auto_adjusts_with_equity_at_plan_time() -> None:
    small = _plan(equity=500.0)
    big = _plan(equity=5000.0)
    assert small.tier_risk_fraction == 0.04
    assert big.tier_risk_fraction == 0.02
    # Same 5% risk distance, so the currency at risk is exactly the tier fraction of equity.
    assert small.risk_amount == pytest.approx(500.0 * 0.04)
    assert big.risk_amount == pytest.approx(5000.0 * 0.02)


def test_slider_scales_risk_within_bounds() -> None:
    cfg = DEFAULT_RISK_CONFIG
    frac, capped = cfg.effective_risk_fraction(equity=2500.0, slider=0.5)
    assert frac == pytest.approx(0.02 * 0.5)  # 2% tier x 0.5
    assert not capped
    # A slider below the minimum is clamped up to the minimum, not applied raw.
    assert cfg.clamp_slider(0.1) == cfg.slider_min
    assert cfg.clamp_slider(9.0) == cfg.slider_max


def test_kill_switch_clamp_binds_on_the_small_account_top_slider() -> None:
    # 4% tier at 2.0x would be 8%, well above the 6% daily kill: the clamp must bind to 5%.
    frac, capped = DEFAULT_RISK_CONFIG.effective_risk_fraction(equity=500.0, slider=2.0)
    assert capped
    assert frac == DEFAULT_RISK_CONFIG.max_effective_risk_fraction == 0.05
    assert frac < DEFAULT_RISK_CONFIG.kill_switch_daily_fraction  # strictly below 6%


def test_clamp_is_strictly_below_the_kill_switch_for_every_tier_and_slider() -> None:
    cfg = DEFAULT_RISK_CONFIG
    for equity in (200.0, 999.0, 1500.0, 3000.0):
        for slider in (0.25, 1.0, 1.5, 2.0):
            frac, _ = cfg.effective_risk_fraction(equity=equity, slider=slider)
            # The load-bearing invariant: no combination ever reaches the daily kill threshold.
            assert frac < cfg.kill_switch_daily_fraction


def test_plan_records_when_the_clamp_bit() -> None:
    plan = _plan(equity=500.0, slider=2.0)
    assert plan.slider_capped is True
    assert plan.effective_risk_fraction == 0.05
    assert plan.tier_risk_fraction == 0.04  # the label still shows the tier, not the clamp


def test_quantity_is_floored_to_precision() -> None:
    # risk_amount = 2000*0.02 = 40; risk_distance = 5; raw qty = 8.0 exactly here.
    plan = _plan(equity=2000.0, entry=100.0, stop=95.0)
    assert plan.status is PlanStatus.PROPOSED
    assert plan.quantity == pytest.approx(8.0)
    assert plan.notional == pytest.approx(800.0)


def test_plan_is_voided_visibly_below_minimum_notional() -> None:
    # A tiny account with a huge risk distance sizes a sub-minimum quantity -> void with a reason.
    tiny_rule = SymbolRule(
        symbol="BTCUSDT", base_precision=4, quote_precision=1, min_trade_volume=1.0
    )
    plan = size_trade_plan(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=100.0,
        stop_price=95.0,
        take_profit_price=115.0,
        equity=100.0,
        symbol_rule=tiny_rule,
        slider=1.0,
    )
    assert plan.is_void
    assert plan.status is PlanStatus.VOID
    assert "minimum" in (plan.void_reason or "")


def test_plan_voided_when_quantity_rounds_to_zero() -> None:
    # A tiny account against a wide stop on a high-priced symbol sizes below one lot: raw qty
    # 0.04/500 = 0.00008 floors to 0 at 4dp, so the plan is voided rather than sent as a zero.
    plan = _plan(equity=1.0, entry=1000.0, stop=500.0)
    assert plan.is_void
    assert "zero" in (plan.void_reason or "") or "minimum" in (plan.void_reason or "")


def test_plan_voided_when_signal_has_no_risk_distance() -> None:
    plan = _plan(equity=2000.0, entry=100.0, stop=100.0)
    assert plan.is_void
    assert "risk distance" in (plan.void_reason or "")


def test_risk_line_shows_one_clear_number_without_tier_logic() -> None:
    plan = _plan(equity=500.0)  # 4% tier, $20 at risk
    assert plan.risk_line == "Risk: 4% tier, $20.00 on this trade"
    # No slider/clamp arithmetic leaks into the human-facing line.
    assert "slider" not in plan.risk_line.lower()
    assert "clamp" not in plan.risk_line.lower()


def test_riskconfig_rejects_a_clamp_not_below_the_kill_switch() -> None:
    with pytest.raises(ValueError):
        RiskConfig(max_effective_risk_fraction=0.06, kill_switch_daily_fraction=0.06)
    with pytest.raises(ValueError):
        RiskConfig(max_effective_risk_fraction=0.07, kill_switch_daily_fraction=0.06)
