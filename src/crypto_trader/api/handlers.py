"""The twelve route handlers, one per row of the plan's endpoint inventory.

Each handler is a pure-ish function of (ApiContext, Request) -> Response: it reads live bot state
and the store, and drives the manager/monitor for the two mutating surfaces (approval decisions,
kill switch, risk PATCH). Outbound exchange reads go through `ctx.exchange_read` (the timeout
wrapper) so a hung or unreachable venue becomes a 503 rather than a blocked handler (the in-process
invariant). Handlers raise ApiError for every non-2xx; the router renders the envelope.
"""

from __future__ import annotations

from crypto_trader.api.context import ApiContext
from crypto_trader.api.errors import ApiError, ErrorCode
from crypto_trader.api.http import Request, Response
from crypto_trader.api.pagination import parse_page_request
from crypto_trader.api.serialization import (
    closed_trade_to_json,
    iso_now,
    kill_switch_event_to_json,
    plan_to_json,
    position_to_json,
    signal_to_json,
)
from crypto_trader.approval.channel import ApprovalVerdict
from crypto_trader.paper.plan import PlanStatus

# Statuses the approvals/terminal surfaces show: awaiting a decision, plus the ones that need the
# operator's attention (auto-voided with a reason, or a submission that failed).
_APPROVAL_STATUSES = [PlanStatus.PROPOSED.value, PlanStatus.VOID.value, PlanStatus.FAILED.value]

# The planned (not unplanned) exit reasons, for the rule-adherence metric (PRD 5.1).
_PLANNED_EXITS = {"stop", "trailing_stop", "take_profit", "momentum_shift"}


# ------------------------------------------------------------------------------- health

def handle_health(ctx: ApiContext, _req: Request) -> Response:
    """Liveness plus exchange/websocket connectivity and rate-limit usage.

    Always 200 (the API process is alive); exchange reachability is a field, not the HTTP status,
    so the always-visible status bar can poll this cheaply.
    """
    reachable = True
    try:
        ctx.exchange_read(ctx.adapter.get_balance)  # a real round trip: the reachability probe
    except ApiError:
        reachable = False
    # rate_limit_status is a local budget report (not a venue round trip), so it is read directly.
    rate = ctx.adapter.rate_limit_status()
    failures = ctx.adapter.consecutive_api_failures()
    return Response(
        200,
        {
            "status": "ok",
            "as_of": iso_now(ctx.now()),
            "exchange_reachable": reachable,
            "consecutive_api_failures": failures,
            "websocket": {"connected": None, "note": "paper adapter has no websocket stream"},
            "rate_limit": None
            if rate is None
            else {
                "scope": rate.scope,
                "limit_per_window": rate.limit_per_window,
                "used_in_window": rate.used_in_window,
                "remaining": rate.remaining,
                "window_seconds": rate.window_seconds,
            },
        },
    )


# ------------------------------------------------------------------------------- dashboard

def handle_dashboard(ctx: ApiContext, _req: Request) -> Response:
    """Summary + bias + positions, composed from adapter equity, the manager, and the bias gate."""
    balance = ctx.exchange_read(ctx.adapter.get_balance)
    positions = ctx.exchange_read(ctx.adapter.get_positions)
    equity = balance.total
    eqt = ctx.equity_tracker
    day_start = eqt.day_start_equity if eqt.day_start_equity is not None else equity
    peak = eqt.peak_equity if eqt.peak_equity is not None else equity
    daily_pnl = equity - day_start
    return Response(
        200,
        {
            "as_of": iso_now(ctx.now()),
            "summary": {
                "equity": equity,
                "daily_pnl": daily_pnl,
                "daily_pnl_fraction": (daily_pnl / day_start) if day_start else 0.0,
                "drawdown_fraction": ((peak - equity) / peak) if peak else 0.0,
                "open_position_count": len(positions),
                "kill_switch_state": ctx.monitor.state().value,
            },
            "bias": ctx.current_bias(),
            "positions": [position_to_json(p) for p in positions],
        },
    )


# ------------------------------------------------------------------------------- approvals

def handle_approvals(ctx: ApiContext, _req: Request) -> Response:
    """Plans awaiting a decision, plus auto-voided (with reason) and failed plans."""
    rows = ctx.store.list_plans_by_status(_APPROVAL_STATUSES)
    return Response(
        200,
        {"as_of": iso_now(ctx.now()), "approvals": [plan_to_json(r) for r in rows]},
    )


