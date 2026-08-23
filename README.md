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

Build Order steps 1 through 4 are complete as machinery, with the live/paper validation windows still owed: project scaffolding, Docker, the SQLite schema, and candle ingest with ugly-data validation (step 1); the pure strategy framework (step 2); the backtest lab (step 3); and the paper loop (step 4).
Step 4 adds the approve-then-execute paper machine (Gate 2): a DryRun exchange adapter with honest fills in `src/crypto_trader/exchange/dryrun.py`, risk sizing with the tiered schedule and the kill-switch clamp plus the position-manager lifecycle and reconciliation in `src/crypto_trader/paper/`, and the approval seam (an in-memory fake and a real Telegram surface) in `src/crypto_trader/approval/`.
It builds and self-tests the machine only; the 4-week paper-trading window is a future operational task, so Gate 2 itself is not yet satisfied (see [PRD.md](PRD.md) section 6.2).
Step 2 adds the deterministic decision core in `src/crypto_trader/strategy/`: a volume profile, a daily bias gate, the separation-then-return zone/setup logic, and the pure `generate_signal()` function, with unit tests that engineer synthetic candles to trigger each path.
Step 3 adds the Gate 1 backtest lab in `src/crypto_trader/backtest/`: a no-lookahead replay engine, a fees/funding/slippage/minimum-notional cost model, the captain-decision-4 funding filter, a parameter sweep with a frozen out-of-sample score, and markdown/JSON reports.
A real Gate 1 run has since happened against ingested Bitunix candles across 14 symbols, and Gate 1 did NOT pass: too few out-of-sample signals against the 150-300 target and a negative out-of-sample mean expectancy with a confidence interval that does not exclude zero (see [PRD.md](PRD.md) section 6.2 and `backtest_reports/gate1_real_2026-08-21.md` for the full honest result).
A read-only Bitunix characterization spike (no orders placed, no credentials, public endpoints only) produced the `ExchangeAdapter` interface in `src/crypto_trader/exchange/`: the abstract contract for paper and live adapters, with its shared order/position/fill data models, designed against Bitunix's verified public API surface (see [PRD.md](PRD.md) section 9.3 and [AGENTS.md](AGENTS.md)).
Step 4 adds the first working implementation of that interface: the DryRun paper adapter, alongside the live Bitunix adapter still to come in step 6.
Step 4 also added one method to that interface, `place_market_order` (MARKET is a confirmed-native Bitunix order type), needed for the momentum-shift exit and reused by step 5's kill switch.
Step 5 adds the backend of "Interfaces": the kill switch and the daily digest in `src/crypto_trader/safety/` (see [PRD.md](PRD.md) section 9.1/9.5), plus the in-process REST API in `src/crypto_trader/api/`.
The REST API is a standard-library WSGI application over the live paper machine and the unified SQLite store: twelve `/api/v1` routes (health, dashboard, approvals plus an async decision endpoint, the signal log, the journal, a terminal bundle, the mutable/derived risk split, and the kill-switch state machine), five new SQLite tables with real writers wired into the paper loop, one error envelope, and cursor pagination on the append-heavy logs (see the "Running the REST API" section below and [AGENTS.md](AGENTS.md)).
The read-only TUI and the web UI are covered by the separate `crypto-trader-web` app, out of this repo's scope; the REST API is the contract that app will build against.
The live Bitunix adapter (step 6) is still a future task; no live exchange credential handling exists yet.
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
    dryrun.py              the DryRun (paper) adapter: honest fills against candle data
  approval/
    channel.py            the ApprovalChannel ABC, decision and event vocabulary
    memory.py              in-memory approval channel (the fake the loop is tested against)
    telegram.py            the real Telegram approval surface (fails safe with no token)
  paper/
    risk.py               tiered risk sizing, the slider, and the kill-switch clamp
    plan.py                the TradePlan a human approves, with its single risk number
    position_manager.py    signal consumption, approval, and the four-state lifecycle
    reconciliation.py      drift check with a documented tolerance, freeze-on-drift
    loop.py                the PaperTrader orchestrator and the Gate 2 critical-failure audit
    __main__.py            paper-loop entry point (python -m crypto_trader.paper --synthetic)
  safety/
    equity.py              EquityTracker: the one shared daily-anchor/peak-equity source
    kill_switch.py          KillSwitch: the armed/triggered state machine
    monitor.py               KillSwitchMonitor: auto-triggers, degraded-mode retry, and the derived state
    digest.py                 the once-a-day digest/heartbeat through ApprovalChannel
  api/
    app.py                 the WSGI router and make_wsgi_app (no third-party framework)
    handlers.py             the twelve route handlers
    context.py               ApiContext: the live in-process state the handlers read and drive
    store.py                  ApiStore: the SQLite reader/writer and the paper loop's PersistenceSink
    errors.py                 the error envelope and machine-readable codes
    pagination.py             cursor pagination for /signals and /journal
    timeouts.py               the outbound-call timeout wrapper (maps failures to 503)
    serialization.py          store rows and live objects to response JSON with as-of timestamps
    build.py                   wire an ApiContext to a paper machine
    server.py                   the wsgiref runnable server (bind host = tailnet interface)
    __main__.py                  API entry point (python -m crypto_trader.api --populate)
  secrets.py             env-loaded secrets (Telegram token), redacted, never in the config plane
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
  test_exchange_adapter.py         the ExchangeAdapter ABC contract and SymbolRule maths
  test_dryrun_adapter.py           the DryRun honest-fills rules (subsequent-candle, slippage, worst-of-intrabar)
  test_paper_risk.py               tiered sizing, the kill-switch clamp, and plan-time min-notional voiding
  test_paper_reconciliation.py     the reconciliation tolerance and each drift condition
  test_approval_channels.py        the in-memory channel, the mocked Telegram surface, and secrets hygiene
  test_paper_loop.py               end-to-end approve-then-execute runs and the Gate 2 audit
  test_kill_switch.py              each auto-trigger, the degraded-mode fallback, and rearm
  test_daily_digest.py             digest content, cadence, and honest degrading when unreachable
  api_helpers.py                   the in-process WSGI test client and paper-machine builders
  test_api_store.py                the five API tables and every PersistenceSink writer/reader
  test_api_read_endpoints.py       read endpoints, error envelope, pagination, and the 503 mapping
  test_api_decision.py             the approval decision endpoint and plan state machine (incl. FAILED)
  test_api_kill_switch.py          arm/rearm, idempotency, and an injected flatten failure
  test_api_risk.py                 the risk PATCH clamp and derived recalculation
  test_api_server.py               the wsgiref server binding and serving over a loopback socket
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
A follow-up investigation of the exit-side parameters against that same real data is committed at `backtest_reports/retune_2026-08-22.md`: it found the momentum-shift exit protective rather than premature and no exit-parameter change that moves the out-of-sample result, so the exit parameters were kept unchanged.
The database file itself is never committed; candle data is re-fetchable and gitignored per the data plan.

