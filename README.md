# Crypto Trader

An approve-then-automate swing-trading bot for crypto perpetual futures on Bitunix.
The bot is adapter-scaffolded so any venue can be plugged in later.
It is built for ugly data, driven by risk management first and profitability second.
The human approves; the machine executes and manages.
The v1 strategy is a volume-profile POC zone retest, gated by a daily bias filter, with a tiered risk schedule and a defined validation path from backtest through paper to live trading.

The full requirements, including the concrete strategy parameters, the risk system, the validation gates, and the interface and ops requirements, live in [PRD.md](PRD.md).
Project conduct rules, locked captain decisions, and architecture decisions live in [AGENTS.md](AGENTS.md).
Read both before starting any new task on this project.

## Status

Build Order steps 1 through 3 are complete: project scaffolding, Docker, the SQLite schema, and candle ingest with ugly-data validation (step 1); the pure strategy framework (step 2); and the backtest lab (step 3).
Step 2 adds the deterministic decision core in `src/crypto_trader/strategy/`: a volume profile, a daily bias gate, the separation-then-return zone/setup logic, and the pure `generate_signal()` function, with unit tests that engineer synthetic candles to trigger each path.
Step 3 adds the Gate 1 backtest lab in `src/crypto_trader/backtest/`: a no-lookahead replay engine, a fees/funding/slippage/minimum-notional cost model, the captain-decision-4 funding filter, a parameter sweep with a frozen out-of-sample score, and markdown/JSON reports.
A real Gate 1 run has since happened against ingested Bitunix candles across 14 symbols, and Gate 1 did NOT pass: too few out-of-sample signals against the 150-300 target and a negative out-of-sample mean expectancy with a confidence interval that does not exclude zero (see [PRD.md](PRD.md) section 6.2 and `backtest_reports/gate1_real_2026-08-21.md` for the full honest result).
A read-only Bitunix characterization spike (no orders placed, no credentials, public endpoints only) has also run, producing the `ExchangeAdapter` interface in `src/crypto_trader/exchange/`: the abstract contract the future paper and live adapters will implement, with its shared order/position/fill data models, designed against Bitunix's verified public API surface (see [PRD.md](PRD.md) section 9.3 and [AGENTS.md](AGENTS.md)).
Later steps (paper loop, interfaces, live adapter) are separate, future tasks.
The `ExchangeAdapter` interface is defined, but no working exchange adapter or credential handling exists yet.
See [PRD.md](PRD.md) section 12 for the full build order with accurate current status on every step.

## Requirements

[PRD.md](PRD.md) is the authoritative requirements document: mission, the v1 strategy with its concrete parameter defaults, the risk system, success definition and validation gates, architecture, interface scope, ops and safety (including still-open risks), data and storage, the stack, build order, and a decisions log.
This README stays a practical entry point; it does not duplicate that detail.

## Project layout

