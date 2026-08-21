"""Report tests: the synthetic run never claims a pass, and the verdict logic is honest."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.backtest.metrics import TradeStats
from crypto_trader.backtest.report import (
    ReportContext,
    _verdict,
    render_json,
    render_markdown,
)
from crypto_trader.backtest.sweep import SweepPoint, run_sweep
from crypto_trader.backtest.synthetic import generate_dataset, generate_funding


def _ctx(is_synthetic: bool) -> ReportContext:
    return ReportContext(
        data_description="test data",
        is_synthetic=is_synthetic,
        symbols=["SYN01USDT"],
        cost_config=CostConfig(),
        funding_description="test funding",
        generated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )


def _stats(n: int, ci_low: float, ci_high: float, max_dd_pct: float) -> TradeStats:
    return TradeStats(
        n=n, mean_r=(ci_low + ci_high) / 2, sd_r=1.0, se_r=0.1, t_stat=1.0,
        ci_normal_low=ci_low, ci_normal_high=ci_high, ci_boot_low=ci_low,
        ci_boot_high=ci_high, win_rate=0.5, payoff=1.5, avg_win_r=1.0, avg_loss_r=0.7,
        total_r=n * (ci_low + ci_high) / 2, max_drawdown_r=1.0,
        max_drawdown_pct=max_dd_pct, exit_counts={},
    )


def _point(oos: TradeStats) -> SweepPoint:
    return SweepPoint(
        lookback_days=60, funding_filter_enabled=False,
        is_stats=oos, oos_stats=oos, n_setups=oos.n, n_funding_filtered=0,
        n_min_notional_voided=0, n_signals=oos.n, n_fills=oos.n, n_closed=oos.n,
        n_open_at_end=0,
    )


def test_synthetic_run_is_reported_as_not_proven():
    dataset = generate_dataset(["SYN01USDT", "SYN02USDT"], years=0.9)
    funding = generate_funding(dataset)
    sweep = run_sweep(
        dataset, funding=funding, lookback_days=(60,), bootstrap_resamples=100,
    )
    md = render_markdown(sweep, _ctx(is_synthetic=True))
    assert "NOT PROVEN (synthetic data)" in md
    assert "## Denominators" in md
    assert "| Lookback (d) |" in md
    assert "SYNTHETIC data" in md

    payload = json.loads(render_json(sweep, _ctx(is_synthetic=True)))
    assert payload["is_synthetic"] is True
    assert payload["gate1_verdict"].startswith("NOT PROVEN")
    assert "combinations" in payload and payload["combinations"]


def test_verdict_passes_only_when_real_numbers_clear_the_bar():
    strong = _point(_stats(n=200, ci_low=0.1, ci_high=0.5, max_dd_pct=0.05))
    head, _why = _verdict(strong, target_low=150, is_synthetic=False)
    assert head == "PASSED"


def test_verdict_fails_when_the_interval_straddles_zero():
    weak = _point(_stats(n=200, ci_low=-0.1, ci_high=0.4, max_dd_pct=0.05))
    head, why = _verdict(weak, target_low=150, is_synthetic=False)
    assert head == "NOT PASSED"
    assert "confidence interval" in why


def test_verdict_fails_on_a_drawdown_breach():
    deep = _point(_stats(n=200, ci_low=0.1, ci_high=0.5, max_dd_pct=0.20))
    head, why = _verdict(deep, target_low=150, is_synthetic=False)
    assert head == "NOT PASSED"
    assert "drawdown" in why


def test_synthetic_verdict_never_claims_a_pass_even_with_strong_numbers():
    strong = _point(_stats(n=200, ci_low=0.3, ci_high=0.6, max_dd_pct=0.02))
    head, _why = _verdict(strong, target_low=150, is_synthetic=True)
    assert head == "NOT PROVEN (synthetic data)"
