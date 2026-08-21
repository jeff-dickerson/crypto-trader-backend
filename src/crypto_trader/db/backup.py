"""One-shot database backup.

Do not shell out to `cp` on a live database file: WAL mode means the on-disk
.sqlite3 file is not a consistent snapshot by itself (there is also a -wal file
with not-yet-checkpointed writes), so a raw file copy can produce a corrupt or
inconsistent backup.
This module uses SQLite's own online backup API (sqlite3.Connection.backup),
which is safe to run against a live, in-use database.

Scheduling this on a cadence (e.g. nightly cron) is out of scope for this task.
See AGENTS.md for that follow-up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from crypto_trader.db.connection import connect


def backup_database(source_db_path: str | Path, dest_backup_path: str | Path) -> Path:
    """Writes a consistent online backup of source_db_path to dest_backup_path.

    Safe to call while the bot process holds the source database open.
    dest_backup_path's parent directory is created if needed.
    Returns the resolved destination path.
    """
    source_db_path = Path(source_db_path)
    dest_backup_path = Path(dest_backup_path)
    dest_backup_path.parent.mkdir(parents=True, exist_ok=True)

    source_conn = connect(source_db_path)
    try:
        dest_conn = sqlite3.connect(str(dest_backup_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    return dest_backup_path
