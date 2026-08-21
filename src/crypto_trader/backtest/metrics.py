"""Performance statistics for a set of closed trades, all in risk multiples (R).

Everything here operates on the `net_r` of closed trades (fees, slippage, and funding
already deducted by the cost model). The Gate 1 bar (PRD 6.1, captain decision 1) is a
CONFIDENCE INTERVAL that excludes zero, not a raw point estimate above a threshold, so the
headline output is an interval, and both a bootstrap percentile interval and a
normal-approximation interval are reported so no single distributional assumption carries
the whole claim. Every statistic is deterministic: the bootstrap uses a fixed seed, so a
report reproduces exactly on re-run, which is what makes "reproduce, don't defend" (conduct
rule 7) practical.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from crypto_trader.backtest.engine import ClosedTrade, ExitType

# 95% two-sided normal critical value. Used for the normal-approximation interval; at Gate
# 1's target of 150-300 trades the Student-t value is within about 1% of this, and the
# bootstrap interval is reported alongside as the assumption-light primary.
_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class TradeStats:
    """Expectancy and risk statistics for one set of closed trades."""

    n: int
    mean_r: float
    sd_r: float
    se_r: float
    t_stat: float
    ci_normal_low: float
    ci_normal_high: float
    ci_boot_low: float
    ci_boot_high: float
    win_rate: float
    payoff: float | None
    avg_win_r: float | None
    avg_loss_r: float | None
    total_r: float
    max_drawdown_r: float
    max_drawdown_pct: float
    exit_counts: dict[str, int]

    @property
    def ci_excludes_zero(self) -> bool:
        """True when the bootstrap 95% interval lies strictly on one side of zero.

        This is the Gate 1 statistical test. A positive expectancy only counts as proven
        when the whole interval is above zero.
        """
        return (self.ci_boot_low > 0.0) or (self.ci_boot_high < 0.0)

    @property
    def positive_edge_proven(self) -> bool:
        """True only when the interval excludes zero AND the edge is positive."""
        return self.ci_boot_low > 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sample_sd(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _bootstrap_ci(
    xs: list[float], *, resamples: int, seed: int, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean. Deterministic given `seed`."""
    if len(xs) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(xs)
    means: list[float] = []
    for _ in range(resamples):
        s = 0.0
        for _ in range(n):
            s += xs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_idx = int((alpha / 2.0) * resamples)
    hi_idx = min(resamples - 1, int((1.0 - alpha / 2.0) * resamples))
    return (means[lo_idx], means[hi_idx])


def _drawdowns(net_rs: list[float], risk_fraction: float) -> tuple[float, float]:
    """Max drawdown of the equity curve, in R and as a compounding percentage.

    The R curve is the cumulative sum of net R; its max drawdown is the largest peak-to-
    trough drop in R. The percentage curve compounds a fixed per-trade risk fraction
    (PRD 4.1), each trade scaling equity by (1 + risk_fraction * net_r); its max drawdown
    is the largest fractional drop from a running peak. Trades must already be in
    chronological order.
    """
    peak_r = 0.0
    cum_r = 0.0
    max_dd_r = 0.0
    equity = 1.0
    peak_eq = 1.0
    max_dd_pct = 0.0
    for r in net_rs:
        cum_r += r
        peak_r = max(peak_r, cum_r)
        max_dd_r = max(max_dd_r, peak_r - cum_r)
        equity *= (1.0 + risk_fraction * r)
        peak_eq = max(peak_eq, equity)
        if peak_eq > 0:
            max_dd_pct = max(max_dd_pct, (peak_eq - equity) / peak_eq)
    return max_dd_r, max_dd_pct


def compute_stats(
    trades: list[ClosedTrade],
    *,
    risk_fraction: float = 0.02,
    bootstrap_resamples: int = 2000,
    seed: int = 0,
) -> TradeStats:
    """Full statistics for `trades`. Trades are sorted chronologically for the equity curve.

    `risk_fraction` converts the R-based drawdown into a percentage one (it does not affect
    expectancy, which is size-independent). The default matches CostConfig's >= $2,000 tier.
    """
    ordered = sorted(trades, key=lambda t: t.exit_time)
    rs = [t.net_r for t in ordered]
    n = len(rs)

    exit_counts: dict[str, int] = {e.value: 0 for e in ExitType}
    for t in ordered:
        exit_counts[t.exit_type.value] += 1

    if n == 0:
        return TradeStats(
            n=0, mean_r=0.0, sd_r=0.0, se_r=0.0, t_stat=0.0,
            ci_normal_low=0.0, ci_normal_high=0.0, ci_boot_low=0.0, ci_boot_high=0.0,
            win_rate=0.0, payoff=None, avg_win_r=None, avg_loss_r=None,
            total_r=0.0, max_drawdown_r=0.0, max_drawdown_pct=0.0, exit_counts=exit_counts,
        )

    mean_r = _mean(rs)
    sd_r = _sample_sd(rs)
    se_r = sd_r / math.sqrt(n) if n > 0 else 0.0
    t_stat = mean_r / se_r if se_r > 0 else 0.0
    ci_normal_low = mean_r - _Z_95 * se_r
    ci_normal_high = mean_r + _Z_95 * se_r
    ci_boot_low, ci_boot_high = _bootstrap_ci(rs, resamples=bootstrap_resamples, seed=seed)

    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    win_rate = len(wins) / n
    avg_win_r = _mean(wins) if wins else None
    avg_loss_r = _mean(losses) if losses else None
    payoff = (avg_win_r / avg_loss_r) if (avg_win_r and avg_loss_r) else None

    max_dd_r, max_dd_pct = _drawdowns(rs, risk_fraction)

    return TradeStats(
        n=n,
        mean_r=mean_r,
        sd_r=sd_r,
        se_r=se_r,
        t_stat=t_stat,
        ci_normal_low=ci_normal_low,
        ci_normal_high=ci_normal_high,
        ci_boot_low=ci_boot_low,
        ci_boot_high=ci_boot_high,
        win_rate=win_rate,
        payoff=payoff,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        total_r=sum(rs),
        max_drawdown_r=max_dd_r,
        max_drawdown_pct=max_dd_pct,
        exit_counts=exit_counts,
    )
