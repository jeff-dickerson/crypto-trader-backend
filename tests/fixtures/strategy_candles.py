"""Synthetic validated Candle builders for the pure strategy tests.

Everything here builds crypto_trader.ingest.models.Candle objects directly (the
validated shape the strategy consumes), engineered to trigger each strategy path:
a shaped volume profile, a clean long/short separation-then-return setup, a window
that never separates, a rising series, and a series that crashes into a momentum
shift. Nothing here touches the network or the clock beyond a fixed synthetic base
time used only to give candles ascending, tz-aware timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.config import StrategyConfig

SYMBOL = "BTCUSDT"

# A fixed base time so candles get ascending, tz-aware timestamps. The strategy never
# reads the clock; these timestamps only satisfy the Candle contract and ordering.
_BASE = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# A test config with short periods so a handful of synthetic candles can exercise the
# bias SMAs, the MA ensemble, and the profile without needing hundreds of bars. Only
# the periods and gates are shrunk; the strategy logic under test is unchanged.
TEST_STRATEGY_CONFIG = StrategyConfig(
    profile_bins=10,
    min_profile_candles=8,
    separation_pct=0.01,
    separation_min_bars=2,
    ma_ensemble_periods=(2, 3, 5),
    ma_exit_min_votes=2,
    bias_fast_period=3,
    bias_slow_period=5,
    trail_activation_rr=1.0,
    trail_distance_rr=1.0,
)


def make_candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    timeframe: Timeframe = Timeframe.H4,
    symbol: str = SYMBOL,
    quote_volume: float | None = None,
) -> Candle:
    """Build one validated Candle at a sequential, tz-aware time for `index`."""
    open_time = _BASE + index * timeframe.duration
    close_time = open_time + timeframe.duration
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        is_closed=True,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_volume=quote_volume,
    )


def bar(
    index: int,
    low: float,
    high: float,
    close: float,
    volume: float,
    open_: float | None = None,
    timeframe: Timeframe = Timeframe.H4,
) -> Candle:
    """A candle from low/high/close/volume; open defaults to close."""
    return make_candle(
        index,
        open_ if open_ is not None else close,
        high,
        low,
        close,
        volume,
        timeframe=timeframe,
    )


def zero_range_bar(index: int, price: float, volume: float) -> Candle:
    """A degenerate candle whose whole range sits at one price (for profile tests)."""
    return make_candle(index, price, price, price, price, volume)


def profile_bars_from_bin_volumes(
    volumes: list[float], price_low: float = 100.0, price_high: float = 200.0
) -> list[Candle]:
    """Build zero-range candles that place `volumes[i]` into profile bin i.

    Requires the profile config to use len(volumes) bins over [price_low, price_high].
    Bin 0 is anchored at price_low and the last bin at price_high (pinning the range);
    the rest sit at their bin centres, so each candle's whole volume lands in one bin.
    """
    n = len(volumes)
    width = (price_high - price_low) / n
    bars: list[Candle] = []
    for b, volume in enumerate(volumes):
        if b == 0:
            price = price_low
        elif b == n - 1:
            price = price_high
        else:
            price = price_low + b * width + width / 2.0
        bars.append(zero_range_bar(b, price, volume))
    return bars


def rising_daily(count: int = 8, start: float = 100.0, step: float = 2.0) -> list[Candle]:
    """Strictly rising daily closes: a long bias under a fast/slow SMA filter."""
    bars: list[Candle] = []
    for i in range(count):
        close = start + i * step
        bars.append(bar(i, close - 1.0, close + 1.0, close, 100.0, timeframe=Timeframe.D1))
    return bars


def falling_daily(
    count: int = 8, start: float = 114.0, step: float = 2.0
) -> list[Candle]:
    """Strictly falling daily closes: a short bias."""
    bars: list[Candle] = []
    for i in range(count):
        close = start - i * step
        bars.append(bar(i, close - 1.0, close + 1.0, close, 100.0, timeframe=Timeframe.D1))
    return bars


def flat_daily(count: int = 8, price: float = 100.0) -> list[Candle]:
    """Constant daily closes: a neutral bias (no trend, no trade)."""
    return [
        bar(i, price - 1.0, price + 1.0, price, 100.0, timeframe=Timeframe.D1)
        for i in range(count)
    ]


def long_entry_window() -> list[Candle]:
    """A 4H window that fires a clean long first-touch retest.

    Phase 1: an oscillating high-volume cluster around 150 (builds the value area).
    Phase 2: four candles separating up to ~170-180 (price leaves the zone).
    Return: a final candle opening above the zone and dipping down to touch the value
    area low (a resting buy limit at the boundary fills).
    """
    bars: list[Candle] = []
    i = 0
    for _ in range(20):
        bars.append(bar(i, low=146.0, high=154.0, close=150.0, volume=100.0))
        i += 1
    for high in (168.0, 174.0, 179.0, 182.0):
        bars.append(bar(i, low=165.0, high=high, close=high - 2.0, volume=20.0))
        i += 1
    bars.append(bar(i, low=140.0, high=173.0, close=150.0, volume=5.0, open_=172.0))
    return bars


def long_no_separation_window() -> list[Candle]:
    """A 4H window where price never leaves the zone: no valid setup.

    Every candle oscillates through the value area (repeatedly touching the boundary),
    so there is no separation run before the final touch, and no signal should fire.
    """
    bars: list[Candle] = []
    i = 0
    for _ in range(20):
        bars.append(bar(i, low=140.0, high=155.0, close=148.0, volume=100.0))
        i += 1
    bars.append(bar(i, low=140.0, high=170.0, close=145.0, volume=5.0, open_=168.0))
    return bars


def short_entry_window() -> list[Candle]:
    """A 4H window that fires a clean short first-touch retest.

    Mirror of the long window: an oscillating cluster around 150, four candles
    separating DOWN to ~130, then a final candle opening below the zone and rising up
    to touch the value area high (a resting sell limit at the boundary fills).
    """
    bars: list[Candle] = []
    i = 0
    for _ in range(20):
        bars.append(bar(i, low=146.0, high=154.0, close=150.0, volume=100.0))
        i += 1
    for low in (132.0, 126.0, 121.0, 118.0):
        bars.append(bar(i, low=low, high=135.0, close=low + 2.0, volume=20.0))
        i += 1
    bars.append(bar(i, low=127.0, high=175.0, close=150.0, volume=5.0, open_=128.0))
    return bars


def rising_4h(count: int = 8, start: float = 100.0, step: float = 2.0) -> list[Candle]:
    """A rising 4H series: no momentum shift against a long."""
    bars: list[Candle] = []
    for i in range(count):
        close = start + i * step
        bars.append(bar(i, close - 1.0, close + 1.0, close, 100.0))
    return bars


def rising_then_crash_4h() -> list[Candle]:
    """A 4H series that rises, then the last candle crashes below the MA ensemble."""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 90.0]
    bars: list[Candle] = []
    for i, close in enumerate(closes):
        high = max(close, closes[i - 1] if i else close) + 1.0
        bars.append(bar(i, low=close - 1.0, high=high, close=close, volume=100.0))
    return bars
