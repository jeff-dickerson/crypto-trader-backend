"""EquityTracker: the one place the daily-anchor and peak-equity numbers live.

The kill switch's daily-loss and max-drawdown auto-triggers (PRD 9.1) and the daily digest's
"today's P&L" and "drawdown" lines (PRD 9.1/9.5) both need the same two derived numbers: today's
starting equity, and the highest equity ever observed. Tracking them in two places would risk the
two disagreeing, the same anti-pattern the DryRun adapter's shared CostConfig avoids for slippage
(AGENTS.md), so this is the one tracker both consult; KillSwitchMonitor owns an instance and the
paper loop passes the same instance to the digest builder.

Day boundary matches the project's candle anchor (UTC 00:00, MUST-VERIFY per config.py): a new
UTC calendar date starts a new "today" for both the daily-loss trigger and the digest's daily P&L
line. Callers pass timestamps that are already UTC (as every Candle timestamp in this project is).

Known limitation, matching an already-accepted one elsewhere in this project: the paper loop
replays symbols SEQUENTIALLY, one symbol's whole history before the next (AGENTS.md, "The loop
and its Gate 2 audit"), so `observe()` calls made against a multi-symbol paper run do not see
calendar dates in true wall-clock order; day boundaries reset mid-run as the replay moves from one
symbol's date range back to the start of the next symbol's. True interleaving is a step-6, live-
adapter concern, exactly like the existing "sequential replay" note; this tracker is correct for
a single continuous timeline (live operation, or one symbol at a time), which is what it is built
and tested against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class EquityTracker:
    """Tracks today's starting equity and the all-time peak, updated once per decision cycle."""

    day_start_equity: float | None = None
    peak_equity: float | None = None
    _day: date | None = None

    def observe(self, equity: float, at: datetime) -> None:
        """Record one cycle's equity. Resets the daily anchor on a new UTC calendar date."""
        day = at.date()
        if self._day != day:
            self._day = day
            self.day_start_equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
