"""Read-only candle loading round-trips what the ingest pipeline stored."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from crypto_trader.backtest.data import connect_readonly, load_candles, load_dataset
from crypto_trader.config import Timeframe
from crypto_trader.db.connection import connect_and_migrate
from crypto_trader.ingest.models import Candle
from crypto_trader.ingest.pipeline import store_candles

_T0 = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _candle(i: int, tf: Timeframe, symbol: str = "BTCUSDT") -> Candle:
    ot = _T0 + i * tf.duration
    price = 100.0 + i
    return Candle(
        symbol=symbol, timeframe=tf, open_time=ot, close_time=ot + tf.duration,
        is_closed=True, open=price, high=price + 1, low=price - 1, close=price,
        volume=10.0 + i, quote_volume=None,
    )


def test_readonly_load_round_trips_stored_candles(tmp_path: Path):
    db = tmp_path / "candles.sqlite3"
    conn = connect_and_migrate(db)
    h4 = [_candle(i, Timeframe.H4) for i in range(5)]
    d1 = [_candle(i, Timeframe.D1) for i in range(3)]
    store_candles(conn, h4, Timeframe.H4)
    store_candles(conn, d1, Timeframe.D1)
    conn.close()

    ro = connect_readonly(db)
    try:
        loaded_h4 = load_candles(ro, "BTCUSDT", Timeframe.H4)
        dataset = load_dataset(ro, ["BTCUSDT"])
    finally:
        ro.close()

    assert [c.open_time for c in loaded_h4] == [c.open_time for c in h4]
    assert [c.close for c in loaded_h4] == [c.close for c in h4]
    assert all(c.is_closed for c in loaded_h4)
    assert len(dataset["BTCUSDT"][0]) == 5
    assert len(dataset["BTCUSDT"][1]) == 3


def test_readonly_connection_cannot_write(tmp_path: Path):
    db = tmp_path / "ro.sqlite3"
    conn = connect_and_migrate(db)
    store_candles(conn, [_candle(0, Timeframe.H4)], Timeframe.H4)
    conn.close()

    ro = connect_readonly(db)
    try:
        raised = False
        try:
            ro.execute(
                "INSERT INTO candles_4h (symbol, open_time, close_time, is_closed, "
                "open, high, low, close, volume, ingested_at) "
                "VALUES ('X', 1, 2, 1, 1, 1, 1, 1, 1, 1)"
            )
            ro.commit()
        except Exception:
            raised = True
        assert raised  # the single-writer invariant: a reader cannot write
    finally:
        ro.close()
