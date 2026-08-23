"""Risk endpoint E2E (build-order step 6): the mutable/derived split and server-side clamp.

GET returns the two mutable fields plus everything derived fresh from RiskConfig; PATCH accepts
only the mutable fields, clamps the slider to [0.25, 2.0] server-side, recomputes the derived
numbers, and rejects any attempt to touch the account-level guardrails (they are simply not in the
schema, so the slider can never route around the kill-switch cap).
"""

from __future__ import annotations

from crypto_trader.api.context import ApiContext
from tests.api_helpers import WsgiClient, build_test_context


def _client_ctx() -> tuple[WsgiClient, ApiContext]:
    ctx = build_test_context(defer_approval=False)
    return WsgiClient(ctx), ctx


def test_get_risk_splits_mutable_and_derived() -> None:
    client, _ = _client_ctx()
    status, body = client.get("/api/v1/risk")
    assert status == 200
    assert set(body["mutable"]) == {"risk_slider", "overlay_enabled"}
    derived = body["derived"]
    # Equity 2000 -> the 2% tier; slider 1.0 -> 2% effective, well under the 6% daily kill cap.
    assert derived["active_tier_fraction"] == 0.02
    assert abs(derived["effective_risk_fraction"] - 0.02) < 1e-9
    assert derived["daily_kill_switch_cap_fraction"] == 0.06
    assert derived["slider_clamped"] is False
    assert "overlay" in derived


def test_patch_clamps_a_high_slider_and_recomputes_derived() -> None:
    client, ctx = _client_ctx()
    status, body = client.patch("/api/v1/risk", json_body={"risk_slider": 5.0})
    assert status == 200
    assert body["mutable"]["risk_slider"] == 2.0  # clamped to the ceiling
    # 2% tier * 2.0 slider = 4% effective, still under the 5% hard cap (no clamp bite here).
    assert abs(body["derived"]["effective_risk_fraction"] - 0.04) < 1e-9
    # The manager's slider was updated too, so future entries size with it.
    assert ctx.manager.slider == 2.0
    # A follow-up GET reflects the persisted change.
    _, after = client.get("/api/v1/risk")
    assert after["mutable"]["risk_slider"] == 2.0


def test_patch_clamps_a_low_slider() -> None:
    client, _ = _client_ctx()
    _, body = client.patch("/api/v1/risk", json_body={"risk_slider": 0.05})
    assert body["mutable"]["risk_slider"] == 0.25  # clamped to the floor


def test_patch_toggles_the_overlay() -> None:
    client, _ = _client_ctx()
    _, body = client.patch("/api/v1/risk", json_body={"overlay_enabled": True})
    assert body["mutable"]["overlay_enabled"] is True
    assert body["derived"]["overlay"]["enabled"] is True


def test_patch_rejects_an_unknown_field() -> None:
    client, _ = _client_ctx()
    status, body = client.patch("/api/v1/risk", json_body={"leverage": 10})
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"


def test_patch_cannot_touch_the_account_guardrails() -> None:
    # The kill-switch threshold and tier boundaries are not in the schema, so an attempt to set one
    # is a 400 rather than a silently-accepted override (the slider can never route around them).
    client, _ = _client_ctx()
    status, body = client.patch(
        "/api/v1/risk", json_body={"daily_kill_switch_cap_fraction": 0.5}
    )
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"


def test_patch_rejects_a_non_numeric_slider() -> None:
    client, _ = _client_ctx()
    status, body = client.patch("/api/v1/risk", json_body={"risk_slider": "high"})
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"
