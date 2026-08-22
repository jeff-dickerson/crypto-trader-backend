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

## Architecture decisions from the Bitunix characterization spike (ExchangeAdapter interface)

This spike was read-only: no orders placed, no credentials used, only Bitunix's public documentation and public unauthenticated REST endpoints.
It exists to design the `ExchangeAdapter` seam against verified reality (PRD section 9.3), between Build Order steps 3 and 4.
The deliverable is the interface only: `src/crypto_trader/exchange/adapter.py` (the abstract `ExchangeAdapter`) plus `src/crypto_trader/exchange/types.py` (its shared data models).
No working Bitunix adapter is built here; that is step 6.

- **ABC, not Protocol, for the exchange seam.**
  The candle-source seam is a `Protocol` (single-method, duck-typed, fixture-swapped).
  `ExchangeAdapter` is an `abc.ABC` instead because it is money-adjacent, multi-method, and implemented by exactly two named classes (paper/DryRun in step 4, live Bitunix in step 6) that the position manager and reconciler will isinstance-check.
  An ABC fails loudly at instantiation if a method is missing, rather than silently at first call; for an order-placing surface that is the safer failure mode.
  The rationale is in `adapter.py`'s module docstring and is covered by `tests/test_exchange_adapter.py`.

- **CONFIRMED against the public API (cite the endpoint or doc page).**
  Order types (`.../api-docs/futures/trade/place_order.html`, `.../tp_sl/place_tp_sl_order.html`): base types are LIMIT and MARKET only, with time-in-force `effect` in {GTC, IOC, FOK, POST_ONLY}.
  Native server-side stops EXIST: a stop is a MARKET order gated on a trigger price via the TP/SL surface (`slPrice` with `slOrderType=MARKET`, `slStopType` MARK_PRICE or LAST_PRICE), so "on-exchange stops are the floor" (principle 4) is literal, not emulated.
  `reduceOnly` is a native order flag.
  A paired take-profit + stop-loss BRACKET on a position is placed in one native call (either leg firing closes the position): this is the specific "OCO" the strategy needs, and it is native.
  Min notional and precision (`.../api/v1/futures/market/trading_pairs`, public): per-symbol `minTradeVolume` (base units), `basePrecision` (qty dp), `quotePrecision` (price dp), leverage bounds, and per-symbol funding-rate caps.
  Funding (`.../api/v1/futures/market/funding_rate`): `fundingInterval` is 8 (hours), confirming the spec assumption; per-symbol.
  Historical funding (`.../api/v1/futures/market/get_funding_rate_history`, public): available, returns `fundingRate`/`fundingTime`/`markPrice` at 8h spacing, so the Gate 1 cost model (step 3) and the funding filter (decision 4) can source real funding from Bitunix itself.
  Rate limits: REST market data 10 req/sec/IP, private trade/TP-SL endpoints 10 req/sec/UID, WebSocket max 5 inbound messages/sec (exceed then disconnect, repeat then IP block); no weight-accounting scheme and no quota headers in responses.
  Candle depth (empirical, via `BitunixCandleSource` / the public kline endpoint): the kline endpoint caps at 200 rows per request (`limit` default 100, max 200) but `startTime`/`endTime` paginate backward; BTCUSDT and ETHUSDT reach ~2022-04-17 (about 4.35 years) at BOTH 4H and daily, so Gate 1's 2-3 years is achievable from Bitunix alone for established majors.
  History is per-symbol and bounded by listing date: a recently-listed alt (for example WIFUSDT) goes back only to its listing (about 2.6 years), so the spec's "history supplement from a major venue" fallback (PRD section 10) is only needed for symbols younger than the Gate 1 window, not for the BTC/ETH core.
  Kline rows return newest-first (descending time); this is an ingest-layer detail owned by step 1, noted here only so a future reader is not surprised.

