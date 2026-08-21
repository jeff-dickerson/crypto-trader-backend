"""generate_signal: the pure decision core of the v1 strategy.

generate_signal(candles, htf_candles, position_state, config) is a real pure function:
no wall-clock reads (no datetime.now), no I/O, no hidden global state. It trusts that
every candle it receives is already closed (step 1's ingest layer guarantees this) and
that both lists are ascending; it does not re-check "now" itself. The current decision
point is always the LAST 4H candle (candles[-1]).

- candles: the primary 4H timeframe. Drives the volume profile, the separation-then-
  return setup, and the trailing-stop / MA-ensemble management.
- htf_candles: the higher (daily) timeframe. Drives the bias gate only.

No-lookahead responsibility split (see AGENTS.md): the future backtest replay engine
(step 3) owns feeding this function a correctly causally-sliced, growing window. This
function's only job is never to break that guarantee on its own, which it does by
computing every profile and bias from exactly the candles present and never reading a
candle beyond the decision point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.bias import Bias, compute_bias
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from crypto_trader.strategy.indicators import sma
from crypto_trader.strategy.position import PositionSide, PositionState, PositionStatus
from crypto_trader.strategy.volume_profile import build_volume_profile
from crypto_trader.strategy.zones import detect_entry_setup, is_zone_already_traded


class SignalAction(str, Enum):
    """What generate_signal is telling the caller to do."""

    NO_SIGNAL = "no_signal"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    UPDATE_TRAILING_STOP = "update_trailing_stop"
    EXIT_MOMENTUM_SHIFT = "exit_momentum_shift"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    """A single decision. Frozen, so identical inputs give an equal result.

    entry_price/stop_price/take_profit_price/zone_boundary are populated for the
    ENTER_LONG and ENTER_SHORT actions. For UPDATE_TRAILING_STOP, stop_price carries
    the new (tighter) resting stop level. `reason` is human-readable context for logs
    and tests. New fields can be added with defaults without breaking callers.
    """

    action: SignalAction
    symbol: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    zone_boundary: float | None = None
    bias: Bias | None = None
    reason: str = ""


def generate_signal(
    candles: list[Candle],
    htf_candles: list[Candle],
    position_state: PositionState,
    config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
) -> Signal:
    """Decide the action at candles[-1]. Pure: identical inputs give an equal Signal."""
    symbol = _symbol_of(candles, htf_candles)

    if position_state.has_exposure:
        return _manage_position(candles, position_state, config, symbol)
    if position_state.status is PositionStatus.PENDING:
        return Signal(
            action=SignalAction.HOLD,
            symbol=symbol,
            reason="entry limit resting, awaiting fill",
        )
    return _seek_entry(candles, htf_candles, position_state, config, symbol)


def _seek_entry(
    candles: list[Candle],
    htf_candles: list[Candle],
    position_state: PositionState,
    config: StrategyConfig,
    symbol: str | None,
) -> Signal:
    if not candles:
        return Signal(SignalAction.NO_SIGNAL, symbol, reason="no candles")

    bias = compute_bias(htf_candles, config)
    if bias is Bias.NEUTRAL:
        return Signal(
            SignalAction.NO_SIGNAL, symbol, bias=bias, reason="neutral bias, no entry"
        )

    window = candles[-config.lookback_candles :]
    if len(window) < config.min_profile_candles:
        return Signal(
            SignalAction.NO_SIGNAL,
            symbol,
            bias=bias,
            reason="insufficient 4h history for a trusted profile",
        )

    profile = build_volume_profile(window, config)
    if profile is None:
        return Signal(
            SignalAction.NO_SIGNAL, symbol, bias=bias, reason="no volume profile"
        )

    boundary = profile.val if bias is Bias.LONG else profile.vah
    if is_zone_already_traded(boundary, position_state.traded_zones, config):
        return Signal(
            SignalAction.NO_SIGNAL,
            symbol,
            bias=bias,
            zone_boundary=boundary,
            reason="zone already traded (one zone, one trade)",
        )

    setup = detect_entry_setup(profile, window, bias, config)
    if setup is None:
        return Signal(
            SignalAction.NO_SIGNAL,
            symbol,
            bias=bias,
            zone_boundary=boundary,
            reason="no separation-then-return setup at the boundary",
        )

    action = (
        SignalAction.ENTER_LONG if setup.side is Bias.LONG else SignalAction.ENTER_SHORT
    )
    return Signal(
        action=action,
        symbol=symbol,
        entry_price=setup.entry_price,
        stop_price=setup.stop_price,
        take_profit_price=setup.take_profit_price,
        zone_boundary=setup.boundary,
        bias=bias,
        reason="first-touch value-area boundary retest",
    )


def _manage_position(
    candles: list[Candle],
    position_state: PositionState,
    config: StrategyConfig,
    symbol: str | None,
) -> Signal:
    if not candles or position_state.side is None:
        return Signal(
            SignalAction.HOLD, symbol, reason="no candles or side to manage against"
        )

    current = candles[-1]
    closes = [c.close for c in candles]
    side = position_state.side

    # Momentum-shift exit takes priority: if the ensemble has flipped, exit fully
    # rather than merely trail.
    if _momentum_shifted(closes, side, config):
        return Signal(
            SignalAction.EXIT_MOMENTUM_SHIFT,
            symbol,
            reason="ma ensemble flipped against the position",
        )

    new_stop = _trailing_stop_update(position_state, current, config)
    if new_stop is not None:
        return Signal(
            SignalAction.UPDATE_TRAILING_STOP,
            symbol,
            stop_price=new_stop,
            reason="trailing stop advanced",
        )

    return Signal(
        SignalAction.HOLD,
        symbol,
        reason="position held; no momentum shift, no trailing advance",
    )


def _momentum_shifted(
    closes: list[float], side: PositionSide, config: StrategyConfig
) -> bool:
    """True when a majority of the MA ensemble has flipped against the position.

    Returns False when the full ensemble is not yet computable: we do not declare a
    shift we cannot confirm, so the position is held.
    """
    mas = [sma(closes, period) for period in config.ma_ensemble_periods]
    if any(ma is None for ma in mas):
        return False
    last_close = closes[-1]
    if side is PositionSide.LONG:
        votes = sum(1 for ma in mas if last_close < ma)
    else:
        votes = sum(1 for ma in mas if last_close > ma)
    return votes >= config.ma_exit_min_votes


def _trailing_stop_update(
    state: PositionState, current: Candle, config: StrategyConfig
) -> float | None:
    """The new resting stop if the trail has armed and advanced, else None.

    The trail arms once the favourable excursion reaches trail_activation_rr x R and
    then sits trail_distance_rr x R behind the best price. It only ever ratchets in the
    favourable direction and is never placed on the wrong side of the last close.
    """
    entry = state.entry_price
    initial_stop = state.initial_stop
    if entry is None or initial_stop is None:
        return None
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None

    reference_stop = state.resting_stop
    if reference_stop is None:
        return None

    if state.side is PositionSide.LONG:
        extreme = max(
            state.extreme_price if state.extreme_price is not None else entry,
            current.high,
        )
        if extreme - entry < config.trail_activation_rr * risk:
            return None
        new_stop = extreme - config.trail_distance_rr * risk
        if new_stop > reference_stop and new_stop < current.close:
            return new_stop
        return None

    extreme = min(
        state.extreme_price if state.extreme_price is not None else entry,
        current.low,
    )
    if entry - extreme < config.trail_activation_rr * risk:
        return None
    new_stop = extreme + config.trail_distance_rr * risk
    if new_stop < reference_stop and new_stop > current.close:
        return new_stop
    return None


def _symbol_of(candles: list[Candle], htf_candles: list[Candle]) -> str | None:
    if candles:
        return candles[-1].symbol
    if htf_candles:
        return htf_candles[-1].symbol
    return None