## Running the paper loop (Gate 2)

The paper loop is the approve-then-execute machine Gate 2 proves: honest fills, the approval flow, and reconciliation.
Synthetic mode needs no network, no database, and no Telegram bot token, so it runs the whole machine and prints its critical-failure audit.

```bash
python -m crypto_trader.paper --synthetic --years 1.5
```

If a Telegram bot token and an allowlisted chat id are set in the environment (`CRYPTO_TRADER_TELEGRAM_BOT_TOKEN` and `CRYPTO_TRADER_TELEGRAM_ALLOWED_CHAT_ID`), the loop uses the real Telegram approval channel; otherwise it prints a clear message and falls back to the in-memory auto-approve channel, so it never crashes for lack of a credential.
Like the Gate 1 lab, a synthetic run validates only that the machine works, never the edge: it builds and self-tests the machine, and the 4-week paper-trading window is a future operational task (see [PRD.md](PRD.md) section 6.2).
The kill switch and the daily digest (`src/crypto_trader/safety/`) are wired into this run: the summary prints the kill switch's final state, and a digest fires once per UTC day through the same approval channel.

## Running the REST API

The backend REST API (`src/crypto_trader/api/`) is the in-process seam every read-only and control surface polls: twelve `/api/v1` routes over the live paper machine and the unified SQLite store.
It is a standard-library WSGI application (no framework), polling-only for v1, and meant to run in the same process as the bot; bind the host to the tailnet interface for Tailscale-only exposure.

```bash
python -m crypto_trader.api --populate --years 1.5
curl http://127.0.0.1:8787/api/v1/health
```

`--populate` first runs a short synthetic paper loop (auto-approved) so the read endpoints have real signals and closed trades to show; without it the API serves an empty in-memory store.
`--host <tailnet-ip>` binds a specific interface (the auth boundary is the tailnet; there is no token in v1), and `--db <path>` points at a persistent SQLite file instead of the in-memory demo.
The routes cover health, dashboard, approvals plus an async `POST /approvals/{id}/decision`, the signal log, the journal, a terminal bundle, the mutable/derived `/risk` split (`GET` and `PATCH`), and the kill-switch state machine (`GET` plus `POST /kill-switch/arm` and `/rearm`); see [AGENTS.md](AGENTS.md) for the full architecture.

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

The trading universe is rule-defined and self-updating (`crypto_trader.ingest.universe`): a 24h quote-volume floor plus an order-book depth check run against Bitunix public market data, yielding the live top-10-to-15 set and, for backtest research, the full liquid survivor set.
A research-only deep-history source (`crypto_trader.ingest.binance_source.BinanceSpotCandleSource`, Binance spot via the public data-vision mirror) exists solely to widen the Gate 1 backtest sample; it is RESEARCH/BACKTEST ONLY, never a live or paper data source, and a test (`tests/test_research_source_isolation.py`) proves it cannot be reached from any live/paper code path.
A universe-expansion Gate 1 re-run on this enlarged, multi-venue, multi-regime sample is committed at `backtest_reports/universe_expansion_2026-08-23.md` (and `.json`): Gate 1 still does not pass, and the negative out-of-sample edge hardens into statistical significance at scale (see [PRD.md](PRD.md) section 6.2).
