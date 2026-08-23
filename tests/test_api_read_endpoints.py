"""Read endpoints E2E through the real WSGI app and a real paper loop (build-order step 3).

A synthetic paper run populates the store through the same manager the API reads, so /dashboard,
/signals, /journal, /terminal, /risk, and /kill-switch report genuine loop-produced data. Also
covers the error envelope, cursor pagination on the append-heavy logs, and the 503 mapping (both an
ExchangeConnectionError and the outbound-call timeout wrapper). No network.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from crypto_trader.strategy.bias import Bias
from crypto_trader.strategy.signal import Signal, SignalAction
from tests.api_helpers import (
    SYMBOL,
    WsgiClient,
    build_test_context,
    make_adapter,
    make_store,
    populate_synthetic_history,
)

_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _populated_client() -> WsgiClient:
    ctx = build_test_context(defer_approval=False)
    populate_synthetic_history(ctx, years=1.5)
    return WsgiClient(ctx)


# --------------------------------------------------------------------------- health / dashboard


def test_health_is_200_and_reports_connectivity() -> None:
    client = WsgiClient(build_test_context())
    status, body = client.get("/api/v1/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["exchange_reachable"] is True
    assert body["rate_limit"]["limit_per_window"] == 10
    assert "as_of" in body


def test_dashboard_composes_summary_bias_positions() -> None:
    bias = {SYMBOL: "long"}
    ctx = build_test_context(defer_approval=False, bias_provider=lambda: bias)
    populate_synthetic_history(ctx, years=1.2)
    client = WsgiClient(ctx)
    status, body = client.get("/api/v1/dashboard")
    assert status == 200
    assert "equity" in body["summary"]
    assert body["summary"]["kill_switch_state"] == "armed"
    assert body["bias"] == bias
    assert isinstance(body["positions"], list)
    assert "as_of" in body


# --------------------------------------------------------------------------- signals / journal


def test_signals_endpoint_returns_the_logged_signals() -> None:
    client = _populated_client()
    status, body = client.get("/api/v1/signals")
    assert status == 200
    assert body["signals"], "a synthetic run should log at least one entry signal"
    first = body["signals"][0]
    assert first["action"] in {a.value for a in SignalAction}
    assert "created_at" in first


def test_journal_returns_closed_trades_and_scaling_gate_stats() -> None:
    client = _populated_client()
    status, body = client.get("/api/v1/journal")
    assert status == 200
    assert body["trades"], "a synthetic run should close at least one trade"
    stats = body["stats"]
    assert stats["closed_trade_count"] >= len(body["trades"]) >= 1
    assert stats["mean_r"] is not None
    assert 0.0 <= stats["rule_adherence_fraction"] <= 1.0
    assert "expectancy_positive" in stats


def test_terminal_bundles_positions_approvals_signals_with_timestamp() -> None:
    client = _populated_client()
    status, body = client.get("/api/v1/terminal")
    assert status == 200
    assert set(body) >= {"positions", "approvals", "signals", "as_of"}


# --------------------------------------------------------------------------- error envelope


def test_unknown_route_is_404_with_the_error_envelope() -> None:
    client = WsgiClient(build_test_context())
    status, body = client.get("/api/v1/does-not-exist")
    assert status == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert "message" in body["error"]
    assert "detail" in body["error"]


def test_wrong_method_is_404() -> None:
    client = WsgiClient(build_test_context())
    status, body = client.post("/api/v1/health")
    assert status == 404
    assert body["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- pagination


def test_signals_cursor_pagination_walks_the_whole_log_without_gaps() -> None:
    ctx = build_test_context()
    store = ctx.store
    for i in range(5):
        store.record_signal(
            Signal(action=SignalAction.ENTER_LONG, symbol=SYMBOL, bias=Bias.LONG, reason=f"s{i}"),
            symbol=SYMBOL,
            at=_AT,
        )
    client = WsgiClient(ctx)

    status, page1 = client.get("/api/v1/signals", query="limit=2")
    assert status == 200
    assert len(page1["signals"]) == 2
    assert page1["next_cursor"] is not None

    _, page2 = client.get("/api/v1/signals", query=f"limit=2&cursor={page1['next_cursor']}")
    assert len(page2["signals"]) == 2

    _, page3 = client.get("/api/v1/signals", query=f"limit=2&cursor={page2['next_cursor']}")
    assert len(page3["signals"]) == 1
    assert page3["next_cursor"] is None

    seen = [s["reason"] for s in page1["signals"] + page2["signals"] + page3["signals"]]
    assert seen == ["s4", "s3", "s2", "s1", "s0"]  # newest first, every row once


def test_pagination_rejects_a_bad_limit_and_a_bad_cursor() -> None:
    client = WsgiClient(build_test_context())
    status, body = client.get("/api/v1/signals", query="limit=9999")
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"

    status, body = client.get("/api/v1/signals", query="cursor=not-a-cursor")
    assert status == 400
    assert body["error"]["code"] == "VALIDATION"


# --------------------------------------------------------------------------- 503 mapping


def test_exchange_unreachable_maps_to_503_on_dashboard() -> None:
    adapter = make_adapter()
    ctx = build_test_context(adapter=adapter, defer_approval=False)
    client = WsgiClient(ctx)
    adapter.simulate_outage(1)  # the next guarded call (get_balance) raises
    status, body = client.get("/api/v1/dashboard")
    assert status == 503
    assert body["error"]["code"] == "EXCHANGE_UNREACHABLE"

    # /health degrades rather than 503-ing: the API process is still alive.
    adapter.simulate_outage(1)
    status, body = client.get("/api/v1/health")
    assert status == 200
    assert body["exchange_reachable"] is False


class _SlowAdapter:
    """A minimal stand-in whose get_balance blocks, to exercise the timeout wrapper."""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name):  # delegate everything else to the real adapter
        return getattr(self._real, name)

    def get_balance(self):
        time.sleep(0.5)
        return self._real.get_balance()


def test_outbound_timeout_maps_to_503() -> None:
    store = make_store()
    slow = _SlowAdapter(make_adapter())
    ctx = build_test_context(store=store, adapter=slow, defer_approval=False)
    ctx.exchange_timeout_s = 0.1  # far below the 0.5s the fake blocks for
    client = WsgiClient(ctx)
    status, body = client.get("/api/v1/dashboard")
    assert status == 503
    assert body["error"]["code"] == "EXCHANGE_UNREACHABLE"
