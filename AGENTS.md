# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

Crypto Trader is an approve-then-automate swing-trading bot for crypto perpetual futures on Bitunix, adapter-scaffolded for any venue.
See [README.md](README.md) for what has actually been built and how to run it.
This file carries the standing rules and decisions that apply beyond any one task.

## Governing principles (apply to everything built in this project)

1. Exchange is the single source of truth: analyze exactly what you execute against.
2. Pure decision core: `generate_signal()` (a future task) is a pure function of (candles, HTF candles, position state, config).
3. No-lookahead, ever: a still-forming candle must never be exposed to any consumer, at any timeframe, at any layer.
4. On-exchange stops are the safety floor (future risk-system task).
5. Ugly data is normal: dedup, gap detection, timestamp sanity on every ingested candle.

## Locked captain decisions (project-wide, apply from Build Order step 1 forward)

1. Trade-count gates: the live validation floor is 20 trades, an operations check only (every trade had a stop, zero unplanned exits), never statistical proof.
   The backtest validation gate (Gate 1, future task) targets roughly 150-300 signals with a confidence interval excluding zero.
2. Risk slider vs. kill switch: effective per-trade risk must be capped strictly below the daily kill-switch threshold, regardless of the user's risk-slider setting.
   Not implemented yet; a locked requirement for the future risk-system task.
3. v1 interface scope: Telegram-first plus a read-only terminal monitor.
   The five-tab web UI and Whisper voice are deferred past v1.
   Docker scaffolding in this project is for the bot process only; do not add a second web UI container until that future task starts.
4. Funding cost treatment: a funding-cost filter or penalty for with-bias entries is future strategy work, not implemented yet.
5. Strategy parameter authority: the builder of the future strategy task proposes concrete values for unspecified parameters (volume-profile bucketing, MA ensemble, entry/stop/TP derivation, trailing thresholds) and validates them against the Gate 1 backtest, rather than the captain hand-specifying them.

## Conduct rules (captain-locked, apply for the life of this build)

1. No em dashes anywhere produced for this project: code, comments, docs, commit messages, UI copy.
   Use a hyphen, comma, or colon instead.
   Machine-checked by `scripts/check_no_em_dash.py`, run as part of the normal lint step.
2. E2E-first bug reproduction: reproduce bugs through the real interface before fixing.
   Applies from the first task with a real interface to reproduce through.
3. Sentence-per-line in committed long-form markdown: README, docs, and report files get one full sentence per physical line, normal markdown structure otherwise preserved.
4. Quality over development cost: the standing tie-break when a fork appears between a quick patch and the proper abstraction.
5. Pixel perfection at end testing: applies once there is UI to test.
6. Zero tolerance on lint, test failures, and flakiness.
   `pytest -q` and the linter (`ruff check .`) must run clean before any task reports done.
   Tests must never touch the network or live APIs; use fixtures/mocks for anything exchange-related.
7. Correctness over persuasion: reproduce and verify any numeric or behavioral claim rather than asserting it.
8. UI encapsulates complexity: applies once there is UI.
9. Settings must not outweigh exploration tolerance: applies once there are user-facing settings.

Manual-review items not automated yet: commit-message em-dash and co-author-trailer checks (impractical to wire as a pre-commit hook in this task's time budget; review by eye until automated).

## Architecture decisions from Build Order step 1 (scaffold + data layer)

- **Single-writer SQLite invariant.** Only the bot process ever writes the database.
  Any future reader (CLI tools, a later UI) opens it read-only.
  Enforced by convention today (all writes route through `crypto_trader.ingest.pipeline.store_candles`), not by a file permission or lock; revisit if a second writer is ever proposed.
- **WAL mode and busy_timeout.** Every connection opened through `crypto_trader.db.connection.connect()` runs in WAL journal mode with a busy_timeout (default 5000ms), so a future read-only reader does not block the writer and brief contention waits instead of raising.
- **Backup mechanism.** `crypto_trader.db.backup.backup_database()` uses SQLite's online backup API (`sqlite3.Connection.backup`), safe to run against a live database.
  Never shell out to `cp` on the live `.sqlite3` file: WAL mode means the file alone is not a consistent snapshot.
  Nightly-cron wiring is out of scope; the next ops/cron task should call this function on a schedule.
- **Candle anchor: UTC 00:00, MUST-VERIFY.** The daily/4H boundary is pinned to UTC 00:00 in `crypto_trader.config`.
  This is provisional: it has not been verified against Bitunix's real candle-stamp convention.
  Confirm before live trading and update `crypto_trader.config`'s module docstring once verified.
- **Data plan: 4H and daily candles only, by design.** This is the original spec's plan, kept as an engineering default, not an oversight.
  It cannot faithfully replay intrabar order (stop vs. take-profit within one bar).
  Any future backtest engine must apply a conservative intrabar ordering rule (assume the worse of stop-vs-TP within a bar) rather than silently assume a favorable fill.
  Do not build a finer-grained data pipeline without a deliberate decision to revisit this default.
- **No-lookahead enforcement point.** Lives in `crypto_trader.ingest.validate.validate_candles`, at the candle-slicing / ingest layer, not deferred to a downstream "pure function."
  A still-forming candle (`close_time > now`) is rejected before it ever becomes a `Candle` and is never written to storage; the `is_closed` column on `candles_4h` / `candles_1d` exists as a second line of defense.
- **Data source.** `crypto_trader.ingest.source.CandleSource` is the swappable interface.
  `BitunixCandleSource` hits Bitunix's public, unauthenticated futures kline endpoint (`https://fapi.bitunix.com/api/v1/futures/market/kline`), reachable from the dev/CI environment as of this task; no credentials involved.
  `FixtureCandleSource` serves synthetic data for tests and offline development; tests use only this source.
- **Symbol universe.** `crypto_trader.config.DEFAULT_SYMBOLS` is a small seed list (BTCUSDT, ETHUSDT) for development.
  Building out the full universe is future work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