def handle_approval_decision(ctx: ApiContext, req: Request) -> Response:
    """Record APPROVE/REJECT on a PROPOSED plan; on APPROVE submit in-process (observable)."""
    plan_id = req.path_params["id"]
    row = ctx.store.get_plan(plan_id)
    if row is None:
        raise ApiError(ErrorCode.PLAN_NOT_FOUND, f"no plan {plan_id!r}")
    if row["status"] != PlanStatus.PROPOSED.value:
        raise ApiError(
            ErrorCode.PLAN_NOT_PROPOSABLE,
            f"plan {plan_id} is no longer open for decision",
            detail={"current_status": row["status"], "void_reason": row["void_reason"]},
        )

    body = req.json_body or {}
    raw = body.get("decision")
    if not isinstance(raw, str) or raw.upper() not in ("APPROVE", "REJECT"):
        raise ApiError(
            ErrorCode.VALIDATION,
            "decision must be 'APPROVE' or 'REJECT'",
            detail={"got": raw},
        )
    verdict = ApprovalVerdict.APPROVE if raw.upper() == "APPROVE" else ApprovalVerdict.REJECT
    actor = body.get("actor")
    note = body.get("note")

    try:
        outcome = ctx.manager.decide_parked_plan(
            plan_id, verdict, actor=actor, note=note, now=ctx.now()
        )
    except KeyError:
        # PROPOSED in the store but not awaiting a live decision through this manager instance.
        raise ApiError(
            ErrorCode.PLAN_NOT_PROPOSABLE,
            f"plan {plan_id} is not awaiting a live decision",
            detail={"current_status": row["status"]},
        ) from None

    updated = ctx.store.get_plan(plan_id)
    return Response(
        200,
        {
            "as_of": iso_now(ctx.now()),
            "plan": plan_to_json(updated) if updated is not None else None,
            "outcome": outcome.status.value,
            "failure_reason": outcome.failure_reason,
        },
    )


# ------------------------------------------------------------------------------- signals

def handle_signals(ctx: ApiContext, req: Request) -> Response:
    """The signal log, cursor-paginated (append-heavy history)."""
    page = parse_page_request(req.query.get("limit"), req.query.get("cursor"))
    rows, next_cursor = ctx.store.list_signals(page)
    return Response(
        200,
        {
            "as_of": iso_now(ctx.now()),
            "signals": [signal_to_json(r) for r in rows],
            "next_cursor": next_cursor,
        },
    )


# ------------------------------------------------------------------------------- journal

def handle_journal(ctx: ApiContext, req: Request) -> Response:
    """Closed trades (cursor-paginated) plus the scaling-gate stats block (PRD 5.1)."""
    page = parse_page_request(req.query.get("limit"), req.query.get("cursor"))
    rows, next_cursor = ctx.store.list_closed_trades(page)
    stats = _journal_stats(ctx.store.all_closed_trades())
    return Response(
        200,
        {
            "as_of": iso_now(ctx.now()),
            "trades": [closed_trade_to_json(r) for r in rows],
            "next_cursor": next_cursor,
            "stats": stats,
        },
    )


def _journal_stats(rows: list[dict]) -> dict:
    """Scaling-gate metrics (PRD 5.1): expectancy, the 20-trade floor, drawdown, rule adherence."""
    count = len(rows)
    if count == 0:
        return {
            "closed_trade_count": 0,
            "mean_r": None,
            "expectancy_positive": False,
            "win_rate": None,
            "total_realized_pnl": 0.0,
            "max_drawdown_r": 0.0,
            "rule_adherence_fraction": None,
            "meets_min_trade_floor": False,
        }
    r_values = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    mean_r = sum(r_values) / len(r_values) if r_values else None
    wins = sum(1 for r in rows if r["realized_pnl"] > 0)
    total_pnl = sum(r["realized_pnl"] for r in rows)
    adherent = sum(1 for r in rows if r["exit_reason"] in _PLANNED_EXITS)
    # Max drawdown on the cumulative-R equity curve (peak-to-trough), the size-independent
    # analogue of the PRD 5.1 max-drawdown bar for a risk-normalised paper journal.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rows:
        cumulative += r["r_multiple"] if r["r_multiple"] is not None else 0.0
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return {
        "closed_trade_count": count,
        "mean_r": mean_r,
        "expectancy_positive": mean_r is not None and mean_r > 0,
        "win_rate": wins / count,
        "total_realized_pnl": total_pnl,
        "max_drawdown_r": max_dd,
        "rule_adherence_fraction": adherent / count,
        "meets_min_trade_floor": count >= 20,
    }


# ------------------------------------------------------------------------------- terminal

def handle_terminal(ctx: ApiContext, _req: Request) -> Response:
    """Positions + approvals + recent signals bundle, with an as-of timestamp (the TUI screen)."""
    positions = ctx.exchange_read(ctx.adapter.get_positions)
    approvals = ctx.store.list_plans_by_status(_APPROVAL_STATUSES)
    signals = ctx.store.recent_signals(limit=25)
    return Response(
        200,
        {
            "as_of": iso_now(ctx.now()),
            "positions": [position_to_json(p) for p in positions],
            "approvals": [plan_to_json(r) for r in approvals],
            "signals": [signal_to_json(r) for r in signals],
        },
    )


# ------------------------------------------------------------------------------- risk

