"""Project-wide configuration and constants.

MUST-VERIFY: the daily and 4H candle boundary anchor is pinned to UTC 00:00 here as
a provisional default.
The real Bitunix candle-stamp convention has not been verified against exchange
documentation or support.
Confirm this anchor against actual exchange behavior before live trading and update
this note once verified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path


class Timeframe(str, Enum):
    """Supported candle timeframes.

    Only 4H and daily are supported by design.
    The design review flagged that 4H + daily alone cannot faithfully replay
    intrabar order (stop vs take-profit within one bar) for a future backtest
    engine.
    That is accepted as an engineering default for this project, not an oversight.
    Any future backtest engine must apply a conservative intrabar ordering rule
    (assume the worse of stop-vs-TP within a bar) rather than silently assuming a
    favorable fill.
    """

    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        if self is Timeframe.H4:
            return timedelta(hours=4)
        if self is Timeframe.D1:
            return timedelta(days=1)
        raise ValueError(f"unknown timeframe: {self}")

    @property
    def duration_ms(self) -> int:
        return int(self.duration.total_seconds() * 1000)


# Pinned candle boundary anchor: UTC 00:00.
# See module docstring MUST-VERIFY note above.
CANDLE_ANCHOR_UTC_HOUR = 0

DEFAULT_DB_PATH = Path(
    os.environ.get("CRYPTO_TRADER_DB_PATH", "./data/crypto_trader.sqlite3")
)

# Default symbol universe for development.
# The full universe is a future task; this is a small seed list so the ingest
# pipeline has something concrete to run against.
DEFAULT_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")

# Retention target: 3+ years of 4H and daily candles per symbol.
# At 4H resolution that is roughly 6 candles/day * 365 * 3 = ~6570 rows per symbol,
# and ~1095 rows per symbol at daily resolution: comfortably within SQLite's
# capacity, no partitioning needed.
RETENTION_YEARS = 3


@dataclass(frozen=True)
class BitunixSourceConfig:
    """Configuration for the public Bitunix market-data HTTP source.

    No credentials: this only touches unauthenticated public endpoints.
    """

    base_url: str = "https://fapi.bitunix.com"
    kline_path: str = "/api/v1/futures/market/kline"
    request_timeout_seconds: float = 10.0
    symbols: tuple[str, ...] = field(default_factory=lambda: DEFAULT_SYMBOLS)
