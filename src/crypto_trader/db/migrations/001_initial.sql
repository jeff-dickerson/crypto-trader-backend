-- Initial schema: candles, and placeholder tables for future tasks.
--
-- Single-writer invariant: only the bot process writes this database.
-- Any future reader opens it read-only.
--
-- No-lookahead: open_time, close_time, and is_closed are stored explicitly on
-- every candle row so a consumer can never accidentally read a partial bar.
-- The ingest pipeline (crypto_trader.ingest.validate) never writes a row with
-- is_closed = 0: a still-forming candle is rejected before it reaches storage.
-- The column exists anyway, as a second line of defense and so the invariant is
-- visible in the schema itself, not just in application code.
--
-- Candle anchor: the daily/4H boundary is UTC 00:00, pinned as a provisional
-- default. See crypto_trader.config module docstring for the MUST-VERIFY note
-- against real Bitunix candle-stamp conventions.
--
-- Data plan: 4H and daily candles only, by design (see crypto_trader.config.Timeframe
-- docstring). No finer-grained tables exist in this schema; a future backtest engine
-- must apply a conservative intrabar ordering rule rather than assume favorable fills.

CREATE TABLE IF NOT EXISTS candles_4h (
    symbol        TEXT    NOT NULL,
    open_time     INTEGER NOT NULL,  -- epoch ms, UTC, 4H-boundary aligned
    close_time    INTEGER NOT NULL,  -- epoch ms, UTC; open_time + 4h
    is_closed     INTEGER NOT NULL CHECK (is_closed IN (0, 1)),
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        REAL    NOT NULL,
    quote_volume  REAL,
    ingested_at   INTEGER NOT NULL,  -- epoch ms, UTC, when this row was written
    PRIMARY KEY (symbol, open_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_4h_symbol_open_time
    ON candles_4h (symbol, open_time);

CREATE TABLE IF NOT EXISTS candles_1d (
    symbol        TEXT    NOT NULL,
    open_time     INTEGER NOT NULL,  -- epoch ms, UTC, daily-boundary aligned
    close_time    INTEGER NOT NULL,  -- epoch ms, UTC; open_time + 1d
    is_closed     INTEGER NOT NULL CHECK (is_closed IN (0, 1)),
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        REAL    NOT NULL,
    quote_volume  REAL,
    ingested_at   INTEGER NOT NULL,  -- epoch ms, UTC, when this row was written
    PRIMARY KEY (symbol, open_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_1d_symbol_open_time
    ON candles_1d (symbol, open_time);

-- Placeholder tables below: schema only, populated by future tasks.

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT    NOT NULL,
    opened_at         INTEGER NOT NULL,
    closed_at         INTEGER,
    side              TEXT    NOT NULL CHECK (side IN ('long', 'short')),
    entry_price       REAL,
    exit_price        REAL,
    quantity          REAL,
    stop_price        REAL,
    take_profit_price REAL,
    status            TEXT    NOT NULL DEFAULT 'open',
    signal_id         INTEGER,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    generated_at INTEGER NOT NULL,
    timeframe    TEXT    NOT NULL,
    direction    TEXT,
    payload_json TEXT,
    approved     INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1))
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    category   TEXT    NOT NULL,
    message    TEXT    NOT NULL,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS config_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_by TEXT
);
