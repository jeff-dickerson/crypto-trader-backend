"""Markdown and JSON reports for a Gate 1 sweep.

The report is written to be read honestly: it states its data source (and, for synthetic
data, that the edge is therefore NOT proven), pins the three denominators the PRD requires
(signals, fills, closed trades) with explicit definitions, labels every statistic in-sample
or out-of-sample, lists the cost and funding assumptions as the placeholders they are, and
ends on a plain Gate 1 verdict that never claims a pass the numbers do not earn (conduct
rule 7, PRD 6.2). Markdown prose is one sentence per physical line (conduct rule 3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.backtest.metrics import TradeStats
from crypto_trader.backtest.sweep import SweepPoint, SweepResult


@dataclass(frozen=True)
class ReportContext:
    """Everything the report needs about the run beyond the sweep numbers themselves."""

    data_description: str
    is_synthetic: bool
    symbols: list[str]
    cost_config: CostConfig
    funding_description: str
    generated_at: datetime
    target_signal_low: int = 150
    target_signal_high: int = 300


def _f(x: float | None, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{nd}f}"


def _pct(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.{nd}f}%"


def _verdict(chosen: SweepPoint, target_low: int, is_synthetic: bool) -> tuple[str, str]:
    """Return (headline, explanation sentences) for the Gate 1 verdict, honestly."""
    oos = chosen.oos_stats
    if is_synthetic:
        head = "NOT PROVEN (synthetic data)"
        why = (
            "The data for this run is synthetic, so this run validates only that the "
            "Gate 1 harness works end to end, not the strategy edge.\n"
            "Gate 1 remains not proven and requires a rerun on real ingested candles.\n"
        )
        return head, why
    proven = oos.positive_edge_proven and oos.max_drawdown_pct < 0.15
    if proven and oos.n >= target_low:
        head = "PASSED"
        why = (
            "The out-of-sample bootstrap confidence interval excludes zero on the positive "
            "side and the max drawdown stayed under the 15% bar, on a sample at or above the "
            "signal target.\n"
        )
    else:
        head = "NOT PASSED"
        reasons: list[str] = []
        if oos.n < target_low:
            reasons.append(
                f"the out-of-sample closed-trade count ({oos.n}) is below the "
                f"{target_low}-signal target"
            )
        if not oos.positive_edge_proven:
            reasons.append(
                "the out-of-sample confidence interval does not exclude zero on the "
                "positive side"
            )
        if oos.max_drawdown_pct >= 0.15:
            reasons.append(
                f"the out-of-sample max drawdown ({_pct(oos.max_drawdown_pct)}) breaches "
                "the 15% bar"
            )
        why = "Gate 1 is not passed because " + "; ".join(reasons) + ".\n"
    return head, why


def _combo_table(sweep: SweepResult) -> str:
    header = (
        "| Lookback (d) | Funding filter | Signals | Fills | Closed | "
        "IS mean R | OOS mean R | OOS 95% CI (boot) | OOS excl. 0 | OOS win% | "
        "OOS payoff | OOS maxDD% |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows: list[str] = []
    for idx, p in enumerate(sweep.points):
        oos = p.oos_stats
        chosen_mark = " *" if idx == sweep.chosen_index else ""
        ci = f"[{_f(oos.ci_boot_low)}, {_f(oos.ci_boot_high)}]"
        rows.append(
            f"| {p.lookback_days}{chosen_mark} | {'on' if p.funding_filter_enabled else 'off'} "
            f"| {p.n_signals} | {p.n_fills} | {p.n_closed} "
            f"| {_f(p.is_stats.mean_r)} | {_f(oos.mean_r)} | {ci} "
            f"| {'yes' if oos.ci_excludes_zero else 'no'} | {_pct(oos.win_rate)} "
            f"| {_f(oos.payoff, 2)} | {_pct(oos.max_drawdown_pct)} |"
        )
    return header + "\n".join(rows) + "\n"


def _stats_block(title: str, s: TradeStats) -> str:
    return (
        f"### {title}\n\n"
        f"- Closed trades (n): {s.n}\n"
        f"- Mean expectancy: {_f(s.mean_r)} R per closed trade\n"
        f"- Standard deviation: {_f(s.sd_r)} R; standard error: {_f(s.se_r)} R; "
        f"t-stat: {_f(s.t_stat, 2)}\n"
        f"- 95% CI (bootstrap, seeded): [{_f(s.ci_boot_low)}, {_f(s.ci_boot_high)}] R\n"
        f"- 95% CI (normal approx): [{_f(s.ci_normal_low)}, {_f(s.ci_normal_high)}] R\n"
        f"- Confidence interval excludes zero: {'yes' if s.ci_excludes_zero else 'no'}\n"
        f"- Win rate: {_pct(s.win_rate)}; payoff (avg win / avg loss): {_f(s.payoff, 2)}\n"
        f"- Total: {_f(s.total_r)} R; max drawdown: {_f(s.max_drawdown_r)} R "
        f"({_pct(s.max_drawdown_pct)} compounding)\n"
        f"- Exit mix: {s.exit_counts}\n"
    )


def render_markdown(sweep: SweepResult, ctx: ReportContext) -> str:
    chosen = sweep.chosen
    head, why = _verdict(chosen, ctx.target_signal_low, ctx.is_synthetic)
    cc = ctx.cost_config

    lines: list[str] = []
    lines.append("# Gate 1 Backtest Report\n")
    lines.append(f"Generated at {ctx.generated_at.isoformat()}.\n")
    lines.append(f"Data source: {ctx.data_description}.\n")
    if ctx.is_synthetic:
        lines.append(
            "This run uses SYNTHETIC data, so it proves only that the backtest lab runs "
            "end to end; it does not validate the trading edge.\n"
        )
    lines.append("")

    lines.append("## Gate 1 verdict\n")
    lines.append(f"**{head}**\n")
    lines.append(why)
    lines.append("")

    lines.append("## Denominators (pinned, per PRD 6.1)\n")
    lines.append(
        "A setup that fires an entry order, an order that actually fills, and a trade that "
        "closes are three different counts, so each is reported separately and every "
        "statistic states which one it uses.\n"
    )
    lines.append(
        "- Signals: setups that fired a resting entry order, after the funding and "
        "minimum-notional filters.\n"
        "- Fills: signals whose resting limit actually traded through (modelled as full "
        "fills; partial fills need order-book depth the 4H/daily data plan does not carry).\n"
        "- Closed trades: fills that reached a terminal exit; the expectancy denominator. "
        "Fills still open when the data ends are counted separately, never as closed "
        "trades.\n"
    )
    lines.append("")

    lines.append("## Run setup\n")
    lines.append(f"- Symbols: {', '.join(ctx.symbols)}.\n")
    lines.append(
        f"- Timeline: {sweep.data_start.isoformat()} to {sweep.data_end.isoformat()}.\n"
    )
    lines.append(
        f"- In-sample / out-of-sample split at {sweep.split_time.isoformat()} "
        "(first two-thirds tune, final third is the frozen score).\n"
    )
    lines.append(
        "- The chosen combination is selected on IN-SAMPLE mean R only; its OUT-OF-SAMPLE "
        "figures are the honest gate number, and selecting on the out-of-sample score would "
        "be cheating.\n"
    )
    lines.append(
        f"- Costs (placeholders, MUST-VERIFY per PRD 9.3): maker {_pct(cc.maker_fee_rate, 3)}, "
        f"taker {_pct(cc.taker_fee_rate, 3)}, slippage {_pct(cc.slippage_rate, 3)} on market "
        f"exits, minimum notional {cc.min_notional}, sizing at {_pct(cc.risk_fraction)} risk "
        f"on {cc.assumed_equity} assumed equity.\n"
    )
    lines.append(f"- Funding: {ctx.funding_description}.\n")
    lines.append("")

    lines.append("## Swept-parameter comparison\n")
    lines.append(
        "The lookback is swept inside the spec's 30-90 day bound and the "
        "captain-decision-4 funding filter is run both off and on; the chosen combination "
        "is marked with an asterisk.\n"
    )
    lines.append("")
    lines.append(_combo_table(sweep))

    lines.append("## Chosen combination detail\n")
    lines.append(
        f"Lookback {chosen.lookback_days} days, funding filter "
        f"{'on' if chosen.funding_filter_enabled else 'off'}, selected by in-sample mean R.\n"
    )
    lines.append(
        f"Whole-run funnel: {chosen.n_setups} setups, {chosen.n_funding_filtered} "
        f"funding-filtered, {chosen.n_min_notional_voided} minimum-notional-voided, "
        f"{chosen.n_signals} signals, {chosen.n_fills} fills, {chosen.n_closed} closed, "
        f"{chosen.n_open_at_end} still open at data end.\n"
    )
    lines.append("")
    lines.append(_stats_block("In-sample (tuning window, not the gate)", chosen.is_stats))
    lines.append("")
    lines.append(_stats_block("Out-of-sample (frozen score, the gate)", chosen.oos_stats))
    lines.append("")

    return "\n".join(lines)


def _stats_dict(s: TradeStats) -> dict:
    return {
        "n": s.n,
        "mean_r": s.mean_r,
        "sd_r": s.sd_r,
        "se_r": s.se_r,
        "t_stat": s.t_stat,
        "ci_bootstrap": [s.ci_boot_low, s.ci_boot_high],
        "ci_normal": [s.ci_normal_low, s.ci_normal_high],
        "ci_excludes_zero": s.ci_excludes_zero,
        "positive_edge_proven": s.positive_edge_proven,
        "win_rate": s.win_rate,
        "payoff": s.payoff,
        "avg_win_r": s.avg_win_r,
        "avg_loss_r": s.avg_loss_r,
        "total_r": s.total_r,
        "max_drawdown_r": s.max_drawdown_r,
        "max_drawdown_pct": s.max_drawdown_pct,
        "exit_counts": s.exit_counts,
    }


def render_json(sweep: SweepResult, ctx: ReportContext) -> str:
    head, _why = _verdict(sweep.chosen, ctx.target_signal_low, ctx.is_synthetic)
    payload = {
        "generated_at": ctx.generated_at.isoformat(),
        "data_description": ctx.data_description,
        "is_synthetic": ctx.is_synthetic,
        "symbols": ctx.symbols,
        "timeline": {
            "start": sweep.data_start.isoformat(),
            "end": sweep.data_end.isoformat(),
            "split": sweep.split_time.isoformat(),
        },
        "gate1_verdict": head,
        "cost_config": {
            "maker_fee_rate": ctx.cost_config.maker_fee_rate,
            "taker_fee_rate": ctx.cost_config.taker_fee_rate,
            "slippage_rate": ctx.cost_config.slippage_rate,
            "min_notional": ctx.cost_config.min_notional,
            "assumed_equity": ctx.cost_config.assumed_equity,
            "risk_fraction": ctx.cost_config.risk_fraction,
        },
        "funding_description": ctx.funding_description,
        "chosen_index": sweep.chosen_index,
        "combinations": [
            {
                "lookback_days": p.lookback_days,
                "funding_filter_enabled": p.funding_filter_enabled,
                "counts": {
                    "setups": p.n_setups,
                    "funding_filtered": p.n_funding_filtered,
                    "min_notional_voided": p.n_min_notional_voided,
                    "signals": p.n_signals,
                    "fills": p.n_fills,
                    "closed": p.n_closed,
                    "open_at_end": p.n_open_at_end,
                },
                "in_sample": _stats_dict(p.is_stats),
                "out_of_sample": _stats_dict(p.oos_stats),
            }
            for p in sweep.points
        ],
    }
    return json.dumps(payload, indent=2)
