"""`python -m crypto_trader.backtest` entry point."""

from __future__ import annotations

import sys

from crypto_trader.backtest.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
