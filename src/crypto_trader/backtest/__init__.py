"""Build Order step 3: the backtest lab (Gate 1).

A no-lookahead replay engine, a realistic cost model (fees, funding, slippage, minimum
notional), the captain-decision-4 funding filter, a parameter sweep with a frozen
out-of-sample score, and markdown/JSON reports. It consumes the unmodified step-2
`generate_signal` core and the step-1 candle storage; it does not change their public
contracts. See AGENTS.md for the step-3 architecture decisions and README.md for how to run.

Public surface:
- causal_windows, run_backtest, run_symbol_backtest: the replay engine and its no-lookahead
  slice.
- BacktestConfig, BacktestResult, SymbolResult, ClosedTrade, ExitType: engine types.
- CostConfig, compute_costs, min_notional_ok: the cost model.
- FundingSchedule, funding_is_prohibitive, constant_schedule: funding cost and the filter.
- TradeStats, compute_stats: expectancy and confidence-interval statistics.
- run_sweep, SweepResult, SweepPoint: the parameter sweep.
- ReportContext, render_markdown, render_json: the reports.
"""

from __future__ import annotations

from crypto_trader.backtest.costs import (
    CostBreakdown,
    CostConfig,
    compute_costs,
    min_notional_ok,
    position_notional,
)
from crypto_trader.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    ClosedTrade,
    ExitType,
    SymbolResult,
    causal_windows,
    run_backtest,
    run_symbol_backtest,
)
from crypto_trader.backtest.funding import (
    FundingSchedule,
    constant_schedule,
    funding_is_prohibitive,
    signed_adverse_rate,
)
from crypto_trader.backtest.metrics import TradeStats, compute_stats
from crypto_trader.backtest.report import ReportContext, render_json, render_markdown
from crypto_trader.backtest.sweep import SweepPoint, SweepResult, run_sweep

__all__ = [
    "CostBreakdown",
    "CostConfig",
    "compute_costs",
    "min_notional_ok",
    "position_notional",
    "BacktestConfig",
    "BacktestResult",
    "ClosedTrade",
    "ExitType",
    "SymbolResult",
    "causal_windows",
    "run_backtest",
    "run_symbol_backtest",
    "FundingSchedule",
    "constant_schedule",
    "funding_is_prohibitive",
    "signed_adverse_rate",
    "TradeStats",
    "compute_stats",
    "SweepPoint",
    "SweepResult",
    "run_sweep",
    "ReportContext",
    "render_json",
    "render_markdown",
]