def _risk_body(ctx: ApiContext) -> dict:
    """The GET /risk representation: mutable fields plus everything derived from RiskConfig."""
    equity = ctx.exchange_read(ctx.adapter.get_balance).total
    rc = ctx.risk_config
    rs = ctx.risk_state
    tier = rc.tier_fraction_for_equity(equity)
    effective, capped = rc.effective_risk_fraction(equity, rs.slider)
    open_risk_amount = ctx.manager.open_risk_amount()
    if rs.cycle_start_equity is None:
        rs.cycle_start_equity = equity  # anchor the overlay on first observation (PRD 5.2)
    multiple = equity / rs.cycle_start_equity if rs.cycle_start_equity else 1.0
    return {
        "as_of": iso_now(ctx.now()),
        "mutable": {"risk_slider": rs.slider, "overlay_enabled": rs.overlay_enabled},
        "derived": {
            "equity": equity,
            "active_tier_fraction": tier,
            "effective_risk_fraction": effective,
            "effective_risk_pct": effective * 100.0,
            "slider_clamped": capped,
            "daily_kill_switch_cap_fraction": rc.kill_switch_daily_fraction,
            "max_effective_risk_fraction": rc.max_effective_risk_fraction,
            "open_risk_amount": open_risk_amount,
            "open_risk_fraction": (open_risk_amount / equity) if equity else 0.0,
            "overlay": {
                "enabled": rs.overlay_enabled,
                "cycle_start_equity": rs.cycle_start_equity,
                "current_multiple": multiple,
            },
        },
    }


def handle_risk_get(ctx: ApiContext, _req: Request) -> Response:
    return Response(200, _risk_body(ctx))


def handle_risk_patch(ctx: ApiContext, req: Request) -> Response:
    """Update the mutable fields only. The slider is clamped to [0.25, 2.0] server-side.

    The untouchable guardrails (kill-switch thresholds, tier boundaries) are simply not in the
    accepted schema, so the slider can never route around the account-level cap: any unknown or
    read-only key is a 400 rather than a silently-ignored field.
    """
    body = req.json_body or {}
    allowed = {"risk_slider", "overlay_enabled"}
    unknown = set(body) - allowed
    if unknown:
        raise ApiError(
            ErrorCode.VALIDATION,
            f"unknown or read-only fields: {sorted(unknown)}",
            detail={"allowed": sorted(allowed)},
        )
    rs = ctx.risk_state
    if "risk_slider" in body:
        raw = body["risk_slider"]
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ApiError(
                ErrorCode.VALIDATION, "risk_slider must be a number", detail={"got": raw}
            )
        clamped = ctx.risk_config.clamp_slider(float(raw))
        rs.slider = clamped
        ctx.manager.slider = clamped  # future entries size with the new slider
    if "overlay_enabled" in body:
        raw = body["overlay_enabled"]
        if not isinstance(raw, bool):
            raise ApiError(
                ErrorCode.VALIDATION, "overlay_enabled must be a boolean", detail={"got": raw}
            )
        rs.overlay_enabled = raw
    return Response(200, _risk_body(ctx))


# ------------------------------------------------------------------------------- kill switch

def _kill_switch_body(ctx: ApiContext) -> dict:
    """The GET /kill-switch representation: derived state, per-position detail, recent events."""
    reachable = True
    positions: list = []
    try:
        positions = ctx.exchange_read(ctx.adapter.get_positions)
    except ApiError:
        reachable = False
    ks = ctx.kill_switch
    last = ks.last_event
    events = ctx.store.list_kill_switch_events(limit=20)
    return {
        "as_of": iso_now(ctx.now()),
        "state": ctx.monitor.state().value,
        "status_line": ctx.monitor.status_line(),
        "is_armed": ks.is_armed,
        "flatten_confirmed": ks.flatten_confirmed,
        "flatten_target_count": ctx.monitor.flatten_target_count(),
        "positions_remaining": len(positions),
        "exchange_reachable": reachable,
        "last_reason": last.reason.value if last is not None else None,
        "positions": [position_to_json(p) for p in positions],
        "recent_events": [kill_switch_event_to_json(r) for r in events],
    }


def handle_kill_switch_get(ctx: ApiContext, _req: Request) -> Response:
    return Response(200, _kill_switch_body(ctx))


def handle_kill_switch_arm(ctx: ApiContext, _req: Request) -> Response:
    """Engage the kill switch (idempotent). Returns immediately with the resulting state."""
    ctx.monitor.manual_trigger(now=ctx.now())
    return Response(200, _kill_switch_body(ctx))


def handle_kill_switch_rearm(ctx: ApiContext, _req: Request) -> Response:
    """Resume trading. Valid only from FLAT_CONFIRMED or FLATTEN_FAILED, else a 409 conflict."""
    try:
        ctx.monitor.rearm(now=ctx.now())
    except ValueError as exc:
        raise ApiError(
            ErrorCode.KILL_SWITCH_CONFLICT,
            str(exc),
            detail={"current_state": ctx.monitor.state().value},
        ) from None
    return Response(200, _kill_switch_body(ctx))
