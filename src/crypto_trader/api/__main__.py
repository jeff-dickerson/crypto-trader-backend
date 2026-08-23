"""Runnable API server: python -m crypto_trader.api.

Serves the twelve routes over a paper machine, bound to a configurable host (bind the tailnet
interface for Tailscale-only exposure, decision 1). With --populate it first runs a short synthetic
paper loop (auto-approved) so the read endpoints have real signals and closed trades to show; this
is a demonstration of the hosting shape, not a deployment (decision 5).

    python -m crypto_trader.api --populate --years 1.5
    curl http://127.0.0.1:8787/api/v1/health
"""

from __future__ import annotations

import argparse
import logging

from crypto_trader.api.build import build_context
from crypto_trader.api.server import DEFAULT_HOST, DEFAULT_PORT, serve
from crypto_trader.api.store import ApiStore
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.backtest.synthetic import generate_dataset
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.paper.loop import PaperTrader
from crypto_trader.safety.digest import DailyDigestScheduler
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crypto_trader.api")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the crypto-trader REST API.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (the tailnet interface)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--db", default=":memory:", help="SQLite path (default in-memory demo)")
    parser.add_argument(
        "--populate", action="store_true", help="pre-run a synthetic paper loop for demo data"
    )
    parser.add_argument("--years", type=float, default=1.5, help="years of synthetic demo data")
    args = parser.parse_args()

    store = ApiStore.open(args.db)
    adapter = DryRunExchangeAdapter(DryRunConfig())
    approval = InMemoryApprovalChannel()
    # Synchronous auto-approve so the demo populates a real history; the interactive async approval
    # contract (defer_approval=True) is what POST /approvals/{id}/decision drives in a deployment.
    ctx = build_context(store, adapter=adapter, approval=approval, defer_approval=not args.populate)

    if args.populate:
        dataset = generate_dataset(["BTCUSDT", "ETHUSDT"], years=args.years)
        trader = PaperTrader(
            adapter,
            ctx.manager,
            strategy_config=DEFAULT_STRATEGY_CONFIG,
            kill_switch_monitor=ctx.monitor,
            digest_scheduler=DailyDigestScheduler(),
        )
        result = trader.run(dataset)
        logger.info(
            "populated demo store: %d closed trades, gate2_clean=%s",
            len(result.closed_trades),
            result.gate2_clean,
        )

    logger.info("serving REST API on http://%s:%d/api/v1", args.host, args.port)
    serve(ctx, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
