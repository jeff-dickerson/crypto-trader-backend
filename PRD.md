# Crypto Trader: Product Requirements Document

This document is the authoritative, current requirements record for Crypto Trader.
It synthesizes the captain's original Build Spec with the five decisions the captain made after a pre-implementation design review of that spec.
Where a decision supersedes the original spec, this document states the decided requirement and briefly notes what changed and why.
See [AGENTS.md](AGENTS.md) for build/test commands, committed architecture decisions, and conduct rules; see [README.md](README.md) for a practical entry point.

## 1. Mission

A swing-trading bot for crypto perpetual futures on Bitunix, adapter-scaffolded for any venue, built for ugly data, driven by risk management first and profitability second.
The human approves; the machine executes and manages.
Performance is the primary driver.

The original spec cited a validated edge of +0.22R expectancy (47% win rate, 1.52 payoff) as the profitability baseline to re-prove on crypto.
That triple does not reproduce: 0.47 x 1.52 minus 0.53 x 1.0 equals +0.184R, not +0.22R.
This document states +0.184R as the reproduced baseline, per the project's own "reproduce, don't defend" rule (see section 9, decisions log item 5's companion finding).
The original spec's "+15-25% annually if edge holds" projection is illustrative only, not a validated projection: it was never independently reproduced and rests on the uncorrected expectancy figure.

## 2. Governing principles

These apply to every component built in this project, for its life.

1. **Exchange is the single source of truth.** Analyze exactly what is executed against; account and position state come from the exchange, not from bot memory.
2. **Pure decision core.** `generate_signal(candles, htf_candles, position_state, config)` is a pure function of its inputs: no I/O, no wall clock, no global state.
   Backtest, paper, and live all consume the identical function.
3. **No-lookahead, ever.** A still-forming candle must never be exposed to any consumer, at any timeframe, at any layer.
   Purity of `generate_signal` guarantees referential transparency, not no-lookahead by itself: lookahead is a property of how the input window is constructed and sliced, not of the function that consumes it.
   The enforcement point is therefore the candle-slicing and window-assembly layer that every caller shares, not `generate_signal`'s internals alone.
   This applies to the daily bias candle, the 4H volume profile, and zone/separation-return detection alike: none of these may read a candle after the current decision point.
4. **On-exchange stops are the safety floor.** Isolated margin, stops resting server-side; the bot is redundancy, not sole custody.
5. **Ugly data is normal.** Dedup, gap detection, and timestamp sanity run on every ingested candle; rate-limit status stays visible; drift is reconciled against the exchange, never trusted memory.

## 3. v1 strategy: Volume Profile POC Zone Retest (HTF-gated)

Implemented as the pure strategy core in `src/crypto_trader/strategy/`.
`candles` is the primary 4H timeframe; `htf_candles` is the daily higher timeframe used only for the bias gate; the current decision point is always the last 4H candle.

### 3.1 Mechanics

- **Bias gate:** daily candles resolve to long, short, or neutral; only trade with bias.
- **Zone construction:** 4H candles, a lookback within 30 to 90 days, bot-computed volume profile (POC, VAH, VAL, HVN, LVN).
- **Setup requirement:** price must separate from the zone, then return.
- **Entry:** resting limit order at the zone boundary (value-area edge, never the POC centre line), first touch only; subsequent retests of an already-traded zone are invalid.
- **Stop loss:** behind the heavy-volume barrier, in the adjacent LVN beyond the entry zone.
- **Take profit:** just before the next opposing HVN.
- **Trade management:** trailing stop arms at a profit threshold; a momentum-shift exit fires when the MA ensemble flips against the position.
- **Overtrading governor:** one zone, one trade, done; the first-touch principle is structural, enforced by `PositionState.traded_zones` persisting across a closed trade.
- **Timeframes:** daily bias, 4H profile, first-touch entry; trades held days.
- **Universe:** top 10 to 15 Bitunix perpetuals by 24-hour volume, above a liquidity floor (24h volume threshold plus an order-book depth check); rule-defined and self-updating.
- **v2 (deferred):** a breakout, retest, swing-high ladder strategy behind the same signal-engine interface.

### 3.2 Parameter defaults (captain decision 5: builder proposes, Gate 1 validates)

