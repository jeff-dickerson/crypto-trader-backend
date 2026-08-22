"""The daily digest: a once-a-day summary through ApprovalChannel, and the heartbeat (PRD 9.5).

PRD's own framing: "silence means the bot died." The digest doubles as the heartbeat, so a
person watching Telegram can tell the bot is alive without reading any other signal. This module
only builds and sends the digest; DETECTING a missed one (the bot died and so cannot send its own
"I'm alive" message) needs an external watcher independent of this process, since a dead process
cannot alert about its own silence. That external watch is future ops work, not built here; see
AGENTS.md.

One clear number per line (conduct rules 5 and 8, UI encapsulates complexity): `format_digest`
produces exactly six lines, no internal state dump, matching the approval card's "risk_line"
philosophy (paper/plan.py) of showing the human one settled number per concept rather than the
arithmetic behind it.

`build_daily_digest` shares its equity numbers with the kill switch via the same EquityTracker
instance (equity.py), so the two surfaces never disagree about today's starting equity or the
running peak. It degrades honestly rather than skipping the send when the exchange cannot be
reached: `exchange_reachable=False` and the API-health line say so plainly, which is itself
useful heartbeat information (the digest still arrived, so the process is alive, even though the
venue is not answering).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from crypto_trader.approval.channel import ApprovalChannel, ChannelEvent, EventKind
from crypto_trader.exchange.adapter import ExchangeAdapter, ExchangeConnectionError
from crypto_trader.exchange.types import Position
from crypto_trader.safety.equity import EquityTracker
from crypto_trader.safety.kill_switch import KillSwitch


@dataclass(frozen=True)
class DailyDigestReport:
    """The settled numbers behind one digest. `format_digest` renders this; nothing else should."""

    equity: float
    daily_pnl: float
    daily_pnl_fraction: float
    drawdown_fraction: float
    open_position_count: int
    kill_switch_armed: bool
    kill_switch_reason: str | None
    rate_limit_remaining: int
    rate_limit_limit: int
    consecutive_api_failures: int
    exchange_reachable: bool


def build_daily_digest(
    adapter: ExchangeAdapter, kill_switch: KillSwitch, equity: EquityTracker
) -> DailyDigestReport:
    """Read exchange truth and settle the six digest numbers. Never raises: an unreachable
    exchange degrades the report rather than skipping the send, so the heartbeat still goes out."""
    reachable = True
    balance = None
    positions: list[Position] = []
    rate_limit = None
    try:
        balance = adapter.get_balance()
    except ExchangeConnectionError:
        reachable = False
    try:
        positions = adapter.get_positions()
    except ExchangeConnectionError:
        reachable = False
    try:
        rate_limit = adapter.rate_limit_status()
    except ExchangeConnectionError:
        reachable = False
    failures = adapter.consecutive_api_failures()

    fallback = equity.peak_equity if equity.peak_equity is not None else 0.0
    equity_total = balance.total if balance is not None else fallback
    day_start = equity.day_start_equity if equity.day_start_equity is not None else equity_total
    peak = equity.peak_equity if equity.peak_equity is not None else equity_total
    daily_pnl = equity_total - day_start
    daily_pnl_fraction = daily_pnl / day_start if day_start else 0.0
    drawdown_fraction = (peak - equity_total) / peak if peak else 0.0

    return DailyDigestReport(
        equity=equity_total,
        daily_pnl=daily_pnl,
        daily_pnl_fraction=daily_pnl_fraction,
        drawdown_fraction=drawdown_fraction,
        open_position_count=len(positions),
        kill_switch_armed=kill_switch.is_armed,
        kill_switch_reason=(
            kill_switch.last_event.reason.value if kill_switch.last_event is not None else None
        ),
        rate_limit_remaining=(rate_limit.remaining if rate_limit is not None else 0),
        rate_limit_limit=(rate_limit.limit_per_window if rate_limit is not None else 0),
        consecutive_api_failures=failures,
        exchange_reachable=reachable,
    )


def format_digest(report: DailyDigestReport) -> str:
    """Exactly six lines, one clear number each. No tier tables, no raw state dumps."""
    pnl_sign = "+" if report.daily_pnl >= 0 else ""
    kill_line = (
        "ARMED"
        if report.kill_switch_armed
        else f"TRIGGERED ({report.kill_switch_reason or 'unknown reason'})"
    )
    api_line = (
        f"{'reachable' if report.exchange_reachable else 'UNREACHABLE'}, "
        f"rate limit {report.rate_limit_remaining}/{report.rate_limit_limit}, "
        f"{report.consecutive_api_failures} consecutive failure(s)"
    )
    pnl_pct = report.daily_pnl_fraction * 100
    lines = [
        f"Equity: ${report.equity:,.2f}",
        f"Today's P&L: {pnl_sign}${report.daily_pnl:,.2f} ({pnl_sign}{pnl_pct:.1f}%)",
        f"Drawdown: {report.drawdown_fraction * 100:.1f}% from peak",
        f"Open positions: {report.open_position_count}",
        f"Kill switch: {kill_line}",
        f"API health: {api_line}",
    ]
    return "\n".join(lines)


def send_daily_digest(approval: ApprovalChannel, report: DailyDigestReport) -> None:
    approval.notify(
        ChannelEvent(
            kind=EventKind.DAILY_DIGEST,
            message=format_digest(report),
            detail={
                "equity": report.equity,
                "daily_pnl": report.daily_pnl,
                "drawdown_fraction": report.drawdown_fraction,
                "kill_switch_armed": report.kill_switch_armed,
                "exchange_reachable": report.exchange_reachable,
            },
        )
    )


@dataclass
class DailyDigestScheduler:
    """Fires build_daily_digest + send_daily_digest once per UTC calendar day.

    See the module docstring: this is the digest/heartbeat SENDER, not a missed-digest detector.
    """

    _last_sent_day: date | None = None

    def maybe_send(
        self,
        adapter: ExchangeAdapter,
        kill_switch: KillSwitch,
        equity: EquityTracker,
        approval: ApprovalChannel,
        *,
        now: datetime,
    ) -> DailyDigestReport | None:
        day = now.date()
        if day == self._last_sent_day:
            return None
        report = build_daily_digest(adapter, kill_switch, equity)
        send_daily_digest(approval, report)
        self._last_sent_day = day
        return report
