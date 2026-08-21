"""Statistics tests: expectancy, the confidence-interval gate, drawdown, win rate, payoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto_trader.backtest.engine import ClosedTrade, ExitType
from crypto_trader.backtest.metrics import compute_stats
from crypto_trader.strategy.position import PositionSide

_T0 = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _trade(net_r: float, minutes: int) -> ClosedTrade:
    """A closed trade carrying `net_r`, timestamped so ordering is well defined."""
    t = _T0 + timedelta(minutes=minutes)
    return ClosedTrade(
        symbol="X",
        side=PositionSide.LONG,
        entry_time=t,
        exit_time=t + timedelta(hours=4),
        entry_price=100.0,
        exit_price=100.0 + net_r,
        initial_stop=99.0,
        take_profit=110.0,
        exit_type=ExitType.TAKE_PROFIT,
        bars_held=1,
        funding_fraction=0.0,
        gross_r=net_r,
        net_r=net_r,
    )


def test_empty_set_is_well_defined():
    s = compute_stats([])
    assert s.n == 0
    assert s.payoff is None
    assert not s.ci_excludes_zero


def test_win_rate_and_payoff():
    trades = [_trade(r, i) for i, r in enumerate([1.0, 1.0, -1.0, -1.0, 1.0])]
    s = compute_stats(trades)
    assert s.n == 5
    assert s.win_rate == pytest.approx(0.6)
    assert s.avg_win_r == pytest.approx(1.0)
    assert s.avg_loss_r == pytest.approx(1.0)
    assert s.payoff == pytest.approx(1.0)
    assert s.mean_r == pytest.approx(0.2)


def test_max_drawdown_in_r():
    trades = [_trade(r, i) for i, r in enumerate([1.0, 1.0, -1.0, -1.0, 1.0])]
    s = compute_stats(trades)
    # Equity R curve 1, 2, 1, 0, 1: peak 2, trough 0 -> drawdown 2 R.
    assert s.max_drawdown_r == pytest.approx(2.0)
    assert s.max_drawdown_pct > 0.0


def test_confidence_interval_excludes_zero_only_when_clearly_positive():
    positive = [_trade(r, i) for i, r in enumerate([0.4, 0.5, 0.6] * 20)]
    sp = compute_stats(positive, bootstrap_resamples=500)
    assert sp.ci_excludes_zero
    assert sp.positive_edge_proven
    assert sp.ci_boot_low > 0.0

    mixed = [_trade(r, i) for i, r in enumerate([1.0, -1.0] * 20)]
    sm = compute_stats(mixed, bootstrap_resamples=500)
    assert not sm.ci_excludes_zero
    assert not sm.positive_edge_proven


def test_stats_are_deterministic_given_the_seed():
    trades = [_trade(r, i) for i, r in enumerate([0.3, -0.2, 0.5, -0.1, 0.4] * 10)]
    a = compute_stats(trades, bootstrap_resamples=500, seed=7)
    b = compute_stats(trades, bootstrap_resamples=500, seed=7)
    assert a.ci_boot_low == b.ci_boot_low
    assert a.ci_boot_high == b.ci_boot_high
