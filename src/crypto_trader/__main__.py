"""Bot process entry point.

This task only scaffolds the project and the candle-ingest layer: this entry
point runs one ingest pass over the configured symbol universe for 4H and daily
candles, using the public Bitunix source, and exits.
The future paper-loop and live-adapter tasks will replace this with a
long-running process.
"""

from __future__ import annotations

import logging

from crypto_trader.config import DEFAULT_DB_PATH, BitunixSourceConfig, Timeframe
from crypto_trader.db.connection import connect_and_migrate
from crypto_trader.ingest.pipeline import ingest_symbol
from crypto_trader.ingest.source import BitunixCandleSource

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crypto_trader")


def main() -> None:
    conn = connect_and_migrate(DEFAULT_DB_PATH)
    source = BitunixCandleSource(BitunixSourceConfig())

    for symbol in source.symbols:
        for timeframe in (Timeframe.H4, Timeframe.D1):
            result = ingest_symbol(conn, source, symbol, timeframe)
            logger.info(
                "ingested %s %s: %d accepted, %d issues",
                symbol,
                timeframe.value,
                len(result.accepted),
                len(result.issues),
            )
            for issue in result.issues:
                logger.warning("%s %s issue: %s", symbol, timeframe.value, issue)

    conn.close()


if __name__ == "__main__":
    main()
