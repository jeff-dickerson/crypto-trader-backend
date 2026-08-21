FROM python:3.12-slim AS base

WORKDIR /app

# System deps: sqlite3 CLI is handy for ops/debugging inside the container,
# the Python sqlite3 module itself is stdlib and needs nothing extra.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Data volume mount point: the single SQLite database file lives here.
# Single-writer invariant: only this container's bot process ever writes to it.
VOLUME ["/data"]
ENV CRYPTO_TRADER_DB_PATH=/data/crypto_trader.sqlite3

ENTRYPOINT ["python", "-m", "crypto_trader"]
