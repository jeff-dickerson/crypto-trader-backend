-- REST API persistence (Build Order step "Interfaces: backend REST API").
--
-- Single-writer invariant preserved: only the bot process writes this database, and
-- the in-process REST API is that same process (hosting decision 5), so this adds no
-- second writer. Any out-of-process reader still opens the database read-only. If the
-- API is ever split into its own process, this invariant must be revisited (AGENTS.md).
--
-- Five tables back the API's read and write endpoints:
--   signals            the strategy signal log (feeds GET /signals, /terminal, /dashboard bias)
--   trade_plans        the persisted TradePlan with its status and timestamps (feeds /approvals)
--   plan_decisions     the approval-decision audit trail (one row per operator decision)
--   closed_trades      persisted ClosedPaperTrade records (feeds GET /journal)
--   kill_switch_events the kill-switch audit trail (feeds GET /kill-switch recent events)
--
-- The 001 `signals` table was a schema-only placeholder ("populated by future tasks") and
-- was never written to. This task IS that future task, so its real, planned shape replaces
-- the placeholder. The 001 `trades`, `journal`, and `config_history` placeholders are left
-- untouched: `closed_trades` here is the paper-trade journal writer the API reads, distinct
-- from the generic `trades`/`journal` placeholders (AGENTS.md documents the distinction).

DROP TABLE IF EXISTS signals;

CREATE TABLE signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        INTEGER NOT NULL,   -- epoch ms UTC, the decision bar's close time
    symbol            TEXT    NOT NULL,
    action            TEXT    NOT NULL,   -- SignalAction value (enter_long, exit_momentum_shift, ...)
    bias              TEXT,               -- Bias value at signal time (long/short/neutral), nullable
    zone_boundary     REAL,               -- the value-area edge the signal traded against
    entry_price       REAL,
    stop_price        REAL,
    take_profit_price REAL,
    reason            TEXT,
    plan_id           TEXT,               -- nullable link to the resulting trade_plans.plan_id
    recorded_at       INTEGER NOT NULL    -- epoch ms UTC, when this row was written
);

CREATE INDEX idx_signals_created_at ON signals (created_at, id);

CREATE TABLE trade_plans (
    plan_id                 TEXT    PRIMARY KEY,
    created_at              INTEGER NOT NULL,   -- epoch ms UTC
    updated_at              INTEGER NOT NULL,   -- epoch ms UTC, last status change
    symbol                  TEXT    NOT NULL,
    side                    TEXT    NOT NULL,   -- PositionSide value (long/short)
    status                  TEXT    NOT NULL,   -- PlanStatus value
    entry_price             REAL    NOT NULL,
    stop_price              REAL    NOT NULL,
    take_profit_price       REAL    NOT NULL,
    quantity                REAL    NOT NULL,
    notional                REAL    NOT NULL,
    equity                  REAL    NOT NULL,
    tier_risk_fraction      REAL    NOT NULL,
    slider_multiplier       REAL    NOT NULL,
    effective_risk_fraction REAL    NOT NULL,
    risk_amount             REAL    NOT NULL,
    slider_capped           INTEGER NOT NULL DEFAULT 0 CHECK (slider_capped IN (0, 1)),
    void_reason             TEXT,
    failure_reason          TEXT,
    signal_id               INTEGER             -- link back to signals.id, nullable
);

CREATE INDEX idx_trade_plans_status ON trade_plans (status, updated_at);

CREATE TABLE plan_decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id    TEXT    NOT NULL,
    decided_at INTEGER NOT NULL,   -- epoch ms UTC
    verdict    TEXT    NOT NULL,   -- ApprovalVerdict value (approve/reject/modify)
    actor      TEXT,               -- who decided (allowlisted operator id), nullable
    note       TEXT
);

CREATE INDEX idx_plan_decisions_plan_id ON plan_decisions (plan_id, decided_at);

CREATE TABLE closed_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,   -- PositionSide value
    plan_id      TEXT,               -- link to the originating trade_plans.plan_id, nullable
    entry_price  REAL    NOT NULL,
    exit_price   REAL    NOT NULL,
    initial_stop REAL    NOT NULL,
    take_profit  REAL    NOT NULL,
    quantity     REAL    NOT NULL,
    exit_reason  TEXT    NOT NULL,   -- PaperExitReason value
    realized_pnl REAL    NOT NULL,
    r_multiple   REAL,               -- realized R (risk multiple), nullable if risk distance is zero
    entry_time   INTEGER NOT NULL,   -- epoch ms UTC
    exit_time    INTEGER NOT NULL,   -- epoch ms UTC
    recorded_at  INTEGER NOT NULL    -- epoch ms UTC, when this row was written
);

CREATE INDEX idx_closed_trades_exit_time ON closed_trades (exit_time, id);

CREATE TABLE kill_switch_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at    INTEGER NOT NULL,   -- epoch ms UTC
    trigger_source TEXT    NOT NULL,   -- KillSwitchReason value (manual/daily_loss/max_drawdown/api_failures/websocket_dead)
    detail         TEXT,               -- JSON blob of structured context, nullable
    outcome        TEXT    NOT NULL    -- flattening/flat_confirmed/flatten_failed/rearmed
);

CREATE INDEX idx_kill_switch_events_occurred_at ON kill_switch_events (occurred_at, id);
