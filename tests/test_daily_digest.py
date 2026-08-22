"""Daily digest tests (Build Order step 5, PRD 9.1/9.5): content, cadence, and honest degrading.

Uses the DryRun adapter (real, already-tested honest state) for the happy path and its
simulate_outage hook for the unreachable-exchange path, plus the in-memory approval channel
(the existing step-4 test pattern). No network, no bot token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crypto_trader.approval.channel import EventKind
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.safety.digest import (
    DailyDigestScheduler,
    build_daily_digest,
    format_digest,
    send_daily_digest,
)
from crypto_trader.safety.equity import EquityTracker
from crypto_trader.safety.kill_switch import KillSwitch, KillSwitchReason

_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _adapter() -> DryRunExchangeAdapter:
    return DryRunExchangeAdapter(DryRunConfig(starting_equity=10_000.0))


def test_build_daily_digest_reports_equity_pnl_and_drawdown() -> None:
    adapter = _adapter()
    equity = EquityTracker()
    equity.observe(10_000.0, _BASE)
    equity.observe(9_500.0, _BASE + timedelta(hours=4))  # today's loss, off the day-0 baseline
    kill_switch = KillSwitch()

    report = build_daily_digest(adapter, kill_switch, equity)

    assert report.equity == adapter.get_balance().total
    assert report.daily_pnl == report.equity - 10_000.0
    assert report.drawdown_fraction >= 0.0
    assert report.open_position_count == 0
    assert report.kill_switch_armed is True
    assert report.exchange_reachable is True


def test_format_digest_is_six_clear_lines() -> None:
    adapter = _adapter()
    equity = EquityTracker()
    equity.observe(10_000.0, _BASE)
    kill_switch = KillSwitch()

    text = format_digest(build_daily_digest(adapter, kill_switch, equity))
    lines = text.splitlines()

    assert len(lines) == 6  # one clear number per line (conduct rules 5, 8)
    assert lines[0].startswith("Equity: $")
    assert "P&L" in lines[1]
    assert "Drawdown" in lines[2]
    assert "Open positions" in lines[3]
    assert lines[4] == "Kill switch: ARMED"
    assert "API health" in lines[5]
    # No internal state dump: no raw dict/repr punctuation leaking into the text.
    assert "{" not in text and "TradePlan" not in text


def test_format_digest_shows_the_trigger_reason_when_not_armed() -> None:
    adapter = _adapter()
    equity = EquityTracker()
    equity.observe(10_000.0, _BASE)
    kill_switch = KillSwitch()
    kill_switch.trigger(KillSwitchReason.DAILY_LOSS, "daily loss 7.0%", now=_BASE)

    text = format_digest(build_daily_digest(adapter, kill_switch, equity))
    assert "TRIGGERED (daily_loss)" in text


def test_digest_degrades_honestly_when_the_exchange_is_unreachable() -> None:
    adapter = _adapter()
    adapter.simulate_outage(10)  # every call this cycle fails
    equity = EquityTracker()
    equity.observe(10_000.0, _BASE)
    kill_switch = KillSwitch()

    report = build_daily_digest(adapter, kill_switch, equity)
    text = format_digest(report)

    assert report.exchange_reachable is False
    assert "UNREACHABLE" in text
    # The heartbeat still goes out: the report is degraded, not empty or absent.
    assert report.equity == 10_000.0  # falls back to the tracker's last known peak


def test_send_daily_digest_notifies_through_the_approval_channel() -> None:
    adapter = _adapter()
    equity = EquityTracker()
    equity.observe(10_000.0, _BASE)
    kill_switch = KillSwitch()
    approval = InMemoryApprovalChannel()

    report = build_daily_digest(adapter, kill_switch, equity)
    send_daily_digest(approval, report)

    events = approval.events_of(EventKind.DAILY_DIGEST)
    assert len(events) == 1
    assert "Equity:" in events[0].message


# --------------------------------------------------------------------------- scheduler cadence


def test_scheduler_fires_once_per_utc_day() -> None:
    adapter = _adapter()
    kill_switch = KillSwitch()
    equity = EquityTracker()
    approval = InMemoryApprovalChannel()
    scheduler = DailyDigestScheduler()

    first = scheduler.maybe_send(adapter, kill_switch, equity, approval, now=_BASE)
    second = scheduler.maybe_send(
        adapter, kill_switch, equity, approval, now=_BASE + timedelta(hours=4)
    )

    assert first is not None
    assert second is None  # same UTC day: no second send
    assert len(approval.events_of(EventKind.DAILY_DIGEST)) == 1


def test_scheduler_fires_again_on_a_new_utc_day() -> None:
    adapter = _adapter()
    kill_switch = KillSwitch()
    equity = EquityTracker()
    approval = InMemoryApprovalChannel()
    scheduler = DailyDigestScheduler()

    scheduler.maybe_send(adapter, kill_switch, equity, approval, now=_BASE)
    next_day = scheduler.maybe_send(
        adapter, kill_switch, equity, approval, now=_BASE + timedelta(days=1)
    )

    assert next_day is not None
    assert len(approval.events_of(EventKind.DAILY_DIGEST)) == 2


# --------------------------------------------------------------------------- heartbeat framing


def test_digest_is_documented_as_the_heartbeat() -> None:
    # PRD 9.5: "silence means the bot died." This pins that the module says so, since detecting
    # a MISSED digest is deliberately out of this task's scope (future ops work, per the brief).
    import crypto_trader.safety.digest as digest_module

    assert "heartbeat" in digest_module.__doc__.lower()
    assert "future ops work" in digest_module.__doc__.lower()
