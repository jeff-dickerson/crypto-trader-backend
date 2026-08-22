"""Kill-switch endpoints E2E (build-order step 5).

Arm with an open paper position and watch it flatten and confirm, prove arm is idempotent, inject a
flatten failure (via the DryRun adapter's simulate_outage knob) and watch the state surface as
FLATTEN_FAILED, and drive the rearm rules (valid only from a flat/failed state). The kill switch is
the same instance the loop's auto-triggers use, so the API represents the built ops behavior rather
than re-deciding it.
"""

from __future__ import annotations

from crypto_trader.api.context import ApiContext
from tests.api_helpers import WsgiClient, build_test_context, open_long_position


def _ctx() -> ApiContext:
    return build_test_context(defer_approval=False)


def test_arm_with_an_open_position_flattens_and_confirms() -> None:
    ctx = _ctx()
    open_long_position(ctx)
    client = WsgiClient(ctx)

    status, body = client.post("/api/v1/kill-switch/arm")
    assert status == 200
    assert body["state"] == "flat_confirmed"
    assert body["is_armed"] is False
    assert body["positions_remaining"] == 0
    assert "confirmed" in body["status_line"].lower()
    # The manual engage was audited as a kill-switch event.
    assert body["recent_events"], "arming should record a kill-switch event"
    assert body["recent_events"][0]["trigger_source"] == "manual"


def test_arm_is_idempotent() -> None:
    ctx = _ctx()
    open_long_position(ctx)
    client = WsgiClient(ctx)

    client.post("/api/v1/kill-switch/arm")
    status, body = client.post("/api/v1/kill-switch/arm")  # second engage
    assert status == 200
    assert body["state"] == "flat_confirmed"
    # A second engage while already halted does not record a fresh trigger event.
    assert len(body["recent_events"]) == 1


def test_injected_flatten_failure_surfaces_flatten_failed() -> None:
    ctx = _ctx()
    open_long_position(ctx)
    client = WsgiClient(ctx)
    ctx.adapter.simulate_outage(100)  # the exchange cannot be reached for the whole flatten

    status, body = client.post("/api/v1/kill-switch/arm")
    assert status == 200
    assert body["state"] == "flatten_failed"
    assert body["is_armed"] is False
    assert body["flatten_confirmed"] is False


def test_rearm_from_armed_is_a_409_conflict() -> None:
    ctx = _ctx()
    client = WsgiClient(ctx)
    status, body = client.post("/api/v1/kill-switch/rearm")
    assert status == 409
    assert body["error"]["code"] == "KILL_SWITCH_CONFLICT"
    assert body["error"]["detail"]["current_state"] == "armed"


def test_rearm_after_confirmed_flat_resumes_trading() -> None:
    ctx = _ctx()
    open_long_position(ctx)
    client = WsgiClient(ctx)
    client.post("/api/v1/kill-switch/arm")

    status, body = client.post("/api/v1/kill-switch/rearm")
    assert status == 200
    assert body["state"] == "armed"
    assert body["is_armed"] is True
    # The rearm was audited.
    sources = [e["outcome"] for e in body["recent_events"]]
    assert "rearmed" in sources


def test_rearm_from_flatten_failed_resumes_trading() -> None:
    ctx = _ctx()
    open_long_position(ctx)
    client = WsgiClient(ctx)
    ctx.adapter.simulate_outage(100)
    client.post("/api/v1/kill-switch/arm")  # -> flatten_failed

    status, body = client.post("/api/v1/kill-switch/rearm")
    assert status == 200
    assert body["state"] == "armed"
