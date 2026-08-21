"""Command-line entry point for the Gate 1 backtest lab.

Run a sweep and write a markdown and JSON report:

    python -m crypto_trader.backtest --synthetic --out backtest_reports
    python -m crypto_trader.backtest --db data/crypto_trader.sqlite3 \
        --symbols BTCUSDT,ETHUSDT --out backtest_reports

The synthetic mode needs no network and no database, so the lab is runnable and reproducible
anywhere; its report states plainly that the data is synthetic and Gate 1 is therefore not
proven. The database mode reads real ingested candles read-only (single-writer invariant) and
is the mode a real Gate 1 run uses.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from crypto_trader.backtest.costs import CostConfig
from crypto_trader.backtest.data import connect_readonly, load_dataset
from crypto_trader.backtest.report import ReportContext, render_json, render_markdown
from crypto_trader.backtest.sweep import run_sweep
from crypto_trader.backtest.synthetic import (
    DEFAULT_SYMBOLS,
    generate_dataset,
    generate_funding,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m crypto_trader.backtest")
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--synthetic", action="store_true",
        help="run on deterministic synthetic data (default when no --db is given)",
    )
    src.add_argument("--db", type=str, default=None, help="path to a candle SQLite database")
    p.add_argument(
        "--symbols", type=str, default=None,
        help="comma-separated symbols; for --db this is required, for --synthetic it is "
             "optional and defaults to the built-in synthetic set",
    )
    p.add_argument("--years", type=float, default=2.5, help="synthetic history length in years")
    p.add_argument("--seed", type=int, default=12345, help="synthetic data seed")
    p.add_argument(
        "--lookbacks", type=str, default="45,60,75",
        help="comma-separated lookback-day values to sweep (within 30-90)",
    )
    p.add_argument(
        "--bootstrap", type=int, default=2000,
        help="bootstrap resamples for the confidence interval",
    )
    p.add_argument("--out", type=str, default="backtest_reports", help="output directory")
    p.add_argument("--name", type=str, default=None, help="report file base name")
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    lookbacks = tuple(int(x) for x in args.lookbacks.split(",") if x.strip())
    for lb in lookbacks:
        if not 30 <= lb <= 90:
            raise SystemExit(f"lookback {lb} is outside the spec's 30-90 day bound")

    cost_config = CostConfig()
    generated_at = datetime.now(timezone.utc)

    if args.db:
        symbols = [s for s in (args.symbols or "").split(",") if s.strip()]
        if not symbols:
            raise SystemExit("--db mode requires --symbols")
        conn = connect_readonly(args.db)
        try:
            dataset = load_dataset(conn, symbols)
        finally:
            conn.close()
        funding = None
        funding_description = "no funding schedule loaded (database mode); funding cost is 0"
        data_description = f"real ingested candles from {args.db}"
        is_synthetic = False
    else:
        symbols = (
            [s for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else DEFAULT_SYMBOLS
        )
        dataset = generate_dataset(symbols, years=args.years, seed=args.seed)
        funding = generate_funding(dataset)
        funding_description = (
            "synthetic placeholder derived from 24h momentum, 8h interval, "
            "positive in rising markets so longs pay (PRD 9.3 MUST-VERIFY)"
        )
        data_description = (
            f"deterministic synthetic data ({args.years} years, seed {args.seed})"
        )
        is_synthetic = True

    sweep = run_sweep(
        dataset,
        cost_config=cost_config,
        funding=funding,
        lookback_days=lookbacks,
        bootstrap_resamples=args.bootstrap,
    )
    ctx = ReportContext(
        data_description=data_description,
        is_synthetic=is_synthetic,
        symbols=symbols,
        cost_config=cost_config,
        funding_description=funding_description,
        generated_at=generated_at,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = args.name or ("synthetic_gate1" if is_synthetic else "gate1")
    md_path = out_dir / f"{base}.md"
    json_path = out_dir / f"{base}.json"
    md_path.write_text(render_markdown(sweep, ctx), encoding="utf-8")
    json_path.write_text(render_json(sweep, ctx), encoding="utf-8")

    chosen = sweep.chosen
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(
        f"Chosen: lookback={chosen.lookback_days}d filter="
        f"{'on' if chosen.funding_filter_enabled else 'off'}; "
        f"OOS n={chosen.oos_stats.n} mean_r={chosen.oos_stats.mean_r:.3f} "
        f"CI=[{chosen.oos_stats.ci_boot_low:.3f}, {chosen.oos_stats.ci_boot_high:.3f}]"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)
