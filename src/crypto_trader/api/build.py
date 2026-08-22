"""Wire an ApiContext to a paper machine, for the runnable server and for tests.

This is the one place the API's live components are assembled: the DryRun adapter, the persistence
store (which is also the paper machine's write sink), the kill switch and its monitor, the position
manager, and the risk config. Everything shares the one store instance so writes the loop makes are
immediately readable through the API, and the one KillSwitch/monitor so the API's kill-switch
surface and the loop's auto-triggers act on the same state (hosting decision 5: one process).

`defer_approval` selects the approval model: True (the REST contract) parks each proposed plan for
an async decision through POST /approvals/{id}/decision; False keeps the synchronous auto-approve
flow used to pre-populate a demo history.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import sleep as _real_sleep

from crypto_trader.api.context import ApiContext, RiskState
from crypto_trader.api.store import ApiStore
from crypto_trader.approval.channel import ApprovalChannel
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.ingest.models import utc_now
from crypto_trader.paper.position_manager import PositionManager
from crypto_trader.paper.risk import DEFAULT_RISK_CONFIG, RiskConfig
from crypto_trader.safety.kill_switch import KillSwitch
from crypto_trader.safety.monitor import KillSwitchMonitor
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig


def build_context(
    store: ApiStore,
    *,
    adapter: DryRunExchangeAdapter | None = None,
    approval: ApprovalChannel | None = None,
    strategy_config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
    slider: float = 1.0,
    defer_approval: bool = True,
    bias_provider: Callable[[], dict[str, str]] | None = None,
    clock: Callable[[], datetime] = utc_now,
    monitor_sleep_fn: Callable[[float], None] = _real_sleep,
) -> ApiContext:
    """Assemble an ApiContext (and the paper components it drives) over `store`."""
    adapter = adapter or DryRunExchangeAdapter(DryRunConfig())
    approval = approval or InMemoryApprovalChannel()
    kill_switch = KillSwitch()
    monitor = KillSwitchMonitor(
        adapter, approval, kill_switch, sink=store, sleep_fn=monitor_sleep_fn
    )
    manager = PositionManager(
        adapter,
        approval,
        strategy_config=strategy_config,
        risk_config=risk_config,
        slider=slider,
        kill_switch=kill_switch,
        sink=store,
        defer_approval=defer_approval,
    )
    return ApiContext(
        adapter=adapter,
        manager=manager,
        kill_switch=kill_switch,
        monitor=monitor,
        store=store,
        risk_config=risk_config,
        risk_state=RiskState(slider=slider),
        bias_provider=bias_provider,
        clock=clock,
    )
