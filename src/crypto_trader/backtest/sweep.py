"""Parameter sweep with a frozen out-of-sample score, the shape of the Gate 1 gate.

Two axes are swept (PRD 6.2, task component 5):
- the lookback, currently pinned at 60 days on StrategyConfig, across a small range inside
  the spec's 30-90 day bound (default 45 / 60 / 75), and
- the captain-decision-4 funding filter, OFF and ON, so the report can show whether the
  filter actually improves the out-of-sample result rather than assuming it does.

The honesty rule of the gate (original spec, restated in PRD 6.2): tuning is done on the
first two-thirds of the timeline and the score is FROZEN and reported only on the final
third. The engine runs once per combination over the whole timeline; each closed trade is
then assigned to in-sample or out-of-sample by its ENTRY time relative to the 2/3 split.
The chosen combination is selected by IN-SAMPLE mean R only; its OUT-OF-SAMPLE statistics
are the honest gate number. Every reported figure is labelled IS or OOS, and selecting on
OOS would be cheating, so the code never does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.backtest.engine import (
    BacktestConfig,
    ClosedTrade,
    run_backtest,
)
from crypto_trader.backtest.funding import FundingSchedule
from crypto_trader.backtest.metrics import TradeStats, compute_stats
from crypto_trader.ingest.models import Candle
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig

DEFAULT_LOOKBACK_DAYS = (45, 60, 75)


@dataclass(frozen=True)
class SweepPoint:
    """One swept combination and its split, labelled statistics, and funnel counts."""

    lookback_days: int
    funding_filter_enabled: bool
    is_stats: TradeStats
    oos_stats: TradeStats
    # Whole-run funnel counts (not split): the three PRD denominators plus the filters.
    n_setups: int
    n_funding_filtered: int
    n_min_notional_voided: int
    n_signals: int
    n_fills: int
    n_closed: int
    n_open_at_end: int


@dataclass(frozen=True)
class SweepResult:
    """The full sweep: every combination, the split point, and the IS-selected winner."""

    points: list[SweepPoint]
    split_time: datetime
    data_start: datetime
    data_end: datetime
    chosen_index: int

    @property
    def chosen(self) -> SweepPoint:
        return self.points[self.chosen_index]


def _timeline_bounds(
    dataset: dict[str, tuple[list[Candle], list[Candle]]],
) -> tuple[datetime, datetime]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for h4, _d1 in dataset.values():
        if h4:
            starts.append(h4[0].open_time)
            ends.append(h4[-1].close_time)
    if not starts:
        raise ValueError("dataset has no 4H candles")
    return min(starts), max(ends)


def _split_trades(
    trades: list[ClosedTrade], split_time: datetime
) -> tuple[list[ClosedTrade], list[ClosedTrade]]:
    """Partition closed trades into (in-sample, out-of-sample) by ENTRY time."""
    in_sample = [t for t in trades if t.entry_time < split_time]
    out_sample = [t for t in trades if t.entry_time >= split_time]
    return in_sample, out_sample


def run_sweep(
    dataset: dict[str, tuple[list[Candle], list[Candle]]],
    *,
    base_config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    cost_config: CostConfig | None = None,
    funding: FundingSchedule | None = None,
    backtest_config: BacktestConfig | None = None,
    lookback_days: tuple[int, ...] = DEFAULT_LOOKBACK_DAYS,
    filter_states: tuple[bool, ...] = (False, True),
    oos_fraction: float = 1.0 / 3.0,
    bootstrap_resamples: int = 2000,
) -> SweepResult:
    """Run every (lookback, funding-filter) combination and freeze the OOS score.

    `oos_fraction` is the final fraction of the timeline reserved out of sample (1/3 per the
    spec). The split time is shared across every combination so they are compared on the same
    frozen OOS window.
    """
    cost_config = cost_config or CostConfig()
    data_start, data_end = _timeline_bounds(dataset)
    span = data_end - data_start
    split_time = data_start + timedelta(
        seconds=span.total_seconds() * (1.0 - oos_fraction)
    )
    risk_fraction = cost_config.risk_fraction

    points: list[SweepPoint] = []
    for lb in lookback_days:
        for filt in filter_states:
            cfg = replace(
                base_config, lookback_days=lb, funding_filter_enabled=filt
            )
            result = run_backtest(
                dataset,
                strategy_config=cfg,
                cost_config=cost_config,
                funding=funding,
                backtest_config=backtest_config,
            )
            is_t, oos_t = _split_trades(result.closed_trades, split_time)
            points.append(
                SweepPoint(
                    lookback_days=lb,
                    funding_filter_enabled=filt,
                    is_stats=compute_stats(
                        is_t, risk_fraction=risk_fraction,
                        bootstrap_resamples=bootstrap_resamples,
                    ),
                    oos_stats=compute_stats(
                        oos_t, risk_fraction=risk_fraction,
                        bootstrap_resamples=bootstrap_resamples,
                    ),
                    n_setups=result.n_setups,
                    n_funding_filtered=result.n_funding_filtered,
                    n_min_notional_voided=result.n_min_notional_voided,
                    n_signals=result.n_signals,
                    n_fills=result.n_fills,
                    n_closed=result.n_closed,
                    n_open_at_end=result.n_open_at_end,
                )
            )

    chosen_index = _select_by_in_sample(points)
    return SweepResult(
        points=points,
        split_time=split_time,
        data_start=data_start,
        data_end=data_end,
        chosen_index=chosen_index,
    )


def _select_by_in_sample(points: list[SweepPoint]) -> int:
    """Pick the combination with the best IN-SAMPLE mean R (never the OOS score).

    Ties (including the all-empty case) fall to the earliest combination, which keeps the
    selection deterministic.
    """
    best_index = 0
    best_key = None
    for idx, p in enumerate(points):
        key = (p.is_stats.n > 0, p.is_stats.mean_r)
        if best_key is None or key > best_key:
            best_key = key
            best_index = idx
    return best_index
