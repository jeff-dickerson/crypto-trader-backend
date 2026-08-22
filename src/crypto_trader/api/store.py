"""ApiStore: the SQLite reader/writer behind the REST API's persistence.

This is the concrete PersistenceSink (crypto_trader.paper.persistence) the paper machine writes
through, and the reader the API's endpoints query. It owns one connection to the unified SQLite
database, opened and migrated through crypto_trader.db.connection so the 002 API tables exist and
the WAL/busy-timeout invariants hold. The single-writer invariant is preserved: the bot loop and
the in-process API are one process (hosting decision 5), so every write goes through this one
connection; a future out-of-process split would need per-thread connections or a write queue
(noted in AGENTS.md).

Writers implement the PersistenceSink seam. Readers return raw column dicts; the serialization
layer (crypto_trader.api.serialization) shapes them into response JSON with as-of timestamps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from crypto_trader.api.pagination import PageRequest, encode_cursor
from crypto_trader.db.connection import connect_and_migrate
from crypto_trader.paper.persistence import PersistenceSink
from crypto_trader.paper.plan import PlanStatus, TradePlan
from crypto_trader.paper.position_manager import ClosedPaperTrade
from crypto_trader.strategy.signal import Signal


def _ms(at: datetime) -> int:
    """A UTC datetime as epoch milliseconds (the storage convention across this project)."""
    return int(at.timestamp() * 1000)


class ApiStore(PersistenceSink):
    """One connection's worth of reads and writes for the five API tables."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    @classmethod
    def open(cls, db_path: str | Path) -> ApiStore:
        """Open (and migrate) the unified database at `db_path` and return a store over it."""
        return cls(connect_and_migrate(db_path))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------- writers (the sink)

    def record_signal(self, signal: Signal, *, symbol: str, at: datetime) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO signals
                (created_at, symbol, action, bias, zone_boundary, entry_price, stop_price,
                 take_profit_price, reason, plan_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                _ms(at),
                symbol,
                signal.action.value,
                signal.bias.value if signal.bias is not None else None,
                signal.zone_boundary,
                signal.entry_price,
                signal.stop_price,
                signal.take_profit_price,
                signal.reason,
                _ms(at),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def insert_plan(
        self, plan: TradePlan, *, at: datetime, signal_id: int | None = None
    ) -> None:
        when = _ms(at)
        self._conn.execute(
            """
            INSERT INTO trade_plans
                (plan_id, created_at, updated_at, symbol, side, status, entry_price, stop_price,
                 take_profit_price, quantity, notional, equity, tier_risk_fraction,
                 slider_multiplier, effective_risk_fraction, risk_amount, slider_capped,
                 void_reason, failure_reason, signal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                status = excluded.status,
                void_reason = excluded.void_reason
            """,
            (
                plan.plan_id,
                when,
                when,
                plan.symbol,
                plan.side.value,
                plan.status.value,
                plan.entry_price,
                plan.stop_price,
                plan.take_profit_price,
                plan.quantity,
                plan.notional,
                plan.equity,
                plan.tier_risk_fraction,
                plan.slider_multiplier,
                plan.effective_risk_fraction,
                plan.risk_amount,
                1 if plan.slider_capped else 0,
                plan.void_reason,
                signal_id,
            ),
        )
        # Link the signal row to its resulting plan (nullable FK the plan doc calls for).
        if signal_id is not None:
            self._conn.execute(
                "UPDATE signals SET plan_id = ? WHERE id = ?", (plan.plan_id, signal_id)
            )
        self._conn.commit()

    def update_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        at: datetime,
        void_reason: str | None = None,
    ) -> None:
        # A FAILED status carries an exchange reason in the failure_reason column; VOID (and any
        # other reason-bearing status) uses void_reason. The two are kept semantically distinct.
        if status is PlanStatus.FAILED:
            self._conn.execute(
                """
                UPDATE trade_plans
                   SET status = ?, updated_at = ?,
                       failure_reason = COALESCE(?, failure_reason)
                 WHERE plan_id = ?
                """,
                (status.value, _ms(at), void_reason, plan_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE trade_plans
                   SET status = ?, updated_at = ?,
                       void_reason = COALESCE(?, void_reason)
                 WHERE plan_id = ?
                """,
                (status.value, _ms(at), void_reason, plan_id),
            )
        self._conn.commit()

    def record_plan_decision(
        self,
        plan_id: str,
        verdict: str,
        *,
        at: datetime,
        actor: str | None = None,
        note: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO plan_decisions (plan_id, decided_at, verdict, actor, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (plan_id, _ms(at), verdict, actor, note),
        )
        self._conn.commit()

    def record_closed_trade(self, trade: ClosedPaperTrade, *, at: datetime) -> None:
        risk_per_unit = abs(trade.entry_price - trade.initial_stop)
        r_multiple = (
            trade.realized_pnl / (risk_per_unit * trade.quantity)
            if risk_per_unit > 0 and trade.quantity > 0
            else None
        )
        self._conn.execute(
            """
            INSERT INTO closed_trades
                (symbol, side, plan_id, entry_price, exit_price, initial_stop, take_profit,
                 quantity, exit_reason, realized_pnl, r_multiple, entry_time, exit_time,
                 recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                trade.side.value,
                trade.plan_id,
                trade.entry_price,
                trade.exit_price,
                trade.initial_stop,
                trade.take_profit,
                trade.quantity,
                trade.exit_reason.value,
                trade.realized_pnl,
                r_multiple,
                _ms(trade.entry_time),
                _ms(trade.exit_time),
                _ms(at),
            ),
        )
        self._conn.commit()

    def record_kill_switch_event(
        self,
        *,
        source: str,
        outcome: str,
        at: datetime,
        detail: dict[str, object] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO kill_switch_events (occurred_at, trigger_source, detail, outcome)
            VALUES (?, ?, ?, ?)
            """,
            (_ms(at), source, json.dumps(detail) if detail else None, outcome),
        )
        self._conn.commit()

    # ------------------------------------------------------------------- readers

    def get_plan(self, plan_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM trade_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_plans_by_status(self, statuses: list[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM trade_plans WHERE status IN ({placeholders}) "
            "ORDER BY updated_at DESC, plan_id DESC",
            tuple(statuses),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_signals(self, page: PageRequest) -> tuple[list[dict], str | None]:
        return self._paged("signals", page)

    def recent_signals(self, limit: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_closed_trades(self, page: PageRequest) -> tuple[list[dict], str | None]:
        return self._paged("closed_trades", page)

    def all_closed_trades(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM closed_trades ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_kill_switch_events(self, limit: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM kill_switch_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _paged(self, table: str, page: PageRequest) -> tuple[list[dict], str | None]:
        """Keyset scan of one append-only table, newest first, id-based cursor.

        Fetches one extra row to know whether a further page exists; the returned cursor is the id
        of the last row in the page (clients pass it back as ``?cursor=``).
        """
        if page.before_id is not None:
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE id < ? ORDER BY id DESC LIMIT ?",
                (page.before_id, page.limit + 1),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                (page.limit + 1,),
            ).fetchall()
        has_more = len(rows) > page.limit
        rows = rows[: page.limit]
        next_cursor = encode_cursor(rows[-1]["id"]) if has_more and rows else None
        return [dict(r) for r in rows], next_cursor