```
src/crypto_trader/
  config.py            project constants: timeframes, candle anchor, symbol universe
  db/
    connection.py       SQLite connect(), WAL setup, migration runner
    backup.py            online backup via the SQLite backup API
    migrations/           numbered .sql migration files
  ingest/
    models.py            RawCandle (untrusted) and Candle (validated) data models
    source.py             swappable CandleSource interface, Bitunix HTTP and fixture implementations
    validate.py            ugly-data validation: dedup, gap detection, timestamp sanity, still-forming exclusion
    pipeline.py             fetch, validate, store orchestration
  strategy/
    config.py             every pinned, overridable strategy parameter (StrategyConfig)
    indicators.py          shared pure indicator helpers (SMA)
    position.py             the four-state position contract generate_signal consumes
    volume_profile.py        POC, VAH, VAL, and HVN/LVN structure from 4H candles
    bias.py                   long/short/neutral daily bias gate
    zones.py                   separation-then-return setup, first-touch, stop/TP geometry
    signal.py                   the pure generate_signal() core and its Signal result type
  exchange/
    adapter.py            the abstract ExchangeAdapter interface (paper and live implement it)
    types.py               shared order/position/fill data models and the capability matrix
  backtest/
    engine.py             no-lookahead replay engine (causal_windows) and fill/management sim
    costs.py               fees, slippage, funding, minimum-notional cost model (R-normalized)
    funding.py              funding schedule/cost and the captain-decision-4 with-bias filter
    metrics.py              expectancy, bootstrap and normal confidence intervals, drawdown
    sweep.py                 lookback x funding-filter sweep with a frozen out-of-sample score
    report.py                markdown and JSON report rendering
    data.py                   read-only candle loading from the SQLite database
    synthetic.py               deterministic synthetic market data for the runnable demo
    cli.py                      command-line entry point (python -m crypto_trader.backtest)
  __main__.py            bot process entry point (one ingest pass, for now)
tests/
  fixtures/synthetic_candles.py    engineered candle data for each validation path
  fixtures/strategy_candles.py     engineered candles for each strategy path
  test_validate.py                 unit tests for the validator
  test_db_schema.py                schema, WAL, and backup tests
  test_ingest_pipeline.py          end to end ingest tests, fixture data only
  test_volume_profile.py           volume profile unit tests
  test_bias.py                     daily bias gate unit tests
  test_zones.py                    setup detection and entry-geometry unit tests
  test_generate_signal.py          generate_signal path and purity tests
  test_backtest_no_lookahead.py    the no-lookahead regression tests (the safety-critical set)
  test_backtest_costs.py           cost-model unit tests
  test_backtest_funding.py         funding schedule and filter tests
  test_backtest_metrics.py         expectancy and confidence-interval tests
  test_backtest_engine.py          fill/management state machine and conservative intrabar rule
  test_backtest_sweep.py           sweep split, selection, and filter-effect tests
  test_backtest_report.py          report rendering and honest-verdict tests
  test_backtest_data.py            read-only candle-loading round-trip tests
```

## Running the tests

Install the project with its dev dependencies, then run pytest and the linter.

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
python scripts/check_no_em_dash.py
```

No test touches the network or a live exchange API.
The Bitunix HTTP source is exercised only through the manual `python -m crypto_trader` entry point, never from the test suite.
If the project is not installed as a package, prefix any command below with `PYTHONPATH=src` so `crypto_trader` is importable.

## Running the backtest lab (Gate 1)

The backtest lab runs a parameter sweep and writes a markdown and JSON report.
It has two data modes.

Synthetic mode needs no network and no database and is fully reproducible, so it is the easiest way to see the lab run end to end.

```bash
python -m crypto_trader.backtest --synthetic --out backtest_reports
```

Database mode reads real ingested candles read-only (the single-writer invariant) and is the mode a real Gate 1 run uses.

```bash
python -m crypto_trader.backtest --db data/crypto_trader.sqlite3 --symbols BTCUSDT,ETHUSDT --out backtest_reports
```

Useful flags: `--years` and `--seed` size the synthetic data, `--lookbacks 45,60,75` sets the swept lookback-day values (within the 30 to 90 day bound), `--bootstrap` sets the confidence-interval resample count, and `--name` sets the report file base name.
Reports land in `backtest_reports/`, which is gitignored as run artifacts; one example synthetic report is committed at `backtest_reports/EXAMPLE_synthetic_gate1.md` for reference.
The synthetic run reports Gate 1 as not proven by design: synthetic data validates only that the harness works, never the trading edge.
A real database-mode run has since happened, ingesting 14 symbols from Bitunix's public kline endpoint, and its report is committed at `backtest_reports/gate1_real_2026-08-21.md` (and `.json`) for reference: Gate 1 did not pass on that real data, see [PRD.md](PRD.md) section 6.2 for the honest numbers.
The database file itself is never committed; candle data is re-fetchable and gitignored per the data plan.

## Running with Docker

```bash
docker compose build
docker compose up
```

The bot container mounts a named volume at `/data` and writes its SQLite database there.
Only the bot process ever writes to that database; see AGENTS.md for the single-writer invariant.
A second, read-only UI service can be added to `docker-compose.yml` later without restructuring it.

## Data source

Candle ingest talks to Bitunix's public, unauthenticated futures kline endpoint when the network is reachable.
No exchange credentials are used or stored anywhere in this project.
When the network is not reachable, or for tests and local development, ingest runs against `FixtureCandleSource`, a swappable in-memory data source fed by synthetic candle fixtures.
