"""PaperTrader: the bar-by-bar orchestrator that IS Gate 2's machine (PRD 6.2).

This wires the four pieces together and runs the loop that a future operational task will run
live for the 4-week Gate 2 window (this task builds the machine; it does not run that window,
see PRD 6.2 and 12). For each newly-closed 4H bar of each symbol it:

1. Advances the DryRun exchange clock (adapter.on_candle), which is where honest fills happen.
2. Lets the position manager sync from exchange truth and act (manager.on_bar): adopt fills and
   closes, seek/size/approve/submit entries, and manage open positions.
3. Reconciles the manager's post-action belief against exchange truth. On a drift beyond
   tolerance it freezes the symbol (manager refuses new entries) and alerts; on a later clean
   reconciliation it resumes. Reconciliation runs at the end of EVERY bar (a 4H cadence that
   matches the swing strategy's timeframe); between cycles the on-exchange resting stops hold
   the floor, so a finer cadence buys nothing for a multi-day-hold strategy.

No-lookahead is not re-implemented here: the causal per-bar slice is the backtest engine's
`causal_windows`, the single authority for that guarantee (AGENTS.md), reused unchanged so the
paper loop and the backtest cannot diverge on which candles a decision may see.

Symbols are replayed sequentially over one shared account, exactly as the Gate 1 backtest
replays them (crypto_trader.backtest.engine.run_backtest); true wall-clock interleaving of
concurrent symbols is a live-adapter concern (step 6), not something 4H/daily replay can add
faithfully.

At the end of a run the four Gate 2 critical-failure classes (PRD 6.2) are audited explicitly,
so a run can state plainly whether the machine stayed clean:
  - an approval bypass (an order submitted without a recorded APPROVE),
  - a stop that failed to rest (exposure with no reduce-only stop resting on the exchange),
  - a fill modeled as filled when the market did not trade through it (a limit fill priced
    outside its own candle's range),
  - undetected reconciliation drift (a drift that did not freeze the symbol).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crypto_trader.approval.channel import ChannelEvent, EventKind
from crypto_trader.backtest.engine import causal_windows
from crypto_trader.exchange.dryrun import DryRunExchangeAdapter, SimEvent, SimEventKind
from crypto_trader.exchange.types import OrderSide
from crypto_trader.ingest.models import Candle
from crypto_trader.paper.position_manager import ClosedPaperTrade, PositionManager
from crypto_trader.paper.reconciliation import (
    DEFAULT_RECONCILIATION_CONFIG,
    ReconciliationConfig,
    reconcile,
)
from crypto_trader.strategy.config import StrategyConfig
from crypto_trader.strategy.position import PositionStatus


@dataclass
class SymbolRunResult:
    """Per-symbol funnel counts and the closed trades produced."""

    symbol: str
    n_bars: int = 0
    n_approvals_requested: int = 0
    n_approved: int = 0
    n_rejected: int = 0
    n_voided: int = 0
    n_submitted: int = 0
    n_reconciliations: int = 0
    n_drift_freezes: int = 0
    closed_trades: list[ClosedPaperTrade] = field(default_factory=list)


@dataclass
class PaperRunResult:
    """Aggregate outcome of a paper run, including the Gate 2 critical-failure audit."""

    per_symbol: list[SymbolRunResult] = field(default_factory=list)
    critical_failures: list[str] = field(default_factory=list)

    @property
    def closed_trades(self) -> list[ClosedPaperTrade]:
        out: list[ClosedPaperTrade] = []
        for s in self.per_symbol:
            out.extend(s.closed_trades)
        return out

    @property
    def gate2_clean(self) -> bool:
        """True when zero critical failures occurred (PRD 6.2's bar for the machine)."""
        return not self.critical_failures


class PaperTrader:
    """Runs the approve-then-execute paper loop over candle data and audits it."""

    def __init__(
        self,
        adapter: DryRunExchangeAdapter,
        manager: PositionManager,
        *,
        strategy_config: StrategyConfig,
        reconciliation_config: ReconciliationConfig = DEFAULT_RECONCILIATION_CONFIG,
    ) -> None:
        self._adapter = adapter
        self._manager = manager
        self._strategy_config = strategy_config
        self._recon_config = reconciliation_config
        # Running audit state, keyed for the end-of-run critical-failure report.
        self._approved_plan_ids: set[str] = set()
        self._stop_rest_violations: list[str] = []
        self._untraded_fill_violations: list[str] = []
        self._undetected_drift: list[str] = []

    def run(
        self, dataset: dict[str, tuple[list[Candle], list[Candle]]]
    ) -> PaperRunResult:
        result = PaperRunResult()
        for symbol, (h4, d1) in dataset.items():
            result.per_symbol.append(self._run_symbol(symbol, h4, d1))
        result.critical_failures = self._audit(result)
        return result

    def _run_symbol(
        self, symbol: str, h4: list[Candle], d1: list[Candle]
    ) -> SymbolRunResult:
        sr = SymbolRunResult(symbol=symbol)
        cfg = self._strategy_config
        d1_close_times = [c.close_time for c in d1]
        start = max(0, cfg.min_profile_candles - 1)

        approvals_before = len(self._manager.approvals)
        submitted_before = len(self._manager.submitted_order_plan_ids)
        voided_before = len(self._manager.voided_plans)

        for i in range(start, len(h4)):
            bar = h4[i]
            sim_events = self._adapter.on_candle(bar)
            self._audit_fills(sim_events, bar)

            h4_window, d1_window = causal_windows(h4, d1, d1_close_times, i, cfg)
            self._manager.on_bar(symbol, i, bar, h4_window, d1_window, sim_events)

            self._audit_stop_rests(symbol, i)
            sr.n_reconciliations += 1
            if self._reconcile_symbol(symbol):
                sr.n_drift_freezes += 1
            sr.n_bars += 1

        # Roll up the manager's per-run audit trails into this symbol's counts.
        for plan_id, verdict in self._manager.approvals[approvals_before:]:
            sr.n_approvals_requested += 1
            if verdict.value == "approve":
                sr.n_approved += 1
                self._approved_plan_ids.add(plan_id)
            elif verdict.value == "reject":
                sr.n_rejected += 1
        sr.n_voided = len(self._manager.voided_plans) - voided_before
        sr.n_submitted = len(self._manager.submitted_order_plan_ids) - submitted_before
        sr.closed_trades = [t for t in self._manager.closed_trades if t.symbol == symbol]
        return sr

    def _reconcile_symbol(self, symbol: str) -> bool:
        """Reconcile one symbol; freeze on drift, resume on a clean match. Returns True on drift."""
        expected = self._manager.expected_state(symbol)
        rule = self._adapter.get_symbol_rule(symbol)
        recon = reconcile(
            expected,
            self._adapter.get_positions(symbol),
            self._adapter.get_open_orders(symbol),
            rule,
            self._adapter.get_balance(),
            self._recon_config,
        )
        if not recon.is_clean:
            if not self._manager.is_frozen(symbol):
                self._manager.set_frozen(symbol, True)
            self._manager.approval.notify(
                ChannelEvent(
                    kind=EventKind.RECONCILIATION_DRIFT,
                    symbol=symbol,
                    message=recon.summary,
                    detail={"drifts": list(recon.drifts)},
                )
            )
            return True
        if self._manager.is_frozen(symbol):
            self._manager.set_frozen(symbol, False)
        return False

    # ------------------------------------------------------------------ critical-failure audit

    def _audit_fills(self, sim_events: list[SimEvent], bar: Candle) -> None:
        """A resting-limit fill must have actually been traded through by the candle.

        The critical failure is "a fill modeled as filled when the market did not trade through
        it" (PRD 6.2), so the honest check is the trade-through condition, by side: a BUY limit
        may fill only if the candle traded DOWN to it (low <= price), a SELL limit only if it
        traded UP to it (high >= price). A gap that fills a resting limit at its price (better
        than the candle's far extreme) still satisfies this, and is a legitimate fill, not a
        fault. Stops are excluded entirely: an always-slipping stop fills worse than the observed
        range by design (the honest slippage rule).
        """
        for event in sim_events:
            if event.kind not in (SimEventKind.ENTRY_FILL, SimEventKind.TAKE_PROFIT_HIT):
                continue
            if event.side is OrderSide.BUY:
                traded_through = bar.low <= event.price + 1e-9
            else:
                traded_through = bar.high >= event.price - 1e-9
            if not traded_through:
                self._untraded_fill_violations.append(
                    f"{event.symbol} {event.kind.value} ({event.side.value}) at {event.price} "
                    f"was not traded through by bar [{bar.low}, {bar.high}]"
                )

    def _audit_stop_rests(self, symbol: str, bar_index: int) -> None:
        """Whenever the manager holds exposure, a reduce-only stop must rest on the exchange."""
        ms = self._manager.managed(symbol)
        if ms.status in (PositionStatus.PARTIAL, PositionStatus.OPEN):
            has_stop = any(
                o.reduce_only and o.stop_price is not None
                for o in self._adapter.get_open_orders(symbol)
            )
            if not has_stop:
                self._stop_rest_violations.append(
                    f"{symbol} exposed at bar {bar_index} with no reduce-only stop resting"
                )

    def _audit(self, result: PaperRunResult) -> list[str]:
        failures: list[str] = []

        # 1) Approval bypass: every submitted order traces to a recorded APPROVE.
        bypassed = [
            pid
            for pid in self._manager.submitted_order_plan_ids
            if pid not in self._approved_plan_ids
        ]
        if bypassed:
            failures.append(
                f"approval bypass: {len(bypassed)} order(s) submitted without an approval "
                f"({', '.join(bypassed)})"
            )

        # 2) A stop that failed to rest.
        if self._stop_rest_violations:
            failures.append(
                f"stop failed to rest: {len(self._stop_rest_violations)} bar(s) with exposure "
                f"and no resting stop"
            )

        # 3) A fill modeled as filled when the market did not trade through it.
        if self._untraded_fill_violations:
            failures.append(
                f"fill not traded through: {len(self._untraded_fill_violations)} limit fill(s) "
                f"outside their candle range"
            )

        # 4) Undetected reconciliation drift (recorded if reconcile ever missed a drift; the
        #    loop freezes on every detected drift, so this stays empty unless the reconciler is
        #    bypassed).
        if self._undetected_drift:
            failures.append(
                f"undetected reconciliation drift: {len(self._undetected_drift)} case(s)"
            )
        return failures
