"""Tests for the SQLite schema, migrations, WAL configuration, and backup."""

from __future__ import annotations

from pathlib import Path

from crypto_trader.db.backup import backup_database
from crypto_trader.db.connection import connect, connect_and_migrate, migrate

EXPECTED_TABLES = {
    "candles_4h",
    "candles_1d",
    "trades",
    "signals",
    "journal",
    "config_history",
    "schema_migrations",
    # REST API tables (migration 002). `signals` is redefined from its 001 placeholder shape.
    "trade_plans",
    "plan_decisions",
    "closed_trades",
    "kill_switch_events",
}


def test_migrate_creates_all_expected_tables(tmp_path: Path):
    conn = connect_and_migrate(tmp_path / "db.sqlite3")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    conn.close()

    assert EXPECTED_TABLES <= tables


def test_migrate_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    conn = connect_and_migrate(db_path)
    first_applied = migrate(conn)
    conn.close()

    assert first_applied == []  # already applied by connect_and_migrate


def test_wal_mode_and_busy_timeout_are_configured(tmp_path: Path):
    conn = connect(tmp_path / "db.sqlite3")
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()

    assert journal_mode.lower() == "wal"
    assert busy_timeout > 0


def test_candle_tables_enforce_is_closed_check_constraint(tmp_path: Path):
    conn = connect_and_migrate(tmp_path / "db.sqlite3")
    try:
        raised = False
        try:
            conn.execute(
                """
                INSERT INTO candles_4h
                    (symbol, open_time, close_time, is_closed, open, high, low,
                     close, volume, quote_volume, ingested_at)
                VALUES ('BTCUSDT', 0, 14400000, 2, 1, 2, 0, 1, 1, 1, 0)
                """
            )
        except Exception:
            raised = True
        assert raised
    finally:
        conn.close()


def test_candle_table_primary_key_prevents_duplicate_open_time(tmp_path: Path):
    conn = connect_and_migrate(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            """
            INSERT INTO candles_4h
                (symbol, open_time, close_time, is_closed, open, high, low,
                 close, volume, quote_volume, ingested_at)
            VALUES ('BTCUSDT', 0, 14400000, 1, 1, 2, 0, 1, 1, 1, 0)
            """
        )
        conn.commit()

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO candles_4h
                (symbol, open_time, close_time, is_closed, open, high, low,
                 close, volume, quote_volume, ingested_at)
            VALUES ('BTCUSDT', 0, 14400000, 1, 999, 999, 999, 999, 999, 999, 0)
            """
        )
        conn.commit()
        assert cursor.rowcount == 0

        row = conn.execute(
            "SELECT open FROM candles_4h WHERE symbol = 'BTCUSDT' AND open_time = 0"
        ).fetchone()
        assert row[0] == 1  # original row untouched
    finally:
        conn.close()


def test_backup_database_produces_a_readable_copy_with_same_data(tmp_path: Path):
    source_path = tmp_path / "source.sqlite3"
    conn = connect_and_migrate(source_path)
    conn.execute(
        """
        INSERT INTO candles_4h
            (symbol, open_time, close_time, is_closed, open, high, low,
             close, volume, quote_volume, ingested_at)
        VALUES ('BTCUSDT', 0, 14400000, 1, 1, 2, 0, 1, 1, 1, 0)
        """
    )
    conn.commit()

    backup_path = backup_database(source_path, tmp_path / "backup" / "backup.sqlite3")
    conn.close()

    assert backup_path.exists()
    backup_conn = connect(backup_path)
    row = backup_conn.execute(
        "SELECT symbol FROM candles_4h WHERE open_time = 0"
    ).fetchone()
    backup_conn.close()

    assert row[0] == "BTCUSDT"
