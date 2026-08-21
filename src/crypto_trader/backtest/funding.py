"""Funding-rate schedule and the with-bias funding-cost filter (captain decision 4).

Two separate concerns live here, both driven by the perpetual-futures funding
mechanism:

1. The COST MODEL half: a funding rate applies to an open position at each funding
   settlement it is held across. On a perpetual, a POSITIVE funding rate means longs
   pay shorts and a NEGATIVE rate means shorts pay longs. `FundingSchedule` enumerates
   the settlement instants inside a holding period and totals the funding actually paid
   or earned, so `crypto_trader.backtest.costs` can charge it per trade.

2. The FILTER half (captain decision 4, PRD 3.4): a with-bias entry is skipped when the
   funding rate at the entry bar is running strongly AGAINST the intended position, so
   the strategy does not knowingly open into a funding headwind that can equal or exceed
   the whole edge. `funding_is_prohibitive` is the pure gate; the engine applies it to the
   entry signals `generate_signal` emits. generate_signal itself stays pure and
   funding-unaware (its signature is frozen), so the gate lives at the caller seam.

Funding-schedule reality (interval and real historical rates) is a MUST-VERIFY item for
the Bitunix adapter spike between Build Order steps 3 and 4 (PRD 9.3). Until that lands in
AGENTS.md, this module uses the original spec's assumed funding interval (8 hours, settling
at UTC 00:00 / 08:00 / 16:00) and whatever rate series the caller supplies, which for the
Gate 1 demo is a clearly-labelled synthetic placeholder (see
crypto_trader.backtest.synthetic). No network or live API is touched here.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from crypto_trader.strategy.config import StrategyConfig
from crypto_trader.strategy.position import PositionSide

# Assumed funding interval, in hours, per the original spec. PROVISIONAL: confirm against
# Bitunix's real funding schedule in the adapter spike (PRD 9.3) before live trading.
DEFAULT_FUNDING_INTERVAL_HOURS = 8.0

# Funding settlements are anchored to UTC 00:00, matching the candle anchor
# (crypto_trader.config.CANDLE_ANCHOR_UTC_HOUR). Also PROVISIONAL, same MUST-VERIFY note.
_ANCHOR = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FundingSchedule:
    """A per-symbol funding-rate series with settlement enumeration and cost totalling.

    `rates_by_symbol` maps a symbol to a time-ordered list of (settlement_time, rate)
    points. The rate in effect at any instant is the most recent point at or before it
    (a step function), so a sparse series is fine. `interval_hours` is the spacing
    between settlements. All rates are per-interval fractions (0.0001 = 0.01% per
    interval), signed by the perpetual convention (positive = longs pay shorts).
    """

    rates_by_symbol: dict[str, list[tuple[datetime, float]]]
    interval_hours: float = DEFAULT_FUNDING_INTERVAL_HOURS

    def rate_at(self, symbol: str, when: datetime) -> float:
        """The funding rate in effect for `symbol` at `when` (step function, 0 if none)."""
        series = self.rates_by_symbol.get(symbol)
        if not series:
            return 0.0
        times = [t for t, _ in series]
        idx = bisect_right(times, when) - 1
        if idx < 0:
            return 0.0
        return series[idx][1]

    def settlement_times(self, start: datetime, end: datetime) -> list[datetime]:
        """Funding settlement instants t with start < t <= end.

        A position pays or earns funding at each settlement it is held THROUGH. The
        entry instant itself is excluded (funding is not charged for opening on a
        boundary) and the exit instant is included, which is the conservative choice for
        a cost: a position closed exactly at a settlement still owes that settlement.
        """
        if end <= start:
            return []
        interval = timedelta(hours=self.interval_hours)
        # First settlement strictly after `start`.
        elapsed = (start - _ANCHOR) / interval
        first_index = int(elapsed) + 1
        first = _ANCHOR + first_index * interval
        out: list[datetime] = []
        t = first
        while t <= end:
            out.append(t)
            t = t + interval
        return out

    def funding_cost_fraction(
        self, symbol: str, side: PositionSide, start: datetime, end: datetime
    ) -> float:
        """Total funding as a signed fraction of notional over the holding period.

        Positive means the position PAID funding (a cost); negative means it EARNED
        funding (a rebate). A long pays a positive rate and earns a negative one; a short
        is the mirror. Summed across every settlement in (start, end].
        """
        sign = 1.0 if side is PositionSide.LONG else -1.0
        total = 0.0
        for t in self.settlement_times(start, end):
            total += sign * self.rate_at(symbol, t)
        return total


def signed_adverse_rate(rate: float, side: PositionSide) -> float:
    """The funding rate re-expressed as ADVERSE-to-the-position (positive = a headwind).

    A long is hurt by a positive rate (it pays), a short by a negative rate. So the
    adverse magnitude is +rate for a long and -rate for a short. A negative result means
    funding is actually a tailwind (the position would earn).
    """
    return rate if side is PositionSide.LONG else -rate


def funding_is_prohibitive(
    rate: float, side: PositionSide, config: StrategyConfig
) -> bool:
    """The captain-decision-4 gate: True when a with-bias entry should be SKIPPED.

    Returns False (do not block) when the filter is disabled, so the default behaviour is
    unchanged and the Gate 1 sweep can measure the filter's effect by toggling
    `funding_filter_enabled`. When enabled, blocks the entry only when funding runs
    adverse to the intended position by more than `funding_filter_max_adverse_rate`.
    Favourable or mild funding never blocks.
    """
    if not config.funding_filter_enabled:
        return False
    return signed_adverse_rate(rate, side) > config.funding_filter_max_adverse_rate


def constant_schedule(
    symbols: list[str],
    rate: float,
    start: datetime,
    interval_hours: float = DEFAULT_FUNDING_INTERVAL_HOURS,
) -> FundingSchedule:
    """A flat funding schedule: one constant rate for every symbol from `start` on.

    Convenience for tests and for a calm-funding baseline. The single anchor point at
    `start` makes `rate_at` return `rate` for any later instant.
    """
    return FundingSchedule(
        rates_by_symbol={s: [(start, rate)] for s in symbols},
        interval_hours=interval_hours,
    )