- **COULD NOT confirm without credentials, so the interface reports rather than assumes.**
  Position mode is CONFIRMED account-global on Bitunix (param `positionMode` in {ONE_WAY, HEDGE}, unchangeable while any position or order is open), but the account's ACTUAL current mode needs an authenticated read.
  The interface therefore never hardcodes it: `ExchangeCapabilities.position_mode` and `Position.position_mode` carry the queried value, and the docstrings flag that the four-state `PositionState` contract assumes one-way netting while HEDGE mode can hold a simultaneous long and short.
  Margin mode is CONFIRMED per-symbol (param `marginMode` in {ISOLATION, CROSS}); ISOLATION is the principle-4 floor and the live adapter (step 6) must set it explicitly per traded symbol, since BTCUSDT/ETHUSDT show a non-isolated default in public trading-pairs data.
  A native TRAILING stop was NOT found in the documented REST endpoints; `ExchangeCapabilities.native_trailing_stop` is False and trailing is emulated bot-side by re-placing the stop tighter (the strategy core already computes the trail).
  Arbitrary OCO between two unrelated orders is not offered (`native_arbitrary_oco` is False); only the position-scoped TP/SL bracket above is native.
  WebSocket reconnect/resync semantics are thin in the public docs (public `wss://fapi.bitunix.com/public/`, private `.../private/`, a ping/pong heartbeat with no stated interval); a public stream is not needed for this task or this contract, so no streaming method is in the interface and step 6 owns any stream it adds.

- **How open questions are encoded in code.**
  `ExchangeCapabilities` is the machine-readable confirmed-versus-emulated matrix, so the position manager and reconciler branch on facts, not prose.
  `SymbolRule.min_notional(price)` derives the USD floor as `minTradeVolume * price` because Bitunix has no standalone minimum-notional field; PRD 4.2's plan-time affordability check depends on this.
  `RateLimitStatus` gives the "rate-limit status always visible" surface; because Bitunix returns no quota headers, an adapter tracks its own budget and reports it here.

## Architecture decisions from Build Order step 3 (backtest lab, Gate 1)

Step 3 is the Gate 1 backtest lab in `src/crypto_trader/backtest/`.
It consumes the unmodified step-2 `generate_signal` core and the step-1 candle storage without changing their public contracts; the only strategy-layer change is two new documented `StrategyConfig` fields for the funding filter.
Run it with `python -m crypto_trader.backtest` (see README.md).

- **No-lookahead slicing is owned here, in one function.**
  `crypto_trader.backtest.engine.causal_windows` is the enforcement point step 2 deferred to this step.
  For every simulated decision bar it returns the 4H tail ending exactly at that bar and the daily tail closed at or before it, and nothing later, so the profile and bias are recomputed per bar from a rolling causal window, never from one profile precomputed over the whole dataset.
  The regression tests in `tests/test_backtest_no_lookahead.py` plant a distinctive candle just after a decision point and assert it changes neither the window, the profile, the bias, nor the signal, and also assert it WOULD change a result that wrongly included it, so the tests have real detecting power.
  Any change to the engine must keep those tests passing.
