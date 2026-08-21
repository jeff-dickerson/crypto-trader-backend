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

Build Order step 1 (project scaffolding, Docker, the SQLite schema, and candle ingest with ugly-data validation) and step 2 (the pure strategy framework) are complete.
Step 2 adds the deterministic decision core in `src/crypto_trader/strategy/`: a volume profile, a daily bias gate, the separation-then-return zone/setup logic, and the pure `generate_signal()` function, with unit tests that engineer synthetic candles to trigger each path.
A read-only Bitunix characterization spike (no orders placed, no credentials, public endpoints only) has also run, producing the `ExchangeAdapter` interface in `src/crypto_trader/exchange/`: the abstract contract the future paper and live adapters will implement, with its shared order/position/fill data models, designed against Bitunix's verified public API surface (see [PRD.md](PRD.md) section 9.3 and [AGENTS.md](AGENTS.md)).
Later steps (backtest lab, paper loop, interfaces, live adapter) are separate, future tasks.
The interface is defined, but no working exchange adapter, backtest replay engine, or credential handling exists yet.
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
