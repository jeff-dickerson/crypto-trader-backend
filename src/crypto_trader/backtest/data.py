"""Read-only candle loading from the project SQLite database for the backtest.

The single-writer invariant (AGENTS.md) means only the bot process writes the database;
every reader, this backtest included, opens it READ-ONLY. The connection here is opened with
SQLite's `mode=ro` URI so the backtest structurally cannot write a candle row, and it loads
only closed candles (is_closed = 1), reconstructing the validated `Candle` shape the strategy
core consumes. Timestamps in storage are epoch milliseconds UTC; they are rebuilt as
timezone-aware UTC datetimes here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from crypto_trader.config import Timeframe
from crypto_trader.ingest.models import Candle

_TABLE_BY_TIMEFRAME = {
    Timeframe.H4: "candles_4h",
    Timeframe.D1: "candles_1d",
}


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the database read-only, honouring the single-writer invariant."""
    uri = f"file:{Path(db_path)}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def load_candles(
    conn: sqlite3.Connection, symbol: str, timeframe: Timeframe
) -> list[Candle]:
    """Load all closed candles for one symbol/timeframe, ascending by open_time."""
    table = _TABLE_BY_TIMEFRAME[timeframe]
    rows = conn.execute(
        f"""
        SELECT open_time, close_time, is_closed, open, high, low, close, volume,
               quote_volume
        FROM {table}
        WHERE symbol = ? AND is_closed = 1
        ORDER BY open_time ASC
        """,
        (symbol,),
    ).fetchall()
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=_to_utc(r[0]),
            close_time=_to_utc(r[1]),
            is_closed=bool(r[2]),
            open=r[3],
            high=r[4],
            low=r[5],
            close=r[6],
            volume=r[7],
            quote_volume=r[8],
        )
        for r in rows
    ]


def load_dataset(
    conn: sqlite3.Connection, symbols: list[str]
) -> dict[str, tuple[list[Candle], list[Candle]]]:
    """Load (4H, daily) candle lists for each symbol into the engine's dataset shape."""
    dataset: dict[str, tuple[list[Candle], list[Candle]]] = {}
    for symbol in symbols:
        h4 = load_candles(conn, symbol, Timeframe.H4)
        d1 = load_candles(conn, symbol, Timeframe.D1)
        dataset[symbol] = (h4, d1)
    return dataset