- **The engine calls the unmodified `generate_signal` per bar as the single decision authority.**
  It does not reimplement or fork the decision, so backtest and live cannot diverge on the decision itself.
  A faster incremental-bin volume profile was measured (about 92% of rolling slides leave the window's price extremes unchanged, so add/remove updates would be roughly 8x faster) but deliberately NOT injected: the frozen `generate_signal` signature makes it the single authority, and a second profile implementation risks floating-point-drift decision divergence, which conduct rule 7 forbids trading for speed.
  Two provably-outcome-preserving accelerations ARE applied: an O(log n) bisect for the causal daily slice, trimmed to the last `bias_slow_period` daily candles (the bias gate reads no more than that), and a neutral-bias skip that avoids the profile build on flat bars where `generate_signal` returns NO_SIGNAL before building it.
  Measured cost is about 230us per bar; a full 18-symbol, 2.5-year, 6-combination sweep runs in roughly 3 minutes.
- **Conservative intrabar ordering, because 4H+daily cannot resolve within-bar path.**
  On any management bar where both the stop and the take-profit are in range, the STOP is assumed to fill first (the worse outcome), per the committed rule in the "Data plan" entry above.
  A resting entry limit fills only on a bar AFTER the signal bar (never the signal bar itself), which is the conservative no-lookahead choice and is what makes signals, fills, and closed trades three genuinely different counts.
  Fills are modelled as FULL on trade-through; faithful partial fills need order-book depth the data plan does not carry, so the PARTIAL position state stays a paper/live concern and is not fabricated in the backtest.
- **Cost model is R-normalized (`crypto_trader.backtest.costs`).**
  Every closed trade is scored in risk multiples (R = |entry - initial_stop|), where position size cancels, so expectancy is size-independent; minimum notional is the one size-dependent check and is applied at plan time.
  Fees follow order role (maker on the limit entry and a limit take-profit, taker plus adverse slippage on stop and market exits); funding is charged across the actual holding period at the spec's assumed 8h interval.
  Every fee, slippage, and funding RATE is a documented placeholder pending the Bitunix adapter spike (PRD 9.3), and every report says so.
- **Funding filter (captain decision 4) lives at the caller seam, not inside `generate_signal`.**
  `generate_signal` stays pure and funding-unaware (frozen signature), so the filter is a gate the engine applies to the entry signals it emits: `crypto_trader.backtest.funding.funding_is_prohibitive`.
  Threshold `StrategyConfig.funding_filter_max_adverse_rate = 0.0005` (0.05% per 8h): a with-bias entry is skipped when funding runs adverse to the position beyond this, because over a typical multi-day hold that is on the order of 0.2R, comparable to the whole reproduced ~0.18R edge (PRD 3.4).
  It is OFF by default (`funding_filter_enabled = False`), PENDING Gate 1 validation, and the sweep runs it both off and on so the report can show whether it actually helps out of sample.
- **Swept parameters and the frozen out-of-sample score.**
  The sweep varies the lookback across 45/60/75 days (a small range around the pinned 60, inside the spec's 30-90 bound) and the funding filter off/on.
  Tuning uses the first two-thirds of the timeline and the score is frozen on the final third; the chosen combination is selected by IN-SAMPLE mean R only, and every reported number is labelled in-sample or out-of-sample.
  Reports (`crypto_trader.backtest.report`) go to a gitignored `backtest_reports/`; one example synthetic report is committed for reference.
- **Gate 1 has now run on real data and did NOT pass (project-level finding, PRD 6.2).**
  The synthetic run (`crypto_trader.backtest.synthetic`) only ever validates the harness, never the edge; no report may claim a pass on synthetic data (the verdict logic hard-codes "NOT PROVEN" there).
  A real run happened against 14 symbols ingested from Bitunix (`backtest_reports/gate1_real_2026-08-21.md` and `.json`): the chosen combination produced 33 out-of-sample closed trades against the 150-300 target, with a negative out-of-sample mean expectancy (-0.163R) and a 95% CI that does not exclude zero.
  Two real findings the synthetic run could not surface: the current 14-symbol universe over the Bitunix-reachable history is too thin to reach the target signal count in this backtest window at all, and the real point-estimate sign is negative, opposite what the design-review's synthetic reproduction assumed.
  Neither the universe size nor the pinned parameters were changed to chase a pass (captain decision 5: a parameter change is a captain decision, not a unilateral edit); see PRD 6.2 for the full numbers and candidate next steps.

- **Real-data ingest learnings the synthetic generator could not surface.**
  Bitunix's real kline endpoint returns candle `time` (and every OHLCV field) as a JSON string, not a JSON number; `crypto_trader.ingest.validate._validate_shape` used a strict `isinstance(int)` check on `open_time_ms` that rejected every real candle as malformed until fixed (now coerces a numeric string via `_coerce_open_time_ms`, mirroring the existing OHLCV string coercion).
  Real kline rows arrive newest-first (descending `time`), which the validator's out-of-order check flags on almost every row: this is expected per the source's documented behavior (see the characterization-spike entry above), not a bug, but it makes the per-symbol issue counts look alarming at a glance; a future reader should expect a large `out_of_order` count on every real ingest and not treat it as a red flag on its own.
  The public kline endpoint's `limit` cap (200 rows) is paginated backward with the `endTime` query param (confirmed working live); `BitunixCandleSource.fetch_candles` and `ingest_symbol` now take an optional `end_time_ms` for this, and a pagination loop must track the raw fetched page size to detect "reached the start of listing", not the post-validation accepted count, since a malformed or duplicate row can shrink accepted below a full page.
  Rate limits held with simple pacing: about 5 req/sec (well under the published 10 req/sec/IP), no 429s encountered ingesting 14 symbols x 2 timeframes x up to 33 pages each.
  BTCUSDT, ETHUSDT, and most established majors (SOL, XRP, DOGE, BNB, LINK, SUI, ADA, BCH) reached the full 3-year target window; younger listings (HYPE, BZ, ENA, 1000PEPE) stopped at their listing date, all noted per-symbol in the real report rather than silently padded.

## Architecture decisions from Build Order step 4 (paper loop, Gate 2)

Step 4 is the approve-then-execute paper machine (PRD 6.2, "proves the machine, not the edge").
It consumes the unmodified step-2 `generate_signal` core, the step-1 candle storage, and the characterization-spike `ExchangeAdapter` interface, and adds the DryRun adapter, risk sizing, the approval seam, the position-manager lifecycle, and reconciliation.
Run the machine end to end on synthetic data with `python -m crypto_trader.paper --synthetic` (see README.md).
This task builds the machine only; it does NOT run the 4-week Gate 2 paper window (that is a future operational task), so Gate 2 itself is not satisfied by this task.

- **ExchangeAdapter interface change: `place_market_order` was ADDED (flagged, not silent).**
  The spike's interface exposed resting limits and native stops but no way to close a position at market, which the strategy's momentum-shift exit requires and step 5's kill-switch flatten will reuse.
  MARKET is a CONFIRMED native Bitunix order type, so exposing it assumes nothing unconfirmed; the change is purely additive (a new abstract method plus a new `MarketOrderRequest` type, reduce-only by intent), and the existing `test_exchange_adapter.py` stub was updated to implement it.
  This is the one interface signature change in this step; every other adapter signature is unchanged.

- **DryRun adapter honest fills, faithful to the confirmed venue (`crypto_trader.exchange.dryrun`).**
  The paper venue is driven one CLOSED candle at a time via `on_candle`, which returns the fills/exits it produced so the caller need not diff state.
  A resting limit fills only when a STRICTLY LATER candle trades through it (an order carries the clock value at placement), which is the "never filled on the placement candle" guarantee; a stop ALWAYS slips (fills at the trigger moved adversely by the shared cost model's `slippage_rate`, never at the stop price); the worst-of-intrabar rule holds (a stop wins a same-candle tie with the take-profit, and only a same-bar stop breach can close the fill candle).
  It honours `ExchangeCapabilities`: native stops rest server-side (the bracket's stop is a tracked resting order the instant the entry fills, principle 4), and trailing is emulated by cancel-and-replace because `native_trailing_stop` is False, so the paper branches match what the live adapter (step 6) will do.
  Slippage and fees are the SAME numbers as the Gate 1 backtest: `DryRunConfig` holds a `backtest.costs.CostConfig`, so there is one slippage source, never two inconsistent ones.
  Partial fills are an explicit opt-in (`DryRunConfig.partial_fill_ratio`) that exercises the four-state contract end to end; faithful partial-fill microstructure needs order-book depth the 4H/daily data plan does not carry (see "Data plan"), so it is a documented deterministic stand-in, off by default.

- **Risk sizing: tiered schedule, slider, and the kill-switch clamp (`crypto_trader.paper.risk`).**
  PRD 4.1 tiers (4/3/2% by equity) auto-adjust at every plan from `get_balance()`; the slider (0.25x-2.0x) multiplies the tier base; then a HARD clamp caps the effective per-trade risk STRICTLY BELOW the 6% daily kill switch (captain decision 2).
  The clamp ceiling is a builder-proposed, overridable `RiskConfig.max_effective_risk_fraction = 0.05` (one whole point below the 6% kill): because a stop always slips, a nominal "1R" stop-out loses slightly MORE than the risk fraction, and 5% keeps even a slipped single-trade stop-out under the daily kill with headroom.
  The min-notional check runs at PLAN time (PRD 4.2): a plan that cannot meet `SymbolRule.min_notional(price)` (or rounds to zero size) is VOIDED with a visible reason on the plan, never silently dropped.
  The approval card shows exactly one risk number (`TradePlan.risk_line`, e.g. "Risk: 4% tier, $18.40 on this trade"); tier/slider/clamp arithmetic stays out of the human's view (conduct rules 5, 8).

- **Approval seam: an ABC, fail-closed, with a reserved kill-switch event (`crypto_trader.approval`).**
  `ApprovalChannel` is an `abc.ABC` for the same reason as `ExchangeAdapter` (money-adjacent, multi-method, two named implementers), so a missing method fails loudly at construction.
  `request_approval(plan) -> ApprovalDecision` gates submission: an order reaches the exchange ONLY on a recorded APPROVE, and every implementation FAILS CLOSED (a timeout, transport error, or unauthenticated actor returns REJECT, never APPROVE), because an approval bypass is a Gate 2 critical failure.
  `notify(ChannelEvent)` is one-way; `EventKind` already reserves `KILL_SWITCH_FIRED`/`KILL_SWITCH_REARMED` so step 5 uses this seam without a breaking change.
  The Telegram surface (`approval.telegram`) is real, complete code over `requests` (no external Telegram library, so nothing to install and the transport is a one-object mock in tests); it reads the token once from the environment (`crypto_trader.secrets`, redacted repr, never logged or stored per PRD 9.4), allowlists exactly one `chat_id` (every other chat is refused), and fails safe when no token is configured (`from_env` returns None with a clear message and the loop uses the in-memory channel).

- **Position-manager lifecycle is driven by exchange truth, not by assumption (`crypto_trader.paper.position_manager`).**
  Each bar the manager syncs FLAT->PENDING->PARTIAL/OPEN->FLAT from what `get_positions`/`get_open_orders` actually report (governing principle 1), attributing an exchange-side close (stop or take-profit) from the adapter's returned events; it never assumes a placed order executed.
  It consumes the unmodified `generate_signal` as the single decision authority (entry when FLAT, trailing/momentum management when exposed), so paper, backtest, and live cannot diverge on the decision.
  The overtrading governor's `traded_zones` memory persists across a closed trade back to FLAT (one zone, one trade).

- **Reconciliation tolerance is defined numerically, in three parts (`crypto_trader.paper.reconciliation`).**
  A naive equality check false-positives against ordinary funding/fee accrual (design review, PRD 9.2), so "clean match" is: a SIZE epsilon of `size_tolerance_lots` lots at the symbol's `base_precision` (default 1 lot); a PRICE tolerance of `price_tolerance_ticks` ticks at `quote_precision` (default 1 tick, since a re-placed trailed stop is tick-rounded); and a funding/fee ACCRUAL allowance of `equity_accrual_fraction` of notional (default 0.5%) applied to MONETARY comparisons (equity) only, never to size or side (funding never changes those).
  Protective-stop presence is a hard boolean check (exposure with no reduce-only stop resting is a "stop failed to rest" critical drift).
  Cadence: reconciliation runs at the END of every decision cycle (each newly-closed 4H bar), after the manager has synced and acted; on a drift the symbol FREEZES (refuses new entries, keeps managing any existing position conservatively) and alerts, and resumes only on a later clean reconciliation.

- **The loop and its Gate 2 audit (`crypto_trader.paper.loop`).**
  `PaperTrader` ties adapter + manager + reconciliation together bar by bar, reusing the backtest engine's `causal_windows` as the single no-lookahead authority (not re-implemented, so paper and backtest cannot diverge on which candles a decision sees), and replays symbols sequentially over one shared account exactly as the Gate 1 backtest does (true wall-clock interleaving of concurrent symbols is a step-6 concern).
  At the end of a run it audits the four PRD 6.2 critical-failure classes explicitly (approval bypass, a stop that failed to rest, a fill not traded through, undetected reconciliation drift) and reports `gate2_clean`.

## Architecture decisions from Build Order step 5 (kill switch and daily digest)

Step 5 is the backend half of "Interfaces": the kill switch and the daily digest, in
`src/crypto_trader/safety/`.
The read-only TUI and the web UI are covered by the separate `crypto-trader-web` app (its
Terminal monitor screen substitutes for the TUI); this task does not touch that repo.
It is independent of the REST API (not yet planned) and of any parallel strategy-retune task
(different files: `safety/`, `paper/loop.py`, `paper/position_manager.py`,
`approval/channel.py`'s `EventKind`, versus `strategy/config.py`).
It consumes the unmodified `generate_signal` core and the characterization-spike
`ExchangeAdapter`/`ApprovalChannel` seams unchanged; the only interface change is one additive
`ExchangeAdapter` method, matching the `place_market_order` precedent from step 4.

- **`ExchangeAdapter.consecutive_api_failures() -> int` was ADDED (flagged, not silent).**
  PRD 9.1's "5 consecutive API failures" auto-trigger needs the adapter to track and report its
  own connectivity health (governing principle 1: the adapter is the source of truth for exchange
  state, including whether it can currently reach the exchange).
  This is purely additive: a new abstract method plus a new `ExchangeConnectionError` exception
  (`crypto_trader.exchange.adapter`), with `DryRunExchangeAdapter` implementing both via a shared
  `_guard()` helper called at the top of every method that represents a real venue round trip.
  DryRun has no real network, so failures are never spontaneous: `simulate_outage(n)` is the
  explicit test/simulation hook (the same pattern as `partial_fill_ratio`, a documented
  deterministic stand-in for something the venue would do on its own) that makes the next `n`
  guarded calls raise `ExchangeConnectionError` and increments the tracked count; any guarded
  call that succeeds resets it to 0.
  `test_exchange_adapter.py`'s stub adapter was updated to implement the new method, exactly as
  step 4 updated it for `place_market_order`.

- **KillSwitch is a small state machine with two booleans, not one (`crypto_trader.safety.kill_switch`).**
  `is_armed` goes False the INSTANT `trigger()` is called: this is the "halts signal generation"
  and "requires re-arm" obligation (PRD 9.1), and PositionManager checks it in
  `_maybe_seek_entry` before any new entry, the same gate shape as the existing per-symbol
  `frozen` check from reconciliation.
  `flatten_confirmed` is separate and starts False on trigger: it becomes True only once exchange
  truth (not an accepted order, an actually-confirmed empty `get_positions()`/`get_open_orders()`)
  proves flat.
  This split directly resolves the PRD 9.1 open risk: the halt can never be blocked by a degraded
  exchange, but "killed" is never reported on hope alone.
  `rearm()` is the operator action that resumes trading; wiring an actual `/rearm` Telegram command
  is out of this task's scope (PositionManager and KillSwitchMonitor are the component, not the
  control surface).

- **Degraded-mode fallback, resolving the first PRD 9.1 open risk (`crypto_trader.safety.monitor.KillSwitchMonitor`).**
  `check_cycle(now=...)` runs once per decision cycle, called from `PaperTrader._run_symbol`
  BEFORE the position manager acts on that bar (so a fresh trigger halts new entries the same bar
  it fires) and evaluates, in order, consecutive API failures, daily loss (6%), max drawdown
  (15%); the first breach calls `_trigger`, which halts immediately then attempts to flatten.
  `_attempt_flatten` cancels every resting order and closes every open position at market,
  catching `ExchangeConnectionError` from each call; on a failure it alerts through
  `ApprovalChannel` (`EventKind.KILL_SWITCH_DEGRADED`) and backs off (`sleep_fn`, injectable for
  tests) before retrying, bounded at `flatten_max_attempts` (default 5) within one cycle.
  If none of those attempts confirms flat, `flatten_confirmed` stays False and the NEXT cycle's
  `check_cycle` takes an early-return branch that keeps retrying (not re-evaluating auto-triggers)
  until `_confirm_flat()` reads back empty positions and orders, at which point it alerts
  `EventKind.KILL_SWITCH_FLATTENED` and marks confirmed.
  Throughout, the already-placed on-exchange resting stop from each position's entry (principle 4)
  is what actually protects the account; this class only removes it once it can prove the position
  is already gone.

- **The websocket-dead trigger is a documented, unwired, pure function (`websocket_dead_trigger` in monitor.py), per the task brief.**
  The paper/DryRun adapter has no real websocket, so `KillSwitchMonitor.check_cycle` never calls
  it: the task brief explicitly forbids fabricating a heartbeat timestamp for a trigger that
  cannot genuinely fire yet.
  The function itself (condition only: stale heartbeat AND open positions) is written and tested
  so step 6's live adapter can call it once it has a real `last_message_at`.
  **Still-open judgment call step 6 must resolve explicitly (not resolved here, per design
  review and the task brief):** whether the correct ACTION on this condition is auto-flatten
  (call the same trigger path as the other three auto-triggers) or alert-and-hold, since
  auto-flattening purely because a websocket dropped can fight "on-exchange stops are the floor"
  when REST and the resting stops are actually fine.
  `websocket_dead_trigger`'s docstring carries this note inline so a step-6 reader hits it at the
  point of use, not only here.

- **EquityTracker is the one shared source for "today's start" and "peak" equity (`crypto_trader.safety.equity`).**
  Both the daily-loss/max-drawdown triggers and the digest's P&L/drawdown lines need the same two
  numbers; tracking them twice risks disagreement, the same anti-pattern the DryRun adapter's
  shared `CostConfig` avoids for slippage.
  `KillSwitchMonitor` owns one instance (`monitor.equity`) and `PaperTrader` passes that same
  instance to the digest scheduler each bar, so the two surfaces never diverge.
  Known, accepted limitation matching an existing one: the paper loop replays symbols
  SEQUENTIALLY, one symbol's whole history before the next (see the step-4 entry below,
  "The loop and its Gate 2 audit"), so a multi-symbol paper run's day boundaries do not reflect
  true wall-clock order; this is correct for live operation (step 6, one real timeline) and for
  single-symbol replay, which is what it is tested against.

- **Daily digest: six lines, one clear number each, and an honest degrade (`crypto_trader.safety.digest`).**
  `build_daily_digest` reads equity, today's P&L, drawdown, open position count, kill-switch
  state, and rate-limit/API health from the adapter and the shared `EquityTracker`; `format_digest`
  renders exactly six lines, matching the `TradePlan.risk_line` philosophy (conduct rules 5, 8: UI
  encapsulates complexity, one settled number per concept, no internal state dump).
  It degrades rather than skipping the send when the exchange is unreachable
  (`exchange_reachable=False`, an explicit "UNREACHABLE" line): PRD 9.5 frames the digest itself as
  the heartbeat ("silence means the bot died"), so the digest going out AT ALL, even reporting bad
  news, is itself useful information.
  `DailyDigestScheduler` fires once per UTC calendar date; `PaperTrader` calls it once per bar
  AFTER the manager acts, so the digest reflects that bar's post-action state.
  **Detecting a MISSED digest is explicitly NOT built here** (the task brief only requires noting
  it): a dead process cannot alert about its own silence, so that needs an external watcher
  independent of this process; this is future ops work, documented in `digest.py`'s module
  docstring.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
