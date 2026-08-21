# Crypto Trader

An approve-then-automate swing-trading bot for crypto perpetual futures on Bitunix.
The bot is adapter-scaffolded so any venue can be plugged in later.
It is built for ugly data, driven by risk management first and profitability second.
The human approves; the machine executes and manages.

Project conduct rules, locked captain decisions, and architecture decisions live in [AGENTS.md](AGENTS.md).
Read that file before starting any new task on this project.

## Status

This is Build Order step 1: project scaffolding, Docker, the SQLite schema, and candle ingest with ugly-data validation.
Later steps (the pure signal framework, backtest lab, paper loop, interfaces, live adapter) are separate, future tasks.
No strategy logic, exchange adapter, or credential handling exists yet.

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
  __main__.py            bot process entry point (one ingest pass, for now)
tests/
  fixtures/synthetic_candles.py   engineered candle data for each validation path
  test_validate.py                unit tests for the validator
  test_db_schema.py               schema, WAL, and backup tests
  test_ingest_pipeline.py         end to end ingest tests, fixture data only
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
