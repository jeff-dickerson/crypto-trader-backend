"""Shared pytest fixtures.

No fixture here touches the network: the Bitunix HTTP source is never
instantiated in tests, only FixtureCandleSource and synthetic data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crypto_trader.db.connection import connect_and_migrate


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_and_migrate(tmp_path / "test.sqlite3")
    yield conn
    conn.close()
