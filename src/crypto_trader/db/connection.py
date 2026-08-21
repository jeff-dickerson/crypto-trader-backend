"""SQLite connection setup and schema migrations.

Every connection opened through connect() gets WAL mode and a busy_timeout, per
the single-writer invariant documented in crypto_trader.db.__init__: only the bot
process writes, WAL lets any future read-only reader coexist without blocking or
being blocked by the writer for long, and busy_timeout means a brief lock
contention waits instead of raising.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Milliseconds SQLite will wait on a locked database before raising
# sqlite3.OperationalError.
DEFAULT_BUSY_TIMEOUT_MS = 5000


def connect(
    db_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Opens a SQLite connection configured for this project's invariants.

    Creates the parent directory if needed, so callers do not need to remember to
    do it themselves.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Applies every migration in migrations/ that has not yet been applied.

    Migrations are plain, idempotent-by-convention .sql files, numbered and
    applied in filename order; applied filenames are tracked in
    schema_migrations so re-running this is a no-op once caught up.
    Returns the list of migration filenames that were newly applied.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  INTEGER NOT NULL
        )
        """
    )
    applied = {
        row[0] for row in conn.execute("SELECT filename FROM schema_migrations")
    }

    newly_applied: list[str] = []
    for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration_file.name in applied:
            continue
        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) "
            "VALUES (?, strftime('%s','now') * 1000)",
            (migration_file.name,),
        )
        conn.commit()
        newly_applied.append(migration_file.name)

    return newly_applied


def connect_and_migrate(
    db_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Convenience: connect() followed by migrate()."""
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    migrate(conn)
    return conn
