"""Shape store rows and live objects into response JSON, with as-of timestamps.

Timestamps are stored as epoch milliseconds (the project convention) and rendered here as ISO 8601
UTC strings, which is what a polling client wants for display; the plan requires every payload to
carry as-of timestamps, so the composition endpoints add a top-level `as_of` and every row keeps
its own event time. Keeping this mapping in one module means the twelve handlers stay thin and the
wire shapes stay consistent (the frontend types the plan defers to step 7 are derived from these).
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.exchange.types import Position


def iso(ms: int | None) -> str | None:
    """Epoch milliseconds to an ISO 8601 UTC string, or None passthrough."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def iso_now(now: datetime) -> str:
    """A datetime as an ISO 8601 UTC string (the `as_of` stamp)."""
    return now.astimezone(timezone.utc).isoformat()


def plan_to_json(row: dict) -> dict:
    """A `trade_plans` row as response JSON (the approval card's fields)."""
    return {
        "plan_id": row["plan_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "status": row["status"],
        "entry_price": row["entry_price"],
        "stop_price": row["stop_price"],
        "take_profit_price": row["take_profit_price"],
        "quantity": row["quantity"],
        "notional": row["notional"],
        "equity": row["equity"],
        "risk_amount": row["risk_amount"],
        "tier_risk_fraction": row["tier_risk_fraction"],
        "effective_risk_fraction": row["effective_risk_fraction"],
        "slider_multiplier": row["slider_multiplier"],
        "slider_capped": bool(row["slider_capped"]),
        "void_reason": row["void_reason"],
        "failure_reason": row["failure_reason"],
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


def signal_to_json(row: dict) -> dict:
    """A `signals` row as response JSON (the signal log, including zone data)."""
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "action": row["action"],
        "bias": row["bias"],
        "zone_boundary": row["zone_boundary"],
        "entry_price": row["entry_price"],
        "stop_price": row["stop_price"],
        "take_profit_price": row["take_profit_price"],
        "reason": row["reason"],
        "plan_id": row["plan_id"],
        "created_at": iso(row["created_at"]),
    }


def closed_trade_to_json(row: dict) -> dict:
    """A `closed_trades` row as response JSON (a journal entry)."""
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "plan_id": row["plan_id"],
        "entry_price": row["entry_price"],
        "exit_price": row["exit_price"],
        "initial_stop": row["initial_stop"],
        "take_profit": row["take_profit"],
        "quantity": row["quantity"],
        "exit_reason": row["exit_reason"],
        "realized_pnl": row["realized_pnl"],
        "r_multiple": row["r_multiple"],
        "entry_time": iso(row["entry_time"]),
        "exit_time": iso(row["exit_time"]),
    }


def kill_switch_event_to_json(row: dict) -> dict:
    """A `kill_switch_events` row as response JSON."""
    return {
        "id": row["id"],
        "occurred_at": iso(row["occurred_at"]),
        "trigger_source": row["trigger_source"],
        "outcome": row["outcome"],
        "detail": row["detail"],
    }


def position_to_json(position: Position) -> dict:
    """A live exchange Position as response JSON."""
    return {
        "symbol": position.symbol,
        "side": position.side.value,
        "quantity": position.quantity,
        "entry_price": position.entry_price,
        "unrealized_pnl": position.unrealized_pnl,
        "position_mode": position.position_mode.value,
    }
