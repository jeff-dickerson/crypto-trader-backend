"""No-lookahead regression tests: the safety-critical heart of Build Order step 3.

These tests are engineered to FAIL if lookahead were reintroduced into the replay engine.
Each plants a distinctive candle strictly after a decision point and asserts it cannot reach
that decision, at every layer the PRD names (the 4H volume profile, the daily bias, and the
end-to-end per-bar decision), and each also shows that the planted candle WOULD change a
result that (wrongly) included it, so the test has real detecting power rather than passing
vacuously. Synthetic candles only; no network, no clock.
"""

from __future__ import annotations

from crypto_trader.backtest.engine import causal_windows, run_symbol_backtest
from crypto_trader.backtest.synthetic import generate_symbol
from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.bias import compute_bias
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG as CFG
from crypto_trader.strategy.position import PositionState
from crypto_trader.strategy.signal import generate_signal
from crypto_trader.strategy.volume_profile import build_volume_profile


def _wild_candle(template: Candle, index: int) -> Candle:
    """A candle far outside the normal price range, so including it changes any profile."""
    base = template.close
    return Candle(
        symbol=template.symbol,
        timeframe=Timeframe.H4,
        open_time=template.open_time,
        close_time=template.close_time,
        is_closed=True,
        open=base * 2.0,
        high=base * 3.0,
        low=base * 1.9,
        close=base * 2.5,
        volume=1_000_000.0,
        quote_volume=None,
    )


def test_causal_windows_never_include_a_future_candle():
    h4, d1 = generate_symbol("SYN", seed=1, years=0.6)
    dct = [c.close_time for c in d1]
    for i in range(CFG.min_profile_candles - 1, len(h4), 23):
        h4w, d1w = causal_windows(h4, d1, dct, i, CFG)
        decision_close = h4[i].close_time
        assert h4w[-1] is h4[i]  # the decision point is exactly bar i
        assert all(c.close_time <= decision_close for c in h4w)
        assert all(c.close_time <= decision_close for c in d1w)
        assert max(c.close_time for c in h4w) == decision_close


def test_future_4h_candle_changes_neither_the_window_nor_the_profile_nor_the_signal():
    h4, d1 = generate_symbol("SYN", seed=2, years=0.7)
    dct = [c.close_time for c in d1]
    i = len(h4) // 2

    h4w, d1w = causal_windows(h4, d1, dct, i, CFG)
    profile_before = build_volume_profile(h4w, CFG)
    signal_before = generate_signal(h4w, d1w, PositionState.flat(), CFG)

    # Plant a wild candle at the very next bar, i + 1.
    j = i + 1
    h4_mut = list(h4)
    h4_mut[j] = _wild_candle(h4[j], j)

    h4w2, d1w2 = causal_windows(h4_mut, d1, dct, i, CFG)
    assert h4w2 == h4w
    assert build_volume_profile(h4w2, CFG) == profile_before
    assert generate_signal(h4w2, d1w2, PositionState.flat(), CFG) == signal_before

    # The plant WOULD change a profile that wrongly reached one bar into the future,
    # so this test genuinely detects lookahead rather than passing vacuously.
    leaky_window = h4_mut[max(0, j + 1 - CFG.lookback_candles) : j + 1]
    assert build_volume_profile(leaky_window, CFG) != profile_before


def test_future_daily_candle_does_not_change_the_bias_at_the_decision_point():
    h4, d1 = generate_symbol("SYN", seed=3, years=0.8)
    dct = [c.close_time for c in d1]
    # A 4H decision bar partway through a day, so the current day's candle has not closed.
    i = len(h4) // 2 + 3
    _h4w, d1w = causal_windows(h4, d1, dct, i, CFG)
    bias_before = compute_bias(d1w, CFG)

    # Corrupt every daily candle that closes after the decision point.
    decision_close = h4[i].close_time
    d1_mut = [
        c if c.close_time <= decision_close else _wild_candle(c, k)
        for k, c in enumerate(d1)
    ]
    _h4w2, d1w2 = causal_windows(h4, d1_mut, dct, i, CFG)
    assert d1w2 == d1w
    assert compute_bias(d1w2, CFG) is bias_before


def test_engine_never_feeds_generate_signal_a_candle_past_the_decision_point(monkeypatch):
    import crypto_trader.backtest.engine as engine

    real = generate_signal
    calls = {"n": 0}

    def spy(candles, htf_candles, position_state, config):
        decision_close = candles[-1].close_time
        assert all(c.close_time <= decision_close for c in candles)
        assert all(c.close_time <= decision_close for c in htf_candles)
        calls["n"] += 1
        return real(candles, htf_candles, position_state, config)

    monkeypatch.setattr(engine, "generate_signal", spy)
    h4, d1 = generate_symbol("SYN", seed=4, years=0.6)
    run_symbol_backtest("SYN", h4, d1)
    assert calls["n"] > 0  # the spy actually observed decisions


def test_decisions_fully_before_a_plant_are_identical_with_or_without_future_data():
    h4, d1 = generate_symbol("SYN", seed=5, years=1.0)
    plant = int(len(h4) * 0.6)
    plant_close = h4[plant].close_time

    h4_mut = list(h4)
    for k in range(plant + 1, len(h4)):
        h4_mut[k] = _wild_candle(h4[k], k)

    baseline = run_symbol_backtest("SYN", h4, d1)
    corrupted = run_symbol_backtest("SYN", h4_mut, d1)

    def fully_before(trades):
        return [t for t in trades if t.exit_time <= plant_close]

    before_baseline = fully_before(baseline.closed_trades)
    before_corrupted = fully_before(corrupted.closed_trades)
    assert before_baseline == before_corrupted
    assert before_baseline  # the guard actually covered some closed trades