The original spec left volume-profile bucketing, the MA ensemble, and entry/stop/TP derivation as unspecified prose ("adjacent LVN," "before the next opposing HVN").
Per captain decision 5, the builder of Build Order step 2 proposed concrete values instead of the captain hand-specifying them, with every value living as a named, overridable field on `StrategyConfig` (`src/crypto_trader/strategy/config.py`) rather than a magic number in logic.
None of these are settled truth: they are reasoned defaults, pending the future Gate 1 out-of-sample backtest.

| Area | Default | Rationale (summary) |
|---|---|---|
| Volume profile bucketing | 24 fixed bins across the lookback price span | Self-scales across symbols; a fixed price increment would need per-symbol tuning |
| Value area | 70% | Conventional TPO/volume-profile default |
| HVN cut | >= 1.3x mean bin volume | Genuine cluster, relative to the mean so empty bins drag it down correctly |
| LVN cut | <= 0.5x mean bin volume | Liquidity void, same relative basis |
| Lookback | 60 days, pinned (not swept) | Midpoint of the allowed 30-90 day range; a swept value would be a tuned parameter that only a Gate 1 OOS number can honestly validate, so it stays fixed until Gate 1 exists |
| Minimum profile history | 60 4H candles (~10 days) | Below this, no entry signal; not enough history to trust the profile |
| Separation | >= 1.5% beyond the boundary, for >= 3 consecutive 4H candles (12 hours) | A single wick should not qualify as separation |
| Entry boundary | Value-area edge (VAL for a long, VAH for a short) | Structural rule, never the POC centre line |
| Stop | Adjacent LVN bin centre, nudged 0.1% further into the void; fallback 1% beyond the boundary if no LVN exists in-window | Ordinary wicks into the void should not stop the trade out; fallback covers the data-edge case |
| Take profit | Next opposing HVN's near edge, pulled back 0.2%; fallback 2.0R if no opposing HVN exists in-window | Exit into liquidity, not through it |
| MA ensemble (momentum-shift exit) | 3-SMA ensemble on 4H closes: 10 / 20 / 50, majority vote (>= 2 against position) | Exit reflects agreement across horizons, not one noisy line; full ensemble must be computable or the position holds |
| Trailing stop | Arms at +1.0R, trails 1.0R behind the best price | Reaches breakeven at +1R and only ratchets tighter from there |
| Overtrading governor | Two boundaries within 0.5% count as the same zone | A recomputed boundary that drifted slightly still counts as a retest |
| Daily bias gate | Fast/slow SMA, 20/50, neutral band 0.2% of the slow SMA | A razor-thin cross should not declare a directional bias |

### 3.3 Position-state contract

`PositionState` (`src/crypto_trader/strategy/position.py`) has four states, not a flat/in-position boolean: FLAT, PENDING (entry limit resting, unfilled), PARTIAL (partially filled, real exposure), and OPEN (fully established).
This exists because a resting limit at a zone boundary can partially fill.
Management rules (trailing stop, momentum-shift exit) run for PARTIAL and OPEN; PENDING holds and awaits a fill; FLAT seeks an entry.
`generate_signal` never mutates a `PositionState`; it is frozen, and a future position manager threads an updated state into the next call.
Order placement and fill tracking are future paper-loop work; the pure core only defines and consumes the contract.

### 3.4 Funding cost treatment (captain decision 4)

The original spec listed funding only as a backtest cost-model line item.
Design review found that funding on a multi-day, with-bias hold can equal or exceed the entire edge in a hot trend (reproduced: 0.04R in calm funding, 0.20R elevated, 0.40R in a hot trend, against a ~0.18R edge), and that bias-gating tends to put the strategy on the crowded, funding-paying side.
The captain's decision: a funding-cost filter or penalty for with-bias entries is required future strategy work, on top of keeping funding in the backtest cost model.
Not implemented yet.

## 4. Risk system

### 4.1 Tiered risk schedule (auto-adjusting at every trade plan)

| Equity | Risk / trade |
|---|---|
| < $1,000 | 4% |
| $1,000 to $2,000 | 3% |
| >= $2,000 | 2% |

### 4.2 Risk slider vs. kill switch (captain decision 2, supersedes the original spec)

