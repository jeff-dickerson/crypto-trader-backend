"""Approval decision endpoint + plan state machine E2E (build-order step 4).

A real paper manager in defer-approval mode parks a PROPOSED plan (through the actual strategy
core and risk sizing), then the REST decision endpoint drives it: REJECT -> REJECTED, APPROVE ->
SUBMITTED, and APPROVE with an injected paper rejection -> FAILED and visible in /approvals. A
second decision on an already-decided plan is a 409. The submission failure is injected with the
DryRun adapter's existing simulate_outage knob (step 5's fault-injection pattern), not a new one.
"""

from __future__ import annotations

from crypto_trader.api.context import ApiContext
from tests.api_helpers import SYMBOL, WsgiClient, build_test_context
from tests.fixtures.strategy_candles import (
    TEST_STRATEGY_CONFIG,
    long_entry_window,
    rising_daily,
)


def _park_a_plan(ctx: ApiContext) -> str:
    """Drive the real manager to park one PROPOSED plan; return its plan_id from the store."""
    h4 = long_entry_window()
    d1 = rising_daily(count=12)
    last = len(h4) - 1
    ctx.adapter.on_candle(h4[last])
    ctx.manager.on_bar(SYMBOL, last, h4[last], h4, d1, [])
    proposed = ctx.store.list_plans_by_status(["proposed"])
    assert len(proposed) == 1, "the manager should have parked exactly one proposed plan"
    return proposed[0]["plan_id"]


def _ctx() -> ApiContext:
    return build_test_context(defer_approval=True, strategy_config=TEST_STRATEGY_CONFIG)


def test_proposed_plan_is_visible_in_approvals() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    status, body = client.get("/api/v1/approvals")
    assert status == 200
    ids = [p["plan_id"] for p in body["approvals"]]
    assert plan_id in ids
    card = next(p for p in body["approvals"] if p["plan_id"] == plan_id)
    assert card["status"] == "proposed"
    assert card["risk_amount"] > 0


def test_reject_moves_the_plan_to_rejected_and_submits_nothing() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    status, body = client.post(
        f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "REJECT"}
    )
    assert status == 200
    assert body["outcome"] == "rejected"
    assert ctx.store.get_plan(plan_id)["status"] == "rejected"
    assert ctx.manager.submitted_order_plan_ids == []


def test_approve_submits_the_order_in_process() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    status, body = client.post(
        f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "APPROVE", "actor": "op"}
    )
    assert status == 200
    assert body["outcome"] == "submitted"
    assert ctx.store.get_plan(plan_id)["status"] == "submitted"
    assert plan_id in ctx.manager.submitted_order_plan_ids
    # The decision was recorded to the audit trail with the actor.
    rows = ctx.store.connection.execute(
        "SELECT * FROM plan_decisions WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    assert rows[0]["verdict"] == "approve"
    assert rows[0]["actor"] == "op"


def test_approve_with_an_injected_submission_failure_marks_the_plan_failed() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    ctx.adapter.simulate_outage(1)  # the entry order placement will raise, once

    status, body = client.post(
        f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "APPROVE"}
    )
    assert status == 200
    assert body["outcome"] == "failed"
    assert body["failure_reason"]

    # FAILED stays visible in the approvals list with its failure reason (the failure path).
    _, approvals = client.get("/api/v1/approvals")
    failed = next(p for p in approvals["approvals"] if p["plan_id"] == plan_id)
    assert failed["status"] == "failed"
    assert failed["failure_reason"]
    assert plan_id not in ctx.manager.submitted_order_plan_ids


def test_deciding_an_already_decided_plan_is_409() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    client.post(f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "APPROVE"})

    status, body = client.post(
        f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "APPROVE"}
    )
    assert status == 409
    assert body["error"]["code"] == "PLAN_NOT_PROPOSABLE"
    assert body["error"]["detail"]["current_status"] == "submitted"


def test_decision_on_a_missing_plan_is_404() -> None:
    ctx = _ctx()
    client = WsgiClient(ctx)
    status, body = client.post(
        "/api/v1/approvals/NOPE-000001/decision", json_body={"decision": "APPROVE"}
    )
    assert status == 404
    assert body["error"]["code"] == "PLAN_NOT_FOUND"


def test_an_invalid_decision_value_is_400() -> None:
    ctx = _ctx()
    plan_id = _park_a_plan(ctx)
    client = WsgiClient(ctx)
    status, body = client.post(
        f"/api/v1/approvals/{plan_id}/decision", json_body={"decision": "MAYBE"}
    )
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"
