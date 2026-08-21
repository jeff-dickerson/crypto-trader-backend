"""SQLite storage layer.

Single-writer invariant: only the bot process ever opens this database for
writes.
Any future reader (CLI tools, a later read-only UI) must open the database
read-only or treat it as such: never write from a second process.
WAL mode plus a busy_timeout is configured on every connection in
crypto_trader.db.connection so concurrent readers do not block the writer, and a
brief writer contention does not raise instead of waiting.
"""
