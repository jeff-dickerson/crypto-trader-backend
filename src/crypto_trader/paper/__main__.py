"""Runnable paper loop (Build Order step 4): python -m crypto_trader.paper.

This runs the approve-then-execute machine end to end and prints its Gate 2 audit. It is the
machine a future operational task will run live for the 4-week Gate 2 window (PRD 6.2/12); this
command does not run that window, it demonstrates and self-checks the machine.

Synthetic mode needs no network, no database, and no bot token, so it is the easy way to see the
whole loop run:

    python -m crypto_trader.paper --synthetic --years 1.5

Approval surface: if a Telegram bot token and an allowlisted chat id are configured in the
environment (crypto_trader.secrets), the loop uses the real Telegram approval channel; otherwise
it prints a clear message and falls back to the in-memory auto-approve channel for the synthetic
demo, so it never crashes for lack of a credential. Live Telegram approvals block on a human, so
that path is for an interactive session, not this non-interactive demo.

Database mode (reading real ingested candles read-only, the single-writer invariant) is a thin
wrapper the future operational task will drive; this entry point focuses on the synthetic demo,
which is what a reviewer can reproduce offline.
"""

from __future__ import annotations

import argparse
import logging

from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.approval.telegram import TelegramApprovalChannel
from crypto_trader.backtest.synthetic import generate_dataset
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.paper.loop import PaperRunResult, PaperTrader
from crypto_trader.paper.position_manager import PositionManager
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crypto_trader.paper")


def _build_approval(
    adapter: DryRunExchangeAdapter,
) -> InMemoryApprovalChannel | TelegramApprovalChannel:
    def status() -> str:
        positions = adapter.get_positions()
        if not positions:
            return "Status: flat"
        lines = [f"{p.symbol} {p.side.value} {p.quantity} @ {p.entry_price}" for p in positions]
        return "Status:\n" + "\n".join(lines)

    def profit() -> str:
        b = adapter.get_balance()
        return f"Equity: {b.total:.2f} {b.currency} (unrealized {b.unrealized_pnl:.2f})"

    telegram = TelegramApprovalChannel.from_env(status_provider=status, profit_provider=profit)
    if telegram is not None:
        logger.info("Telegram approval channel configured; using it.")
        return telegram
    logger.info("Using the in-memory auto-approve channel for this synthetic run.")
    return InMemoryApprovalChannel()


def _print_summary(result: PaperRunResult) -> None:
    print("\n=== Paper loop (Gate 2 machine) summary ===")
    for sr in result.per_symbol:
        print(
            f"{sr.symbol}: bars {sr.n_bars}, approvals {sr.n_approved}/{sr.n_approvals_requested}, "
            f"submitted {sr.n_submitted}, voided {sr.n_voided}, "
            f"drift-freezes {sr.n_drift_freezes}, closed {len(sr.closed_trades)}"
        )
    total = len(result.closed_trades)
    net_r_note = "(paper: proves the machine, not the edge)"
    print(f"closed trades: {total} {net_r_note}")
    if result.gate2_clean:
        print("Gate 2 machine audit: CLEAN (zero critical failures)")
    else:
        print("Gate 2 machine audit: CRITICAL FAILURES:")
        for failure in result.critical_failures:
            print(f"  - {failure}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Gate 2 paper loop.")
    parser.add_argument("--synthetic", action="store_true", help="use deterministic synthetic data")
    parser.add_argument("--years", type=float, default=1.5, help="years of synthetic data")
    parser.add_argument("--seed", type=int, default=12345, help="synthetic data seed")
    args = parser.parse_args()

    if not args.synthetic:
        parser.error(
            "only --synthetic mode is wired into this entry point; see the module docstring"
        )

    # Use symbols the DryRun adapter has trading rules for (its default BTCUSDT/ETHUSDT).
    dataset = generate_dataset(["BTCUSDT", "ETHUSDT"], years=args.years, seed=args.seed)
    adapter = DryRunExchangeAdapter(DryRunConfig())
    approval = _build_approval(adapter)
    manager = PositionManager(adapter, approval, strategy_config=DEFAULT_STRATEGY_CONFIG)
    trader = PaperTrader(adapter, manager, strategy_config=DEFAULT_STRATEGY_CONFIG)
    result = trader.run(dataset)
    _print_summary(result)


if __name__ == "__main__":
    main()
