"""Build Order step 5's ops-and-safety layer: the kill switch and the daily digest.

This package is deliberately independent of the REST API (not yet planned) and reads the paper
loop's own state through the ExchangeAdapter and ApprovalChannel seams already built in steps 4
and the characterization spike, so it needs no interface change to either beyond the additive
ExchangeAdapter.consecutive_api_failures method (see AGENTS.md, "Architecture decisions from
Build Order step 5").

- equity.py     EquityTracker: the one shared daily-anchor/peak-equity tracker (PRD 9.1/9.5).
- kill_switch.py  KillSwitch: the armed/triggered state machine, checked before any new entry.
- monitor.py       KillSwitchMonitor: evaluates the auto-triggers each cycle and drives the
                     degraded-mode flatten-retry fallback.
- digest.py        The once-a-day digest/heartbeat, delivered through ApprovalChannel.
"""

from __future__ import annotations

from crypto_trader.safety.digest import (
    DailyDigestReport,
    DailyDigestScheduler,
    build_daily_digest,
    format_digest,
    send_daily_digest,
)
from crypto_trader.safety.equity import EquityTracker
from crypto_trader.safety.kill_switch import KillSwitch, KillSwitchEvent, KillSwitchReason
from crypto_trader.safety.monitor import (
    DEFAULT_KILL_SWITCH_CONFIG,
    KillSwitchConfig,
    KillSwitchMonitor,
    websocket_dead_trigger,
)

__all__ = [
    "DailyDigestReport",
    "DailyDigestScheduler",
    "build_daily_digest",
    "format_digest",
    "send_daily_digest",
    "EquityTracker",
    "KillSwitch",
    "KillSwitchEvent",
    "KillSwitchReason",
    "DEFAULT_KILL_SWITCH_CONFIG",
    "KillSwitchConfig",
    "KillSwitchMonitor",
    "websocket_dead_trigger",
]