The original spec specified a 0.25x to 2.0x slider multiplier on the tier base, with the daily kill-switch limit stated to "survive the slider untouched."
Design review found these two rules contradict each other on a small account: at 1.5x on the 4% tier, a single ordinary stop-out (6% loss) trips the 6% daily kill switch by itself; at 2.0x, one stop-out (8%) exceeds it, and two (15.36%) exceed the 15% max-drawdown kill.
The captain's decision: effective per-trade risk must be capped strictly below the daily kill-switch threshold, regardless of the slider setting.
The hard daily kill limit continues to survive the slider untouched; what changes is that the slider's usable range is constrained so it can never push a single trade's risk above that threshold.
**Not implemented yet; a locked requirement for the future risk-system task.**

Remaining requirements from the original spec, unchanged by the decision:
- The slider is live in every interface, with a `/risk` Telegram command and a confirmation step above 1.5x (once the capped ceiling makes that range reachable), settable via natural language.
- Slider honesty: at low settings on small accounts, plans that fail minimum-notional are voided visibly, not silently.
  Design review recommends checking minimum-notional at plan time (before approval), so a user never approves a plan that cannot execute.

### 4.3 Honest math

At 47% win rate, 4% tier risk, and compounding on current equity: a 5-loss streak produces roughly 18% drawdown (1 minus 0.96^5), survivable but painful, the tuition of the small-account phase.
Design review adds a reproduced context figure: the probability of at least one 5-loss streak within any 20-trade window at this win rate is about 31%, so this drawdown is a roughly 1-in-3 event during the live validation floor, not a tail case; plan user expectations for the small-account phase around that, not around the average.

### 4.4 Expected performance

Illustrative only, not a validated projection: +15-25% annually if the edge holds, per the original spec.
See section 1 for why this figure is not currently backed by a reproduced number.

## 5. Success definition and capital management

### 5.1 Scaling gate (process metrics)

- Positive expectancy after 20 live trades.
- Max drawdown < 15%.
- >= 95% rule adherence: every trade had a stop before entry, zero unplanned exits, zero plan-format bypasses.

### 5.2 Capital overlay (opt-in, default off)

- **ON:** equity doubling from cycle start raises a flag; the captain approves a withdrawal; profit sweeps to spot; the cycle resets.
- **OFF:** compounding continues, and risk tiers step down automatically as equity grows.
- The toggle only changes at cycle boundaries.

## 6. Validation gates

### 6.1 Trade-count statistical bar (captain decision 1, supersedes the original spec's framing)

The original spec called "positive expectancy after 20 live trades" a statistical floor.
Design review found this false as a statistical claim: at the reproduced edge (mean +0.184R, sd 1.258R per trade), 20 trades gives a t-stat of 0.66 and a 95% confidence interval of [-0.37, +0.74], indistinguishable from no edge.
Reaching a one-sided 95%-confidence, 80%-power confirmation that the edge is positive takes roughly 288 trades; at this strategy's cadence (multi-day holds, first-touch-only, a 10-15 symbol universe), that could take a year or more of live calendar time, which the captain rejected as too slow to gate on.

The captain's decision: keep the requirements as two different kinds of gate, not one.

- **Live validation floor (20 trades): an operations check only, never statistical proof of the edge.**
  It verifies every trade had a stop, zero unplanned exits occurred, and zero rule-breaking bypasses happened.
  Trading past this floor continuously accumulates statistical confidence; it is not a second one-time statistical gate that blocks scaling.
- **Backtest gate (Gate 1): tightened to roughly 150-300 signals, gated on a confidence interval that excludes zero,** not a raw point estimate above a fixed threshold.
  Backtest time is computer time, not calendar time, so this is where the statistical burden belongs.
  The signal/trade denominator must be pinned explicitly (closed trades is the honest one for expectancy; a resting-limit, first-touch strategy also produces signals that never fill and trades that fill only partially, so state which count each gate number refers to).

### 6.2 The three gates

