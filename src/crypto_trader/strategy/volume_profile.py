"""Volume profile: POC, VAH, VAL, and the HVN/LVN structure from 4H candles.

No-lookahead note (see AGENTS.md): this function computes the profile from EXACTLY
the candles it is handed, however many that is. It does not assume a fixed, complete
lookback window, because a future backtest replay engine (step 3) will call the
strategy repeatedly with a growing, causally-sliced window. Slicing a window to the
configured lookback is the caller's job (generate_signal); building an honest profile
from whatever candles are present is this module's job.

Only OHLCV is available (no tick data), so each candle's volume is spread uniformly
across the price bins its [low, high] range overlaps. This is the standard
volume-by-price approximation for candle data and is fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.config import StrategyConfig


@dataclass(frozen=True)
class VolumeBin:
    """One price bucket of the profile. `low`/`high` are its price edges."""

    index: int
    low: float
    high: float
    volume: float

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(frozen=True)
class VolumeProfile:
    """The computed profile over a set of candles.

    bins are ascending by price. poc_price is the centre of the highest-volume bin
    (the POC / heaviest volume node). vah/val are the value-area price edges (the
    UPPER edge of the highest value-area bin and the LOWER edge of the lowest). The
    value area is the contiguous run of bins around the POC holding at least
    value_area_pct of total volume. hvn/lvn indices classify every bin against the
    mean bin volume (see StrategyConfig).
    """

    bins: tuple[VolumeBin, ...]
    total_volume: float
    price_low: float
    price_high: float
    poc_index: int
    value_area_indices: tuple[int, ...]
    hvn_indices: tuple[int, ...]
    lvn_indices: tuple[int, ...]

    @property
    def poc_price(self) -> float:
        return self.bins[self.poc_index].center

    @property
    def val(self) -> float:
        """Value area low: the lower price edge of the lowest value-area bin."""
        return self.bins[min(self.value_area_indices)].low

    @property
    def vah(self) -> float:
        """Value area high: the upper price edge of the highest value-area bin."""
        return self.bins[max(self.value_area_indices)].high

    def nearest_lvn_below(self, price: float) -> VolumeBin | None:
        """The closest LVN bin whose centre is below `price`, or None."""
        candidates = [
            self.bins[i] for i in self.lvn_indices if self.bins[i].center < price
        ]
        return max(candidates, key=lambda b: b.center) if candidates else None

    def nearest_lvn_above(self, price: float) -> VolumeBin | None:
        """The closest LVN bin whose centre is above `price`, or None."""
        candidates = [
            self.bins[i] for i in self.lvn_indices if self.bins[i].center > price
        ]
        return min(candidates, key=lambda b: b.center) if candidates else None

    def nearest_hvn_above(self, price: float) -> VolumeBin | None:
        """The closest HVN bin whose centre is above `price`, or None."""
        candidates = [
            self.bins[i] for i in self.hvn_indices if self.bins[i].center > price
        ]
        return min(candidates, key=lambda b: b.center) if candidates else None

    def nearest_hvn_below(self, price: float) -> VolumeBin | None:
        """The closest HVN bin whose centre is below `price`, or None."""
        candidates = [
            self.bins[i] for i in self.hvn_indices if self.bins[i].center < price
        ]
        return max(candidates, key=lambda b: b.center) if candidates else None


def build_volume_profile(
    candles: list[Candle], config: StrategyConfig
) -> VolumeProfile | None:
    """Build the volume profile from exactly `candles`.

    Returns None (rather than raising) when a meaningful profile cannot be built:
    no candles, zero total volume, or a degenerate zero-width price range. Callers
    treat None as "no profile, no entry".
    """
    if not candles:
        return None

    price_low = min(c.low for c in candles)
    price_high = max(c.high for c in candles)
    if price_high <= price_low:
        return None

    total_volume = sum(c.volume for c in candles)
    if total_volume <= 0:
        return None

    n = config.profile_bins
    width = (price_high - price_low) / n
    edges = [price_low + i * width for i in range(n + 1)]
    bin_volume = [0.0] * n

    def bin_index(price: float) -> int:
        idx = int((price - price_low) / width)
        return min(max(idx, 0), n - 1)

    for candle in candles:
        span = candle.high - candle.low
        if span <= 0:
            # Zero-range candle: all its volume falls in the single bin at that price.
            bin_volume[bin_index(candle.low)] += candle.volume
            continue
        first = bin_index(candle.low)
        last = bin_index(candle.high)
        for i in range(first, last + 1):
            overlap = min(candle.high, edges[i + 1]) - max(candle.low, edges[i])
            if overlap > 0:
                bin_volume[i] += candle.volume * (overlap / span)

    bins = tuple(
        VolumeBin(index=i, low=edges[i], high=edges[i + 1], volume=bin_volume[i])
        for i in range(n)
    )

    poc_index = max(range(n), key=lambda i: bin_volume[i])
    value_area_indices = _value_area_indices(
        bin_volume, poc_index, config.value_area_pct * total_volume
    )

    mean_volume = total_volume / n
    hvn_indices = tuple(
        i for i in range(n) if bin_volume[i] >= config.hvn_volume_ratio * mean_volume
    )
    lvn_indices = tuple(
        i for i in range(n) if bin_volume[i] <= config.lvn_volume_ratio * mean_volume
    )

    return VolumeProfile(
        bins=bins,
        total_volume=total_volume,
        price_low=price_low,
        price_high=price_high,
        poc_index=poc_index,
        value_area_indices=tuple(value_area_indices),
        hvn_indices=hvn_indices,
        lvn_indices=lvn_indices,
    )


def _value_area_indices(
    bin_volume: list[float], poc_index: int, target_volume: float
) -> list[int]:
    """Expand out from the POC until the accumulated volume covers target_volume.

    At each step the neighbouring bin (just above the current top, or just below the
    current bottom) with the greater volume is added. Ties expand upward. This is the
    single-step form of the standard value-area walk; documented as a deliberate
    simplification of the two-bins-at-a-time TPO convention, and fully deterministic.
    """
    n = len(bin_volume)
    included = {poc_index}
    accumulated = bin_volume[poc_index]
    low = poc_index
    high = poc_index

    while accumulated < target_volume and (low > 0 or high < n - 1):
        below = bin_volume[low - 1] if low > 0 else float("-inf")
        above = bin_volume[high + 1] if high < n - 1 else float("-inf")
        if above >= below:
            high += 1
            accumulated += bin_volume[high]
            included.add(high)
        else:
            low -= 1
            accumulated += bin_volume[low]
            included.add(low)

    return sorted(included)
