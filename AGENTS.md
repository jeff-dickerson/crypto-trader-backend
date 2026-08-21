# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

Crypto Trader is an approve-then-automate swing-trading bot for crypto perpetual futures on Bitunix, adapter-scaffolded for any venue.
See [README.md](README.md) for what has actually been built and how to run it, and [PRD.md](PRD.md) for the full product requirements (mission, strategy parameters, risk system, validation gates, architecture, interface scope, ops and safety, data and storage, build order, and the decisions log).
This file carries the standing rules and decisions that apply beyond any one task; where this file and PRD.md overlap, treat this file as the more current source for anything already built, since it is updated per task.

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

## Architecture decisions from Build Order step 2 (pure strategy framework)

Step 2 is the pure, deterministic decision core in `src/crypto_trader/strategy/`.
It builds no backtest replay engine, no paper/live adapter, and no interface: those are later tasks.
`generate_signal(candles, htf_candles, position_state, config)` is a real pure function: no `datetime.now`, no I/O, no global state.
`candles` is the primary 4H timeframe; `htf_candles` is the daily higher timeframe used only for the bias gate; the current decision point is always the last 4H candle.

- **Proposed parameter defaults, PENDING Gate 1 (captain decision 5).**
  Every unspecified strategy parameter is pinned to a reasoned, concrete default and lives as a named, overridable field on `crypto_trader.strategy.config.StrategyConfig`, never as a magic number in logic.
  None of these are settled truth: only the future Gate 1 out-of-sample backtest can validate them.
  The exhaustive per-field rationale is in `StrategyConfig`'s field comments; the headline choices are:
  volume profile by a FIXED BIN COUNT (24) across the lookback price span, not a fixed price increment, so it self-scales across symbols;
  70% value area (conventional volume-profile default);
  HVN/LVN cut relative to the MEAN bin volume (>= 1.3x is an HVN, <= 0.5x is an LVN);
  a SINGLE pinned lookback of 60 days (midpoint of the allowed 30-90 range);
  separation defined as price holding >= 1.5% beyond the boundary for >= 3 consecutive 4H candles;
  entry at the value-area EDGE (VAL for a long, VAH for a short), never the POC centre line;
  stop in the adjacent LVN just beyond the entry zone (fixed-offset fallback at a data edge);
  take profit just before the next opposing HVN (R-multiple fallback);
  a 3-SMA 4H ensemble (10/20/50) with a majority vote (>= 2) for the momentum-shift exit;
  trailing stop arming at +1R and trailing 1R behind the best price.
- **A swept lookback would be a tuned parameter.**
  The 60-day lookback is deliberately one fixed value, not swept or tuned in this task.
  Sweeping it is tuning, and only the future Gate 1 out-of-sample number can validate a swept value honestly, so do not sweep it before Gate 1 exists.
- **Position-state contract (four states, not a flat/in-position boolean).**
  `crypto_trader.strategy.position.PositionState` distinguishes FLAT, PENDING (entry limit resting, no fill), PARTIAL (partially filled, real exposure), and OPEN (fully established), because a resting limit at a zone boundary can partially fill.
  Management rules (trailing stop, momentum-shift exit) run for PARTIAL and OPEN; PENDING holds and awaits a fill; FLAT seeks an entry.
  The overtrading governor's memory (`traded_zones`) persists across a closed trade back to FLAT, so a later retest of an already-traded zone is rejected: one zone, one trade.
  `generate_signal` never mutates a `PositionState` (it is frozen); the future position manager threads an updated state into the next call.
  Order placement and fill tracking are future paper-loop work; this task only defines and consumes the contract.
- **Result type.** `generate_signal` returns a frozen `crypto_trader.strategy.signal.Signal` with an explicit `SignalAction` (no signal, enter long/short with entry/stop/take-profit attached, update trailing stop, momentum-shift exit, hold), extensible with defaulted fields without breaking callers.
- **No-lookahead responsibility split (the key contract with step 3).**
  Lookahead is a property of how the input window is constructed, not of the pure function consuming it.
  The volume profile and bias are computed from EXACTLY the candles handed in, however many are present, and no decision ever reads a candle after the last one; separation-then-return is decided using only candles at or before the decision point.
  The future backtest replay engine (step 3) OWNS feeding this function a correctly causally-sliced, growing window; this core's only job is never to break that guarantee on its own.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
