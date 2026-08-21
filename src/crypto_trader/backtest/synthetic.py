"""Deterministic synthetic market data for the runnable Gate 1 demo.

This is NOT real market data and NOT a validation of the edge. It exists so the backtest
lab can be run end to end with a single command, offline, with a reproducible result, and so
the no-lookahead and funnel machinery has structured setups to chew on. Every report
generated from it says plainly that the data is synthetic and that Gate 1 is therefore not
proven (PRD 6.2, conduct rule 7). Real Gate 1 needs real ingested candles (see
crypto_trader.backtest.data) and is a separate run.

Construction. Each symbol is a sequence of directional CHANNELS that alternate up and down,
so the daily bias is clearly long inside an up-channel and short inside a down-channel. Inside
a channel the price OSCILLATES (a triangle wave) around a drifting baseline, and the volume is
weighted toward the bias-side rail: the lower rail in an up-channel, the upper rail in a
down-channel. That pins the volume-profile value-area edge to that rail, so each oscillation is
a real separation-then-return retest of a stable edge (price leaves the edge, holds away from
it, then dips back to touch it). The baseline drift walks the edge slowly higher (or lower), so
successive retests land on DISTINCT zones rather than being rejected by the one-zone-one-trade
governor. Outcomes are whatever the oscillation plus the drift produce, not hand-placed, so the
win/loss distribution stays honest. Daily candles are the exact 6-bar aggregation of the 4H
candles, so the two timeframes never disagree. Funding tracks the persistent channel drift, so
a with-bias entry sits on the crowded, funding-paying side and a hot channel pushes funding past
the filter threshold, which is exactly what captain decision 4's filter is meant to catch.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from crypto_trader.backtest.funding import (
    DEFAULT_FUNDING_INTERVAL_HOURS,
    FundingSchedule,
)
from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.indicators import sma

_BASE = datetime(2021, 1, 1, tzinfo=timezone.utc)

# A synthetic universe roughly the size of the real 10-15 symbol target (PRD 3.1), so the
# demo run reaches the Gate 1 signal scale. Every name is obviously synthetic.
DEFAULT_SYMBOLS = [f"SYN{i:02d}USDT" for i in range(1, 19)]

# One (open, high, low, close, volume) row of the raw generated series.
_Row = tuple[float, float, float, float, float]
# A (4H candles, daily candles) pair for one symbol.
_SymbolCandles = tuple[list[Candle], list[Candle]]


def _h4_candle(
    symbol: str, index: int, o: float, h: float, low: float, c: float, v: float
) -> Candle:
    ot = _BASE + index * Timeframe.H4.duration
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.H4,
        open_time=ot,
        close_time=ot + Timeframe.H4.duration,
        is_closed=True,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        quote_volume=None,
    )


def _triangle(phase: float) -> float:
    """A triangle wave in [-1, +1] over phase in [0, 1): -1 at the ends, +1 at the middle."""
    return 1.0 - 4.0 * abs(phase - 0.5)


def _generate_ohlcv(rng: random.Random, n_target: int) -> list[_Row]:
    """Build a channel-oscillator OHLCV series of at least `n_target` 4H bars."""
    bars: list[_Row] = []
    baseline = 100.0 * rng.uniform(0.5, 4.0)
    direction = 1 if rng.random() < 0.5 else -1

    while len(bars) < n_target:
        # One directional channel: a persistent bias with an oscillation the strategy retests.
        channel_days = rng.randint(120, 200)
        channel_bars = channel_days * 6
        # Drift is deliberately small relative to the amplitude: a strong enough daily-SMA
        # tilt for a directional bias, but slow enough that the value-area edge stays within
        # reach of the oscillation dips even over a 90-day profile window, so retests fire
        # across the whole swept lookback range rather than only the shortest one. Tuned
        # empirically against the 45/60/75-day sweep.
        drift_per_bar = direction * rng.uniform(0.00028, 0.00033)  # ~ 0.17-0.20% per day
        amp = rng.uniform(0.055, 0.065)  # oscillation half-width around the baseline
        period = rng.randint(9, 11)  # bars per oscillation

        prev_close = baseline
        for k in range(channel_bars):
            baseline *= (1.0 + drift_per_bar)
            s = _triangle((k % period) / period)  # -1 at the rails, +1 mid-swing
            close = baseline * (1.0 + amp * s)
            o = prev_close
            wick = close * rng.uniform(0.0015, 0.0045)
            hi = max(o, close) + wick
            lo = min(o, close) - wick
            # Volume is heavy at the bias-side rail so the value-area edge pins there:
            # the lower rail (s low) in an up-channel, the upper rail (s high) in a down one.
            rail_proximity = (1.0 - s) / 2.0 if direction > 0 else (1.0 + s) / 2.0
            volume = 20.0 + 130.0 * rail_proximity + rng.uniform(0.0, 12.0)
            bars.append((o, hi, lo, close, volume))
            prev_close = close

        baseline = max(1.0, prev_close)
        direction *= -1  # alternate channel direction, flipping the bias

    return bars


def _aggregate_daily(symbol: str, h4: list[Candle]) -> list[Candle]:
    """Daily candles as the exact 6-bar aggregation of the 4H candles (00:00 anchored)."""
    daily: list[Candle] = []
    for d in range(len(h4) // 6):
        chunk = h4[d * 6 : d * 6 + 6]
        ot = _BASE + d * Timeframe.D1.duration
        daily.append(
            Candle(
                symbol=symbol,
                timeframe=Timeframe.D1,
                open_time=ot,
                close_time=ot + Timeframe.D1.duration,
                is_closed=True,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
                quote_volume=None,
            )
        )
    return daily


def generate_symbol(symbol: str, seed: int, years: float = 2.0) -> _SymbolCandles:
    """Generate (4H, daily) candles for one synthetic symbol, deterministic in `seed`."""
    rng = random.Random(seed)
    n_target = int(years * 365 * 6)
    ohlcv = _generate_ohlcv(rng, n_target)
    # Trim to a whole number of days so the daily aggregation is clean.
    n_days = len(ohlcv) // 6
    ohlcv = ohlcv[: n_days * 6]
    h4 = [_h4_candle(symbol, i, *row) for i, row in enumerate(ohlcv)]
    d1 = _aggregate_daily(symbol, h4)
    return h4, d1


def generate_dataset(
    symbols: list[str] | None = None, *, years: float = 2.0, seed: int = 12345
) -> dict[str, tuple[list[Candle], list[Candle]]]:
    """A full synthetic dataset: symbol -> (4H candles, daily candles)."""
    symbols = symbols or DEFAULT_SYMBOLS
    dataset: dict[str, tuple[list[Candle], list[Candle]]] = {}
    for k, symbol in enumerate(symbols):
        dataset[symbol] = generate_symbol(symbol, seed=seed + k * 101, years=years)
    return dataset


def generate_funding(
    dataset: dict[str, tuple[list[Candle], list[Candle]]],
    *,
    base_rate: float = 0.0002,
    trend_gain: float = 0.03,
    cap: float = 0.0018,
    fast_period: int = 20,
    slow_period: int = 50,
) -> FundingSchedule:
    """A synthetic funding schedule that tracks the DAILY-TREND direction, not local noise.

    Funding is set from the same fast/slow daily-SMA trend the bias gate reads: when the
    daily trend is up (the side the bias gate takes long), funding is positive so longs pay,
    and vice versa. This is the honest model of captain decision 4's concern (PRD 3.4): the
    bias gate puts the strategy on the crowded side, so its entries face adverse funding over
    the hold even though the instantaneous rate at a pullback dip might look benign. The
    stronger the trend, the larger the (adverse) rate, so a hot channel pushes it past the
    filter threshold and the sweep's filter-on/off comparison becomes meaningful. A
    documented placeholder, not a real funding series (PRD 9.3 MUST-VERIFY).
    """
    interval = DEFAULT_FUNDING_INTERVAL_HOURS
    rates_by_symbol: dict[str, list[tuple[datetime, float]]] = {}
    for symbol, (_h4, d1) in dataset.items():
        closes = [c.close for c in d1]
        points: list[tuple[datetime, float]] = []
        for idx, day in enumerate(d1):
            fast = sma(closes[: idx + 1], fast_period)
            slow = sma(closes[: idx + 1], slow_period)
            if fast is None or slow is None or slow == 0:
                rate = 0.0
            else:
                trend = (fast - slow) / slow  # positive in an up-trend, the crowded-long side
                sign = 1.0 if trend >= 0 else -1.0
                rate = sign * base_rate + trend * trend_gain
                rate = max(-cap, min(cap, rate))
            # A daily settlement stamp; the step-function lookup holds it until the next day.
            points.append((day.close_time, rate))
        rates_by_symbol[symbol] = points
    return FundingSchedule(rates_by_symbol=rates_by_symbol, interval_hours=interval)
