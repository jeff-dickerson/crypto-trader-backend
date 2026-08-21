"""Zone identification, the separation-then-return setup, and entry geometry.

A "zone" is the value area of the volume profile; its boundary on the bias side is
the entry price (VAL for a long, VAH for a short: the value-area EDGE, never the POC
centre line). The setup requires that price separated from that boundary and then
returned to it, and that this is the FIRST return since the separation.

No-lookahead (see AGENTS.md): every decision here uses only the candles in the window
it is handed, whose LAST element is the current decision point. Separation is checked
strictly before the current candle; the return touch happens at the current candle.
The function never inspects any candle after the decision point, because it never
receives one. The future backtest replay engine (step 3) owns feeding a correctly
causally-sliced window; this module's job is only never to break that guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.bias import Bias
from crypto_trader.strategy.config import StrategyConfig
from crypto_trader.strategy.volume_profile import VolumeProfile


@dataclass(frozen=True)
class EntrySetup:
    """A validated entry: the side, the boundary entry price, and stop/take-profit."""

    side: Bias  # LONG or SHORT (never NEUTRAL)
    boundary: float
    entry_price: float
    stop_price: float
    take_profit_price: float


def _longest_run_low_above(candles: list[Candle], threshold: float) -> int:
    """Longest run of consecutive candles trading entirely above `threshold`."""
    best = 0
    run = 0
    for candle in candles:
        if candle.low > threshold:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _longest_run_high_below(candles: list[Candle], threshold: float) -> int:
    """Longest run of consecutive candles trading entirely below `threshold`."""
    best = 0
    run = 0
    for candle in candles:
        if candle.high < threshold:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def detect_entry_setup(
    profile: VolumeProfile,
    window: list[Candle],
    bias: Bias,
    config: StrategyConfig,
) -> EntrySetup | None:
    """Return an EntrySetup if a first-touch-after-separation setup fires, else None.

    `window` is the 4H candle window (same candles used to build `profile`); its last
    element is the current decision point. Only trades in the direction of `bias` are
    considered; a NEUTRAL bias yields None.
    """
    if len(window) < config.separation_min_bars + 1:
        return None
    if bias is Bias.LONG:
        return _detect_long(profile, window, config)
    if bias is Bias.SHORT:
        return _detect_short(profile, window, config)
    return None


def _detect_long(
    profile: VolumeProfile, window: list[Candle], config: StrategyConfig
) -> EntrySetup | None:
    boundary = profile.val
    current = window[-1]

    # Return touch from above: the current candle opened above the boundary and traded
    # down to it (a resting buy limit at the boundary fills).
    if not (current.low <= boundary < current.open):
        return None

    # First touch: find the most recent earlier touch of the boundary. Separation must
    # occur after it, and no touch may sit between it and the current candle.
    prior = window[:-1]
    last_touch = _last_index(prior, lambda c: c.low <= boundary)
    segment = prior[last_touch + 1 :]

    threshold = boundary * (1.0 + config.separation_pct)
    if _longest_run_low_above(segment, threshold) < config.separation_min_bars:
        return None

    stop_price = _long_stop(profile, boundary, config)
    take_profit_price = _long_take_profit(profile, boundary, stop_price, config)
    return EntrySetup(
        side=Bias.LONG,
        boundary=boundary,
        entry_price=boundary,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def _detect_short(
    profile: VolumeProfile, window: list[Candle], config: StrategyConfig
) -> EntrySetup | None:
    boundary = profile.vah
    current = window[-1]

    # Return touch from below: opened below the boundary and traded up to it.
    if not (current.high >= boundary > current.open):
        return None

    prior = window[:-1]
    last_touch = _last_index(prior, lambda c: c.high >= boundary)
    segment = prior[last_touch + 1 :]

    threshold = boundary * (1.0 - config.separation_pct)
    if _longest_run_high_below(segment, threshold) < config.separation_min_bars:
        return None

    stop_price = _short_stop(profile, boundary, config)
    take_profit_price = _short_take_profit(profile, boundary, stop_price, config)
    return EntrySetup(
        side=Bias.SHORT,
        boundary=boundary,
        entry_price=boundary,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def _long_stop(profile: VolumeProfile, entry: float, config: StrategyConfig) -> float:
    """Stop in the adjacent LVN just below the entry zone, else a fixed offset."""
    lvn = profile.nearest_lvn_below(entry)
    if lvn is not None and lvn.center < entry:
        return lvn.center * (1.0 - config.stop_lvn_buffer_pct)
    return entry * (1.0 - config.stop_fallback_pct)


def _short_stop(profile: VolumeProfile, entry: float, config: StrategyConfig) -> float:
    """Stop in the adjacent LVN just above the entry zone, else a fixed offset."""
    lvn = profile.nearest_lvn_above(entry)
    if lvn is not None and lvn.center > entry:
        return lvn.center * (1.0 + config.stop_lvn_buffer_pct)
    return entry * (1.0 + config.stop_fallback_pct)


def _long_take_profit(
    profile: VolumeProfile, entry: float, stop: float, config: StrategyConfig
) -> float:
    """Take profit just before the next opposing HVN above, else an R-multiple."""
    hvn = profile.nearest_hvn_above(profile.vah)
    if hvn is not None:
        target = hvn.low * (1.0 - config.tp_hvn_buffer_pct)
        if target > entry:
            return target
    return entry + config.tp_fallback_rr * (entry - stop)


def _short_take_profit(
    profile: VolumeProfile, entry: float, stop: float, config: StrategyConfig
) -> float:
    """Take profit just before the next opposing HVN below, else an R-multiple."""
    hvn = profile.nearest_hvn_below(profile.val)
    if hvn is not None:
        target = hvn.high * (1.0 + config.tp_hvn_buffer_pct)
        if target < entry:
            return target
    return entry - config.tp_fallback_rr * (stop - entry)


def _last_index(candles: list[Candle], predicate) -> int:
    """Index of the last candle satisfying `predicate`, or -1 if none does."""
    for i in range(len(candles) - 1, -1, -1):
        if predicate(candles[i]):
            return i
    return -1


def is_zone_already_traded(
    boundary: float, traded_zones: tuple[float, ...], config: StrategyConfig
) -> bool:
    """One zone, one trade: True if `boundary` matches an already-traded zone.

    Two boundaries within zone_match_pct of each other are the same zone, so a slightly
    drifted recomputed boundary still counts as the already-traded zone.
    """
    return any(
        abs(boundary - traded) <= config.zone_match_pct * abs(boundary)
        for traded in traded_zones
    )