1. **Backtest (Gate 1, "proves the edge").** 2 to 3 years of data, real costs (fees, funding, slippage, minimum notional).
   Tune on the first two-thirds, score frozen on the final third.
   Target roughly 150-300 signals, out-of-sample expectancy with a confidence interval excluding zero, no rule-breaking drawdown (the same 15% max-DD bar as the live gate).
   **Harness built and run end to end on real data; Gate 1 NOT PASSED.**
   The Build Order step-3 lab (`src/crypto_trader/backtest/`) implements all of the above: no-lookahead per-bar slicing, the cost model including funding across the hold, the captain-decision-4 funding filter, the lookback-and-filter sweep with the frozen out-of-sample third, and the three pinned denominators (signals, fills, closed trades).
   It was first run only on deterministic synthetic data (the report verdict for a synthetic run is hard-coded to "NOT PROVEN"; one example report stays committed at `backtest_reports/EXAMPLE_synthetic_gate1.md`).
   A real run has now happened: 14 symbols (BTCUSDT and ETHUSDT plus 12 best-effort top-volume Bitunix perpetuals) ingested from Bitunix's public kline endpoint, BTCUSDT/ETHUSDT/most majors reaching the full 3-year window and younger listings reaching as far back as their listing date, all details in `backtest_reports/gate1_real_2026-08-21.md` (and `.json`).
   The chosen combination (45-day lookback, funding filter off, selected in-sample) produced only 33 out-of-sample closed trades, well short of the 150-300 target, with an out-of-sample mean expectancy of -0.163R and a 95% CI of [-0.448, 0.156] that does not exclude zero: **Gate 1 fails on both the signal-count bar and the confidence-interval bar**, and the point estimate is negative, opposite the sign the synthetic harness run and the design-review reproduction assumed.
   This is a genuine, captain-relevant finding, not a harness defect: the current 14-symbol universe over the reachable Bitunix history does not generate enough signals to reach statistical power in a reasonable backtest window, and the real out-of-sample edge as currently parameterized shows no evidence of being positive.
   Candidate next steps (not applied by this task, per captain decision 5: a parameter change needs a captain decision, not a unilateral edit): expand the symbol universe further, revisit the pinned 60-day lookback (the sweep already covers 45/60/75 and none look better in this real run), or accept that the current strategy parameterization does not clear Gate 1 as specified.
2. **Paper (Gate 2, "proves the machine").** A paper adapter implementing the identical exchange interface; honest fills (limits fill only when the market trades through, stops always slip).
   4 weeks minimum, zero critical failures (enumerated concretely: undetected reconciliation drift, a stop that failed to rest server-side, an approval bypass, a fill modeled as filled when the market did not trade through it), full reconciliation.
   **Not started.**
3. **Live (Gate 3, "proves reality").** Minimum viable size, the 20-trade operations floor from section 6.1, positive expectancy (not negative) with zero ops failures, then scale.
   **Not started.**

Each gate can kill the build; that is the intended point of gates.

## 7. Architecture

Three swappable seams: exchange adapter, strategy module, interface layer.
The decision core stays pure; LLM or natural-language processing never touches the signal engine.

```
INTERFACES (v1 scope, see section 8): Telegram (control + approval) | read-only TUI
        | REST API (the seam)
SIGNAL ENGINE (pure) -- POSITION MANAGER:
  generate_signal() | volume profile | bias | approval gate | trade plans |
  OCO emulation | trailing stops | reconciliation
        | ExchangeAdapter interface
ADAPTERS: Bitunix | Paper (DryRun) | future
```

Two architecture notes from design review, not yet resolved by implementation:

- **Purity is necessary but not sufficient for no-lookahead** (see principle 3 in section 2).
  The shared input-assembly/slicing layer that backtest, paper, and live all call is where the guarantee is actually earned, and where its tests should concentrate, not solely inside `generate_signal`.
