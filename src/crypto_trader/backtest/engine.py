"""No-lookahead replay engine: the safety-critical core of the Gate 1 backtest lab.

Build Order step 2 left one guarantee explicitly to this step (see AGENTS.md, "No-lookahead
responsibility split"): the pure `generate_signal` core TRUSTS that it is handed a
correctly, causally-sliced window, and this engine OWNS building and defending that slice.
Every principle in PRD sections 2-3 and 7 flows from it, so it is isolated here in one small,
heavily-tested function, `causal_windows`, rather than smeared through the simulation loop.

The design in one paragraph. For each simulated 4H decision bar i, `causal_windows` returns
(a) the 4H window ending exactly at bar i and (b) the daily window of candles CLOSED at or
before bar i's close, and nothing later, ever. That window is handed to the unmodified
`generate_signal`, which recomputes the volume profile and bias from exactly those candles.
The profile is therefore recomputed per decision bar from a rolling window, never precomputed
once over the whole dataset (the thing the task forbids); a distinctive candle planted just
after the decision point cannot change the decision, which the regression tests assert
directly. Fills and management are then simulated with a CONSERVATIVE intrabar ordering rule
(assume the worse of stop-vs-take-profit within any one bar), because 4H+daily data cannot
resolve the true within-bar path (AGENTS.md "Data plan").

Performance note (measured, not asserted). The dominant per-bar cost is the pure
`build_volume_profile` recompute inside `generate_signal` (about 227us over a 360-candle
window; a full single-symbol 3-year run is roughly 1.5s, a 12-symbol 8-combination sweep
about 2 minutes). Two provably-outcome-preserving accelerations are applied: an O(log n)
bisect for the causal daily slice instead of an O(n) rescan, and a neutral-bias skip that
avoids the profile build on flat bars where `generate_signal` would return NO_SIGNAL before
building it anyway. A faster incremental-bin volume profile was measured (about 92% of rolling
slides leave the window's price extremes unchanged, so an add/remove update would be roughly
8x faster) but deliberately NOT wired into the decision path: `generate_signal`'s signature is
frozen and it is the single decision authority, so injecting a second profile implementation
would risk floating-point-drift decision divergence between backtest and live, which conduct
rule 7 forbids trading for speed. It is recorded as the measured next optimization if scale
ever demands it.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from crypto_trader.backtest.costs import CostConfig, compute_costs, min_notional_ok
from crypto_trader.backtest.funding import FundingSchedule, funding_is_prohibitive
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.bias import Bias, compute_bias
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from crypto_trader.strategy.position import PositionSide, PositionState, PositionStatus
from crypto_trader.strategy.signal import SignalAction, generate_signal


class ExitType(str, Enum):
    """How a filled position left the market."""

    STOP = "stop"  # the initial (untrailed) stop was hit
    TRAILING_STOP = "trailing_stop"  # a stop the trail had ratcheted tighter was hit
    TAKE_PROFIT = "take_profit"  # the resting take-profit limit was hit
    MOMENTUM_SHIFT = "momentum_shift"  # generate_signal's MA-ensemble exit fired


@dataclass(frozen=True)
class ClosedTrade:
    """One fully closed trade, scored in R after costs. The expectancy unit."""

    symbol: str
    side: PositionSide
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    initial_stop: float
    take_profit: float
    exit_type: ExitType
    bars_held: int
    funding_fraction: float
    gross_r: float
    net_r: float


@dataclass(frozen=True)
class BacktestConfig:
    """Engine (not strategy) parameters: fill realism and simulation bounds.

    These govern the SIMULATION, not the decision. Strategy parameters live on
    StrategyConfig; costs on CostConfig; funding on the FundingSchedule.
    """

    # A resting entry limit placed at the signal bar's close is valid for this many
    # SUBSEQUENT bars. It fills on the first of those bars that trades through the boundary;
    # if none does, the order is cancelled (a signal that never filled). Modelling the fill
    # on a later bar, never the signal bar itself, is the conservative no-lookahead choice
    # and is exactly what produces the "signals that never fill" the PRD 6.1 denominator
    # discussion calls out. 6 bars is one calendar day of 4H bars.
    entry_valid_bars: int = 6


@dataclass
class SymbolResult:
    """Outcome for one symbol: the funnel counts plus the closed trades.

    The three headline denominators the PRD requires are all distinct here:
    `n_signals` (setups that fired a resting entry ORDER, after the funding and
    minimum-notional filters), `n_fills` (orders whose limit actually traded through),
    and `len(closed_trades)` (fills that reached a terminal exit; still-open-at-end fills
    are counted in `n_open_at_end`, never as closed trades).
    """

    symbol: str
    n_setups: int = 0  # raw ENTER decisions from generate_signal
    n_funding_filtered: int = 0  # setups skipped by the captain-decision-4 funding gate
    n_min_notional_voided: int = 0  # setups voided because the sized trade is sub-minimum
    n_signals: int = 0  # setups that actually placed a resting entry order
    n_fills: int = 0  # placed orders whose limit filled
    n_open_at_end: int = 0  # fills still open when the data ran out (not closed trades)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    @property
    def n_closed(self) -> int:
        return len(self.closed_trades)


@dataclass
class BacktestResult:
    """Aggregate across every symbol in a run."""

    per_symbol: list[SymbolResult] = field(default_factory=list)

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        out: list[ClosedTrade] = []
        for s in self.per_symbol:
            out.extend(s.closed_trades)
        return out

    def _sum(self, attr: str) -> int:
        return sum(getattr(s, attr) for s in self.per_symbol)

    @property
    def n_setups(self) -> int:
        return self._sum("n_setups")

    @property
    def n_funding_filtered(self) -> int:
        return self._sum("n_funding_filtered")

    @property
    def n_min_notional_voided(self) -> int:
        return self._sum("n_min_notional_voided")

    @property
    def n_signals(self) -> int:
        return self._sum("n_signals")

    @property
    def n_fills(self) -> int:
        return self._sum("n_fills")

    @property
    def n_open_at_end(self) -> int:
        return self._sum("n_open_at_end")

    @property
    def n_closed(self) -> int:
        return len(self.closed_trades)


def causal_windows(
    h4: list[Candle],
    d1: list[Candle],
    d1_close_times: list[datetime],
    i: int,
    config: StrategyConfig,
) -> tuple[list[Candle], list[Candle]]:
    """The no-lookahead slice for decision bar i. THE safety-critical function.

    Returns (h4_window, d1_window) where:
    - h4_window is the up-to-`lookback_candles` 4H tail ending exactly at bar i, so its
      last element is the decision point and no later 4H candle is included.
    - d1_window is the daily tail ending at the last daily candle CLOSED at or before bar i's
      close, and no daily candle that closes later.

    `d1_close_times` is the precomputed ascending list of daily close_times (passed in so
    the bisect is not rebuilt every call). Both returned lists contain ONLY candles at or
    before bar i's close: that is the whole guarantee this engine exists to provide, and the
    regression tests assert it by planting future candles that must not change the result.

    The daily tail is trimmed to the last `bias_slow_period` candles, which is a
    provably-identical optimization: the bias gate is the only consumer of the daily window
    and it reads only the last `bias_slow_period` closes (the slow SMA is the longest lookback
    it takes), so trimming older daily candles cannot change the bias while it avoids rebuilding
    a multi-year closes list on every bar.
    """
    decision_close = h4[i].close_time
    lb = config.lookback_candles
    h4_window = h4[max(0, i + 1 - lb) : i + 1]
    di = bisect_right(d1_close_times, decision_close)
    d1_window = d1[max(0, di - config.bias_slow_period) : di]
    return h4_window, d1_window


@dataclass
class _OpenPosition:
    """Engine-local mutable bookkeeping for a live simulated position.

    A PositionState (frozen, the shape generate_signal consumes) is rebuilt from this each
    management bar; generate_signal never mutates state, so the engine threads the update.
    """

    side: PositionSide
    entry_price: float
    initial_stop: float
    take_profit: float
    current_stop: float
    extreme_price: float
    entry_time: datetime
    entry_bar: int
    stop_was_trailed: bool = False

    def to_state(self) -> PositionState:
        return PositionState(
            status=PositionStatus.OPEN,
            side=self.side,
            filled_fraction=1.0,
            entry_price=self.entry_price,
            initial_stop=self.initial_stop,
            take_profit=self.take_profit,
            current_stop=self.current_stop,
            extreme_price=self.extreme_price,
        )


@dataclass
class _PendingOrder:
    """A resting entry limit awaiting a trade-through fill."""

    side: PositionSide
    boundary: float
    stop: float
    take_profit: float
    placed_bar: int


def run_symbol_backtest(
    symbol: str,
    h4: list[Candle],
    d1: list[Candle],
    *,
    strategy_config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    cost_config: CostConfig | None = None,
    funding: FundingSchedule | None = None,
    backtest_config: BacktestConfig | None = None,
) -> SymbolResult:
    """Replay one symbol bar by bar with strict no-lookahead slicing.

    The loop is a small state machine: FLAT seeks an entry (via generate_signal on the
    causal window), a placed order rests until it fills or expires, and a filled position
    is managed with the conservative intrabar rule until it exits. Fills are modelled as
    FULL on trade-through: faithful partial-fill simulation needs order-book depth the
    4H/daily data plan does not carry (AGENTS.md "Data plan"), so the PARTIAL state stays a
    paper/live concern and is not fabricated here.
    """
    cost_config = cost_config or CostConfig()
    backtest_config = backtest_config or BacktestConfig()
    if funding is None:
        funding = FundingSchedule(rates_by_symbol={})

    result = SymbolResult(symbol=symbol)
    d1_close_times = [c.close_time for c in d1]
    lb = strategy_config.lookback_candles
    traded_zones: list[float] = []

    pending: _PendingOrder | None = None
    position: _OpenPosition | None = None

    start = max(0, strategy_config.min_profile_candles - 1)
    for i in range(start, len(h4)):
        bar = h4[i]

        # 1) Manage a live position first: its stop/take-profit rest across bars.
        if position is not None:
            closed = _manage_open_position(
                symbol, position, bar, h4, i, lb, d1_close_times, d1,
                strategy_config, cost_config, funding,
            )
            if closed is not None:
                result.closed_trades.append(closed)
                position = None
            continue

        # 2) A resting entry order: try to fill it on this bar, or expire it.
        if pending is not None:
            filled = _try_fill(pending, bar)
            if filled:
                result.n_fills += 1
                position = _open_from_fill(pending, bar, i)
                pending = None
                # A position filled this bar is managed from the NEXT bar: the fill bar's
                # post-fill intrabar path is unknowable, so we only apply the conservative
                # same-bar stop breach here.
                stopped = _same_bar_stop_breach(position, bar)
                if stopped is not None:
                    result.closed_trades.append(
                        _close_trade(symbol, position, bar, stopped, ExitType.STOP,
                                     i, cost_config, funding, exit_is_maker=False)
                    )
                    position = None
                continue
            if i - pending.placed_bar >= backtest_config.entry_valid_bars:
                pending = None  # expired unfilled; the zone stays recorded as attempted
            # fall through: FLAT this bar, may seek a new entry below only if no pending
            if pending is not None:
                continue

        # 3) FLAT: seek an entry. Neutral-bias skip is a provably-identical shortcut, since
        # generate_signal returns NO_SIGNAL on a neutral bias before it builds any profile.
        h4_window, d1_window = causal_windows(h4, d1, d1_close_times, i, strategy_config)
        if compute_bias(d1_window, strategy_config) is Bias.NEUTRAL:
            continue

        state = PositionState.flat(traded_zones=tuple(traded_zones))
        signal = generate_signal(h4_window, d1_window, state, strategy_config)
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            continue

        result.n_setups += 1
        side = (
            PositionSide.LONG if signal.action is SignalAction.ENTER_LONG
            else PositionSide.SHORT
        )
        rate = funding.rate_at(symbol, bar.close_time)
        if funding_is_prohibitive(rate, side, strategy_config):
            result.n_funding_filtered += 1
            continue
        if not min_notional_ok(signal.entry_price, signal.stop_price, cost_config):
            result.n_min_notional_voided += 1
            continue

        result.n_signals += 1
        traded_zones.append(signal.zone_boundary)
        pending = _PendingOrder(
            side=side,
            boundary=signal.entry_price,
            stop=signal.stop_price,
            take_profit=signal.take_profit_price,
            placed_bar=i,
        )

    if position is not None:
        result.n_open_at_end += 1
    return result


def _try_fill(order: _PendingOrder, bar: Candle) -> bool:
    """A resting limit fills when the bar trades through it (full fill on trade-through)."""
    if order.side is PositionSide.LONG:
        return bar.low <= order.boundary
    return bar.high >= order.boundary


def _open_from_fill(order: _PendingOrder, bar: Candle, bar_index: int) -> _OpenPosition:
    """Open a position at the limit price. Entry time is the fill bar's open (conservative
    for funding: the earliest instant the position could have existed, so it is charged the
    most settlements)."""
    return _OpenPosition(
        side=order.side,
        entry_price=order.boundary,
        initial_stop=order.stop,
        take_profit=order.take_profit,
        current_stop=order.stop,
        extreme_price=order.boundary,
        entry_time=bar.open_time,
        entry_bar=bar_index,
    )


def _same_bar_stop_breach(position: _OpenPosition, bar: Candle) -> float | None:
    """The stop price if the FILL bar's own range already breached the stop, else None.

    Applying the worst-of-intrabar rule to the fill bar: if the same candle that filled the
    entry also reached the stop, we cannot know the entry filled first, so we conservatively
    treat it as stopped out on the fill bar.
    """
    if position.side is PositionSide.LONG:
        return position.initial_stop if bar.low <= position.initial_stop else None
    return position.initial_stop if bar.high >= position.initial_stop else None


def _manage_open_position(
    symbol: str,
    position: _OpenPosition,
    bar: Candle,
    h4: list[Candle],
    i: int,
    lb: int,
    d1_close_times: list[datetime],
    d1: list[Candle],
    strategy_config: StrategyConfig,
    cost_config: CostConfig,
    funding: FundingSchedule,
) -> ClosedTrade | None:
    """Run one management bar. Returns a ClosedTrade if the position exited, else None.

    Ordering enforces both no-lookahead and conservatism:
    1) Stop and take-profit are exchange-resting orders, checked intrabar against this bar's
       range. If BOTH are within the bar, the STOP is assumed to fill first (the worse
       outcome), per AGENTS.md's conservative intrabar rule.
    2) Only if no price level is hit do we consult generate_signal, whose momentum-shift exit
       is a close-based decision (so it cannot pre-empt an intrabar stop) and whose trailing
       update tightens the resting stop for the NEXT bar, never retroactively this one.
    """
    stop = position.current_stop
    tp = position.take_profit
    stop_type = ExitType.TRAILING_STOP if position.stop_was_trailed else ExitType.STOP

    if position.side is PositionSide.LONG:
        stop_hit = bar.low <= stop
        tp_hit = bar.high >= tp
    else:
        stop_hit = bar.high >= stop
        tp_hit = bar.low <= tp

    if stop_hit:  # worst-of-intrabar: stop wins ties with the take-profit
        return _close_trade(symbol, position, bar, stop, stop_type, i,
                            cost_config, funding, exit_is_maker=False)
    if tp_hit:
        return _close_trade(symbol, position, bar, tp, ExitType.TAKE_PROFIT, i,
                            cost_config, funding, exit_is_maker=True)

    # No price level hit: ask the pure core about momentum-shift / trailing.
    h4_window = h4[max(0, i + 1 - lb) : i + 1]
    signal = generate_signal(h4_window, [], position.to_state(), strategy_config)
    if signal.action is SignalAction.EXIT_MOMENTUM_SHIFT:
        return _close_trade(symbol, position, bar, bar.close, ExitType.MOMENTUM_SHIFT, i,
                            cost_config, funding, exit_is_maker=False)
    if signal.action is SignalAction.UPDATE_TRAILING_STOP and signal.stop_price is not None:
        position.current_stop = signal.stop_price
        position.stop_was_trailed = True

    # Advance the favourable-excursion extreme for the next bar's trailing computation.
    if position.side is PositionSide.LONG:
        position.extreme_price = max(position.extreme_price, bar.high)
    else:
        position.extreme_price = min(position.extreme_price, bar.low)
    return None


def _close_trade(
    symbol: str,
    position: _OpenPosition,
    bar: Candle,
    raw_exit_price: float,
    exit_type: ExitType,
    bar_index: int,
    cost_config: CostConfig,
    funding: FundingSchedule,
    *,
    exit_is_maker: bool,
) -> ClosedTrade:
    """Score a closing trade in R after fees, slippage, and funding over the hold."""
    funding_fraction = funding.funding_cost_fraction(
        symbol, position.side, position.entry_time, bar.close_time
    )
    breakdown = compute_costs(
        position.side,
        position.entry_price,
        raw_exit_price,
        position.initial_stop,
        exit_is_maker=exit_is_maker,
        funding_fraction=funding_fraction,
        config=cost_config,
    )
    return ClosedTrade(
        symbol=symbol,
        side=position.side,
        entry_time=position.entry_time,
        exit_time=bar.close_time,
        entry_price=position.entry_price,
        exit_price=raw_exit_price,
        initial_stop=position.initial_stop,
        take_profit=position.take_profit,
        exit_type=exit_type,
        bars_held=bar_index - position.entry_bar,
        funding_fraction=funding_fraction,
        gross_r=breakdown.gross_r,
        net_r=breakdown.net_r,
    )


def run_backtest(
    dataset: dict[str, tuple[list[Candle], list[Candle]]],
    *,
    strategy_config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    cost_config: CostConfig | None = None,
    funding: FundingSchedule | None = None,
    backtest_config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay every symbol in `dataset` (symbol -> (4H candles, daily candles))."""
    result = BacktestResult()
    for symbol, (h4, d1) in dataset.items():
        result.per_symbol.append(
            run_symbol_backtest(
                symbol, h4, d1,
                strategy_config=strategy_config,
                cost_config=cost_config,
                funding=funding,
                backtest_config=backtest_config,
            )
        )
    return result
