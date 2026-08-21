"""Sweep tests: the in-sample/out-of-sample split, honest selection, and filter effect."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crypto_trader.backtest.engine import ClosedTrade, ExitType
from crypto_trader.backtest.sweep import (
    _select_by_in_sample,
    _split_trades,
    run_sweep,
)
from crypto_trader.backtest.synthetic import generate_dataset, generate_funding
from crypto_trader.strategy.position import PositionSide

_T0 = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _trade(entry_days: int) -> ClosedTrade:
    t = _T0 + timedelta(days=entry_days)
    return ClosedTrade(
        symbol="X", side=PositionSide.LONG, entry_time=t, exit_time=t,
        entry_price=100.0, exit_price=101.0, initial_stop=99.0, take_profit=105.0,
        exit_type=ExitType.TAKE_PROFIT, bars_held=1, funding_fraction=0.0,
        gross_r=1.0, net_r=1.0,
    )


def test_split_trades_partitions_by_entry_time():
    trades = [_trade(d) for d in (1, 5, 9)]
    split = _T0 + timedelta(days=6)
    is_t, oos_t = _split_trades(trades, split)
    assert [t.entry_time.day for t in is_t] == [2, 6]
    assert [t.entry_time.day for t in oos_t] == [10]


def test_select_by_in_sample_ignores_out_of_sample():
    dataset = generate_dataset(["SYN01USDT", "SYN02USDT", "SYN03USDT"], years=0.9)
    funding = generate_funding(dataset)
    sweep = run_sweep(
        dataset, funding=funding, lookback_days=(45, 60),
        bootstrap_resamples=200,
    )
    assert len(sweep.points) == 4  # 2 lookbacks x {off, on}
    assert sweep.data_start < sweep.split_time < sweep.data_end
    # Every closed trade lands in exactly one of IS / OOS.
    for p in sweep.points:
        assert p.is_stats.n + p.oos_stats.n == p.n_closed
    # The chosen point is the argmax of IN-SAMPLE mean R, never the OOS score.
    assert sweep.chosen_index == _select_by_in_sample(sweep.points)


def test_filter_on_never_adds_signals():
    dataset = generate_dataset(["SYN01USDT", "SYN02USDT"], years=1.1)
    funding = generate_funding(dataset)
    sweep = run_sweep(
        dataset, funding=funding, lookback_days=(60,),
        bootstrap_resamples=100,
    )
    by_filter = {p.funding_filter_enabled: p for p in sweep.points}
    assert by_filter[True].n_signals <= by_filter[False].n_signals