- **Entries are bar-modelable; management is path-dependent within a bar.**
  A resting-limit first-touch entry is cleanly modeled on a 4H bar (did the bar's range trade through the limit).
  Trailing-stop and momentum-shift management are not: on a single 4H bar it is not knowable whether the stop or the momentum flip happened first.
  Backtest and live can diverge exactly on the trades where management matters most.
  The committed mitigation (see [AGENTS.md](AGENTS.md), "Data plan: 4H and daily candles only, by design") is a conservative intrabar ordering rule in the future backtest engine: assume the worse of stop-vs-TP within a bar, rather than silently assuming a favorable fill.

## 8. Interfaces (v1 scope, per captain decision 3, supersedes the original spec)

The original spec specified four parallel front-ends plus a speech-to-text pipeline for v1: Telegram, a five-tab web UI, a TUI, an NL/voice query layer with Whisper.
Design review found this was not actually a "trimmed v1" as claimed, and that the web UI plus voice deliver little beyond what Telegram and a TUI already cover for a single-user bot, while consuming a large share of the quality budget that conduct rule 4 (quality over development cost) and conduct rule 5 (pixel perfection) commit to for every shipped surface.

The captain's decision: **v1 ships Telegram plus a read-only terminal monitor only.**
The five-tab web UI and Whisper voice are deferred past v1, to v1.x or v2.

- **Telegram (v1, the control and approval surface):** full command vocabulary (`/status`, `/profit`, `/daily`, `/forceexit`, `/risk`, and others), inline Approve/Reject/Modify buttons on every trade plan, a daily digest, and the kill switch.
  Design review flags Telegram as a money-adjacent control surface: the allowlisted `chat_id` must be pinned, and approval buttons must be treated as authenticated actions, not open to any chat that finds the bot.
- **TUI (v1, read-only):** a lightweight `rich` monitor showing positions, pending approvals, and recent signals.
- **Web UI (deferred to v1.x/v2):** the original five-tab structure (dashboard, open/closed trades, approval history, settings) remains the design if and when it is built, including the risk slider, overlay toggle, and rate-limit status requirements, and a Qt-free logic-separation pattern.
- **Natural language (deferred, tiered):** Level 1 (regex-first query parsing) targeted for v1.x; Level 2 (guarded command execution, every consequential action still routed through the approval gate) for a later v1.x; Level 3 (LLM retrieves and explains, never modifies parameters without explicit numeric approval) for v2+.
- **Voice (deferred to v2+):** speech-to-text feeding the same NL layer, query-only, never voice approval.

Regardless of deferral, conduct rule 2 (E2E-first bug reproduction) and the "paper loop runnable for the life of the bot" rule mean the Telegram approval path is a Gate 2 dependency from Build Order step 4 onward, not a step-5 nicety; its quality bar applies as soon as it exists.

## 9. Ops and safety

### 9.1 Kill switch

Triggers on all interfaces: cancels orders, closes at market, halts signal generation, requires re-arm.
Auto-triggers: 6% daily loss, 15% max drawdown, 5 consecutive API failures, websocket dead for more than 5 minutes with open positions.

**Open risk, not yet resolved by implementation or by any captain decision (from design review):** two of the kill switch's own auto-triggers are API failure and a dead websocket, which are exactly the conditions where its own remedy (cancel and flatten via that same API) may not be able to execute.
This needs a defined degraded-mode fallback before the live adapter (Build Order step 6) ships: when flatten cannot reach the exchange, rely on the on-exchange resting stops as the stated floor, keep retrying cancel/flatten with backoff, alert loudly and repeatedly, and do not report "killed" until the exchange confirms flat.
A related, separate judgment call flagged by the same review: auto-flattening at market purely because the websocket dropped can fight the "on-exchange stops are the floor" principle if REST and the resting stops are actually fine; consider preferring alert-and-hold over market-flatten in that case, reserving auto-flatten for when the stops themselves cannot be confirmed present.

### 9.2 Exchange downtime and reconciliation

Exchange downtime: retry with backoff, alert; on-exchange stops hold the floor.
Reconciliation drift: exchange is truth; freeze the affected symbol, alert, reconcile, resume only on a clean match.
Design review notes that a naive equality check will false-positive against ordinary funding and fee accrual between reconciliation cycles; "clean match" needs a numeric tolerance definition (size epsilon at exchange lot precision, price rounding, a funding/fee accrual term) before the reconciler is built in Build Order step 4.

### 9.3 Bitunix API surface (characterized by a read-only spike; residual items still open)

The read-only Bitunix characterization spike recommended by design review has run (no orders placed, no credentials used, public documentation and public unauthenticated REST endpoints only), inserted between Build Order steps 3 and 4.
It produced the `ExchangeAdapter` interface (`src/crypto_trader/exchange/`) designed against verified reality, and the full findings with endpoint citations are in [AGENTS.md](AGENTS.md), "Architecture decisions from the Bitunix characterization spike."
Headline results:

**Confirmed against the public API.**
Native server-side stops exist (a MARKET order gated on a trigger price via the TP/SL surface), so "on-exchange stops are the floor" is literal, not emulated.
Reduce-only is a native order flag.
A paired take-profit + stop-loss bracket on a position is a single native call, and that is the specific "OCO" this strategy needs; arbitrary OCO between two unrelated orders is not offered.
No native trailing stop was found in the documented REST endpoints, so trailing is emulated bot-side (the strategy core already computes the trail).
Rate limits are simple fixed caps with no weight accounting and no quota headers: REST market data 10 req/sec/IP, private trade endpoints 10 req/sec/UID, WebSocket 5 inbound messages/sec.
Per-symbol precision and a per-symbol minimum ORDER SIZE come from the public trading-pairs endpoint, but there is no standalone minimum-NOTIONAL field, so the plan-time notional floor (section 4.2) is derived as min order size times price.
The funding interval is 8 hours, confirming the original assumption, and historical funding rate is available from a public endpoint, so the Gate 1 cost model (step 3) and the funding filter (decision 4) can source real funding from Bitunix itself.
Historical candle depth reaches about 4.35 years for BTCUSDT and ETHUSDT at both 4H and daily (paginated back via startTime/endTime past the 200-row-per-request cap), so Gate 1's 2-3 years of data is achievable from Bitunix alone for established majors; history is per-symbol and bounded by listing date, so the section 10 "history supplement from a major venue" fallback is only needed for symbols younger than the Gate 1 window, not for the BTC/ETH core.

**Still open, deferred to the live adapter (Build Order step 6) with credentials.**
Position mode is confirmed account-global (one-way or hedge) but the live account's actual current mode needs an authenticated read, so the interface reports it rather than hardcoding an assumption.
Margin mode is confirmed per-symbol (isolation or cross); isolation is the principle-4 floor and must be set explicitly per traded symbol, since the majors show a non-isolated default in public data.
WebSocket reconnect/resync semantics are thin in the public docs; a public stream is not needed for the interface, so it carries no streaming method and step 6 owns any stream it adds (this residual item also feeds the kill-switch degraded-mode work in section 9.1).

### 9.4 Secrets (four layers)

1. Docker secrets or `.env` plus a gitleaks pre-commit hook.
2. Secrets load once into an in-memory object; the config plane structurally cannot contain them.
3. A redaction filter scrubs secret values from every log sink.
4. The Bitunix API key is trade-only, withdrawals disabled, IP-allowlisted.
   No Vault at this scale.
   The database never stores secrets.
   Optional: 1Password or Bitwarden CLI injection.

Design review notes two soft spots worth closing: the local gitleaks pre-commit hook is bypassable with `--no-verify`, so a server-side/CI gitleaks run is recommended in addition; and log redaction is best-effort by nature, so the structural control (layer 2, secrets never entering loggable objects) should be treated as the strong guarantee, with redaction as defense-in-depth rather than the primary control.

### 9.5 Heartbeat

A daily digest; silence means the bot died.

## 10. Data and storage

- One unified SQLite file: 4H and daily candles (3+ year retention, whole universe), all trades, signals, and journal entries forever (never pruned), config history.
- On the order of tens of MB total, reproduced by design review at roughly 200,000 candle rows for a 15-symbol, 3-year, 4H-plus-daily universe.
- Nightly local rotated backups.
  Journal and trade history are irreplaceable; candles are re-fetchable.
  Per [AGENTS.md](AGENTS.md), the backup mechanism (`crypto_trader.db.backup.backup_database`) uses SQLite's online backup API, never a raw file copy of a live WAL-mode database.
- The candle-ingest validator (`crypto_trader.ingest.validate.validate_candles`) is the first line of defense against ugly data; see [AGENTS.md](AGENTS.md) for the no-lookahead enforcement point and the single-writer SQLite invariant.
- History supplement from a major venue is permitted if Bitunix's own depth is shallow, but research/backtest only, never for live decisions.
- TradingView is supplementary only: approval-context chart links, and a Gate 1 sanity spot-check.

**Open risk (design review, not yet resolved):** local-only backups leave the one irreplaceable dataset (the trade/signal journal) on a single host and disk.
An off-host encrypted copy of the irreplaceable tables is recommended; this does not require Postgres or cloud infrastructure, only a second location.
Separately, if a supplemental history source is ever used for backtesting, its OHLCV agreement with Bitunix's own candles over overlapping periods should be measured before trusting both as equivalent, to avoid reintroducing a parity risk between backtest and live data sources.

## 11. Stack

Python, Docker (bot process container; a second web UI container is future work per section 8, not to be added until that task starts), ccxt-pattern adapters, a pandas backtest engine, SQLite, the Telegram bot API, a `rich` TUI, Whisper speech-to-text for the deferred voice layer.

## 12. Build order and current status

1. **Scaffold and data layer.** Project structure, Docker, the SQLite schema, candle ingest with full ugly-data validation. **Done, merged.**
2. **Pure framework.** Volume profile, bias, `generate_signal()` core, unit tests with synthetic candles engineered to trigger each path. **Done, merged.**
3. **Backtest lab (Gate 1).** Replay engine with no-lookahead slicing, cost model, parameter sweep, markdown/JSON reports. **Done (harness and a real run); Gate 1 NOT PASSED.**
   Built in `src/crypto_trader/backtest/`: a no-lookahead replay engine, a fees/funding/slippage/minimum-notional cost model, the captain-decision-4 funding filter, a lookback-and-filter sweep with a frozen out-of-sample score, and markdown/JSON reports; see [AGENTS.md](AGENTS.md) for the step-3 architecture decisions.
   First run only on synthetic data (the harness-only signal); a real run has since happened against 14 symbols of ingested Bitunix candles and Gate 1 failed on both bars (33 out-of-sample closed trades against the 150-300 target, a negative out-of-sample mean expectancy with a confidence interval that does not exclude zero), see section 6.2 and `backtest_reports/gate1_real_2026-08-21.md` for the full honest result.
   Design review's recommended read-only Bitunix spike (section 9.3) has also run since; see [AGENTS.md](AGENTS.md).
4. **Paper loop (Gate 2).** DryRun adapter, position manager, approval flow, reconciliation, the Telegram bot with approval buttons. **Not started.**
5. **Interfaces.** The read-only TUI, daily digest, kill switch (Telegram's control and approval surface is a step-4 dependency per section 8, not deferred here). **Not started.**
6. **Bitunix live adapter (Gate 3).** Smallest viable size, scale after it is earned. **Not started.**

## 13. Deferred to v2+

- The breakout, retest, swing-high ladder strategy (behind the same signal-engine interface).
- Mid-cap universe expansion beyond the top 10-15 by volume.
- Plot configurator and backtest comparison views.
- Level 3 conversational strategy adjustment.
- Voice command execution (voice stays query-only through v1 and v1.x).
- Postgres migration, only if earned.
- Public-facing web authentication.
- The five-tab web UI and Whisper voice specifically are deferred by captain decision 3 (section 8), from v1 to v1.x/v2 rather than to v2+ outright; they are listed here for completeness of the deferral picture.

## 14. Decisions log

Five decisions the captain made when reviewing the original Build Spec, each superseding part of that spec.
Full records live in `data/crypto-trader-buildspec-review/decisions/` in the firstmate home; this is the durable in-repo summary.

1. **Trade-count validation gates** (section 6.1). The live floor stays at 20 trades as an operations check only, never statistical proof.
   Gate 1 tightens to roughly 150-300 signals with a confidence interval excluding zero, because backtest time is computer time, not calendar time.
2. **Risk slider vs. kill switch** (section 4.2). Effective per-trade risk is capped strictly below the daily kill-switch threshold, regardless of the slider setting, resolving a contradiction where a high slider on a small account could let one or two ordinary stop-outs trip the kill switch by themselves.
3. **v1 interface scope** (section 8). Trimmed to Telegram-first plus a read-only terminal monitor; the five-tab web UI and Whisper voice move to v1.x/v2.
4. **Funding cost treatment** (section 3.4). A funding-cost filter or penalty for with-bias entries is added to the future strategy work, because funding on a multi-day with-bias hold can equal or exceed the entire edge in a hot trend.
5. **Strategy parameter authority** (section 3.2). The builder of each future strategy task proposes concrete values for unspecified parameters and validates them against the Gate 1 backtest, rather than the captain hand-specifying them.
   Already exercised in Build Order step 2.
