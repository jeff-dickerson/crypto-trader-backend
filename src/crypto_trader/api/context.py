"""ApiContext: the live in-process state the handlers read and drive.

The REST API is a control surface over the running bot, so it does not own trading state; it holds
references to the same objects the paper loop runs (the ExchangeAdapter, the PositionManager, the
KillSwitch and its KillSwitchMonitor) plus the ApiStore for persisted history and a small mutable
RiskState for the slider and overlay toggle. Hosting decision 5 makes the API and the bot one
process, so these are literally the same instances, not copies.

The context exposes only what handlers need and keeps the arithmetic in the components that own it:
risk numbers come from RiskConfig (paper.risk) via the manager, kill-switch state from
KillSwitchMonitor, equity/day-P&L from the shared EquityTracker. `bias_provider` is an injected
callable so the dashboard's bias line reflects real daily-bias-gate output where a candle feed is
available, without forcing candle storage into the API layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeVar

from crypto_trader.api.store import ApiStore
from crypto_trader.api.timeouts import DEFAULT_EXCHANGE_TIMEOUT_SECONDS, exchange_call
from crypto_trader.exchange.adapter import ExchangeAdapter
from crypto_trader.ingest.models import utc_now
from crypto_trader.paper.position_manager import PositionManager
from crypto_trader.paper.risk import RiskConfig
from crypto_trader.safety.equity import EquityTracker
from crypto_trader.safety.kill_switch import KillSwitch
from crypto_trader.safety.monitor import KillSwitchMonitor

T = TypeVar("T")


@dataclass
class RiskState:
    """The mutable, PATCH-able risk settings (the slider and the overlay toggle).

    Everything else the /risk endpoint reports is derived on every GET from RiskConfig and live
    equity, never stored here, so the slider can never route around the account-level guardrail
    (the kill-switch cap and tier boundaries live in RiskConfig, which is not PATCH-able).
    `cycle_start_equity` anchors the capital overlay's "current multiple" and only moves at a cycle
    boundary (PRD 5.2).
    """

    slider: float = 1.0
    overlay_enabled: bool = False
    cycle_start_equity: float | None = None


@dataclass
class ApiContext:
    """Everything the twelve handlers need, wired to the one running bot."""

    adapter: ExchangeAdapter
    manager: PositionManager
    kill_switch: KillSwitch
    monitor: KillSwitchMonitor
    store: ApiStore
    risk_config: RiskConfig
    risk_state: RiskState = field(default_factory=RiskState)
    bias_provider: Callable[[], dict[str, str]] | None = None
    clock: Callable[[], datetime] = utc_now
    exchange_timeout_s: float = DEFAULT_EXCHANGE_TIMEOUT_SECONDS

    @property
    def equity_tracker(self) -> EquityTracker:
        """The one shared equity tracker (owned by the monitor), for day-P&L and drawdown."""
        return self.monitor.equity

    def now(self) -> datetime:
        return self.clock()

    def exchange_read(self, fn: Callable[[], T]) -> T:
        """Run one outbound exchange call with the context's timeout (maps failures to 503)."""
        return exchange_call(fn, timeout_s=self.exchange_timeout_s)

    def current_bias(self) -> dict[str, str]:
        """Per-symbol daily bias for the dashboard, or an empty map when no feed is wired."""
        if self.bias_provider is None:
            return {}
        return self.bias_provider()
