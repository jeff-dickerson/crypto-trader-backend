"""Engine tests: the fill/management state machine and its conservative intrabar rule.

The no-lookahead slicing has its own dedicated file (test_backtest_no_lookahead.py); this
file covers the rest of the engine: the signal/fill/closed funnel is three distinct counts,
the simulation is deterministic, and the worst-of-stop-vs-take-profit intrabar rule holds.
Some tests reach into the engine's private helpers to pin one rule in isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.backtest.engine import (
    ExitType,
    _manage_open_position,
    _OpenPosition,
    _PendingOrder,
    _same_bar_stop_breach,
    _try_fill,
    run_symbol_backtest,
)
from crypto_trader.backtest.funding import FundingSchedule
from crypto_trader.backtest.synthetic import generate_funding, generate_symbol
from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG as CFG
from crypto_trader.strategy.position import PositionSide

_T = datetime(2021, 6, 1, tzinfo=timezone.utc)


def _bar(o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        symbol="X", timeframe=Timeframe.H4, open_time=_T,
        close_time=_T + Timeframe.H4.duration, is_closed=True,
        open=o, high=h, low=low, close=c, volume=100.0, quote_volume=None,
    )


def _long_pos() -> _OpenPosition:
    return _OpenPosition(
        side=PositionSide.LONG, entry_price=100.0, initial_stop=95.0, take_profit=105.0,
        current_stop=95.0, extreme_price=100.0, entry_time=_T, entry_bar=0,
    )


def _manage(bar: Candle):
    return _manage_open_position(
        "X", _long_pos(), bar, [bar], 0, CFG.lookback_candles, [], [],
        CFG, CostConfig(), FundingSchedule(rates_by_symbol={}),
    )


def test_try_fill_needs_a_trade_through():
    long_order = _PendingOrder(PositionSide.LONG, 100.0, 95.0, 105.0, 0)
    assert _try_fill(long_order, _bar(101.0, 102.0, 99.5, 101.0))  # low dipped to the limit
    assert not _try_fill(long_order, _bar(101.0, 103.0, 100.5, 102.0))  # never reached it
    short_order = _PendingOrder(PositionSide.SHORT, 100.0, 105.0, 95.0, 0)
    assert _try_fill(short_order, _bar(99.0, 100.5, 98.0, 99.0))  # high rose to the limit


def test_same_bar_stop_breach_is_conservative():
    pos = _long_pos()
    assert _same_bar_stop_breach(pos, _bar(100.0, 101.0, 94.0, 99.0)) == 95.0  # wick took the stop
    assert _same_bar_stop_breach(pos, _bar(100.0, 101.0, 96.0, 99.0)) is None


def test_intrabar_stop_wins_ties_with_the_take_profit():
    # A bar that reaches BOTH the stop (95) and the take-profit (105): the stop is assumed
    # to fill first, the worse outcome (AGENTS.md conservative intrabar rule).
    both = _manage(_bar(100.0, 106.0, 94.0, 100.0))
    assert both is not None and both.exit_type is ExitType.STOP
    assert both.exit_price == 95.0


def test_take_profit_alone_closes_at_the_target():
    tp = _manage(_bar(100.0, 106.0, 99.0, 104.0))
    assert tp is not None and tp.exit_type is ExitType.TAKE_PROFIT
    assert tp.exit_price == 105.0


def test_stop_alone_closes_at_the_stop():
    st = _manage(_bar(100.0, 101.0, 94.0, 96.0))
    assert st is not None and st.exit_type is ExitType.STOP
    assert st.exit_price == 95.0


def test_no_level_hit_holds():
    assert _manage(_bar(100.0, 104.0, 96.0, 101.0)) is None


def test_funnel_counts_are_a_monotone_nonincreasing_funnel():
    from crypto_trader.backtest.engine import run_backtest
    from crypto_trader.backtest.synthetic import generate_dataset

    symbols = [f"SYN{i:02d}USDT" for i in range(1, 7)]
    r = run_backtest(generate_dataset(symbols, years=1.2))
    assert r.n_setups > 0
    assert r.n_signals > 0
    # setups >= signals (filters) >= fills (some never fill) >= closed (some open at end).
    assert r.n_setups >= r.n_signals >= r.n_fills >= r.n_closed
    assert r.n_fills == r.n_closed + r.n_open_at_end
    # Across several symbols the model really does drop some orders between signal and fill,
    # so the three PRD denominators are genuinely distinct counts, not the same number.
    assert r.n_signals > r.n_fills


def test_backtest_is_deterministic():
    h4, d1 = generate_symbol("SYN", seed=12, years=0.7)
    fund = generate_funding({"SYN": (h4, d1)})
    a = run_symbol_backtest("SYN", h4, d1, funding=fund)
    b = run_symbol_backtest("SYN", h4, d1, funding=fund)
    assert a.closed_trades == b.closed_trades
    assert (a.n_setups, a.n_signals, a.n_fills, a.n_closed) == (
        b.n_setups, b.n_signals, b.n_fills, b.n_closed,
    )


def test_funding_filter_only_removes_entries():
    from dataclasses import replace

    h4, d1 = generate_symbol("SYN", seed=13, years=1.2)
    fund = generate_funding({"SYN": (h4, d1)})
    off = run_symbol_backtest("SYN", h4, d1, funding=fund)
    on = run_symbol_backtest(
        "SYN", h4, d1,
        strategy_config=replace(CFG, funding_filter_enabled=True),
        funding=fund,
    )
    assert on.n_signals <= off.n_signals
    assert on.n_funding_filtered >= 0
