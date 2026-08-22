"""Shared helpers for the REST API tests: an in-process WSGI client and setup builders.

The client drives the REAL WSGI application (crypto_trader.api.app.make_wsgi_app) with a synthetic
environ, so every endpoint test goes through the actual router, JSON rendering, and error envelope,
deterministically and with no sockets and no network (conduct rule 6, and the no-network test rule).
The builders wire a real paper machine over an in-memory SQLite store so the read endpoints have
genuine loop-produced data (the "E2E through the real paper loop" requirement).
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from crypto_trader.api.app import make_wsgi_app
from crypto_trader.api.build import build_context
from crypto_trader.api.context import ApiContext
from crypto_trader.api.store import ApiStore
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.config import Timeframe
from crypto_trader.exchange.dryrun import DryRunConfig, DryRunExchangeAdapter
from crypto_trader.exchange.types import SymbolRule
from crypto_trader.ingest.models import Candle
from crypto_trader.paper.loop import PaperTrader
from crypto_trader.paper.risk import size_trade_plan
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG
from crypto_trader.strategy.position import PositionSide, PositionStatus

SYMBOL = "BTCUSDT"
RULE = SymbolRule(symbol=SYMBOL, base_precision=4, quote_precision=1, min_trade_volume=0.0001)
FIXED_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)


class _Sig:
    """A minimal signal view for the white-box open-a-position helper."""

    entry_price = 100.0
    stop_price = 95.0
    take_profit_price = 110.0
    zone_boundary = 100.0


def _candle(index: int, o: float, h: float, low: float, c: float) -> Candle:
    open_time = _BASE + index * Timeframe.H4.duration
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.H4,
        open_time=open_time,
        close_time=open_time + Timeframe.H4.duration,
        is_closed=True,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=100.0,
        quote_volume=None,
    )


def open_long_position(ctx: ApiContext) -> None:
    """Drive one long position to OPEN on the adapter, through the real submit/fill path."""
    manager = ctx.manager
    adapter = ctx.adapter
    ms = manager.managed(SYMBOL)
    plan = size_trade_plan(
        symbol=SYMBOL,
        side=PositionSide.LONG,
        entry_price=100.0,
        stop_price=95.0,
        take_profit_price=110.0,
        equity=adapter.get_balance().total,
        symbol_rule=RULE,
    )
    adapter.on_candle(_candle(0, 100.0, 101.0, 99.6, 100.0))  # set the clock before placing
    manager._submit_entry(ms, plan.approved(), PositionSide.LONG, _Sig())
    fill_bar = _candle(1, 100.0, 101.0, 99.0, 100.0)
    events = adapter.on_candle(fill_bar)
    manager.on_bar(SYMBOL, 1, fill_bar, [fill_bar], [], events)
    assert ms.status is PositionStatus.OPEN


class WsgiClient:
    """Drives the real WSGI app with a synthetic environ; returns (status_int, parsed_body)."""

    def __init__(self, ctx: ApiContext) -> None:
        self._app = make_wsgi_app(ctx)

    def request(
        self, method: str, path: str, *, query: str = "", json_body: object | None = None
    ) -> tuple[int, object]:
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        captured: dict[str, int] = {}

        def start_response(status: str, _headers: list) -> None:
            captured["status"] = int(status.split()[0])

        raw = b"".join(self._app(environ, start_response))
        parsed = json.loads(raw) if raw else None
        return captured["status"], parsed

    def get(self, path: str, *, query: str = "") -> tuple[int, object]:
        return self.request("GET", path, query=query)

    def post(self, path: str, *, json_body: object | None = None) -> tuple[int, object]:
        return self.request("POST", path, json_body=json_body)

    def patch(self, path: str, *, json_body: object | None = None) -> tuple[int, object]:
        return self.request("PATCH", path, json_body=json_body)


def make_store() -> ApiStore:
    """A fresh in-memory store with the migrations applied (no files, no network)."""
    return ApiStore.open(":memory:")


def make_adapter(**config: object) -> DryRunExchangeAdapter:
    """A DryRun adapter with rules for the test symbols."""
    return DryRunExchangeAdapter(
        DryRunConfig(symbol_rules={SYMBOL: RULE, "ETHUSDT": RULE}, **config)
    )


def build_test_context(
    *,
    store: ApiStore | None = None,
    adapter: DryRunExchangeAdapter | None = None,
    approval: InMemoryApprovalChannel | None = None,
    defer_approval: bool = True,
    strategy_config=DEFAULT_STRATEGY_CONFIG,
    bias_provider=None,
) -> ApiContext:
    """An ApiContext over a real paper machine, with a fixed clock and a no-op flatten sleep."""
    store = store or make_store()
    adapter = adapter or make_adapter()
    approval = approval or InMemoryApprovalChannel()
    return build_context(
        store,
        adapter=adapter,
        approval=approval,
        strategy_config=strategy_config,
        defer_approval=defer_approval,
        bias_provider=bias_provider,
        clock=lambda: FIXED_NOW,
        monitor_sleep_fn=lambda _s: None,
    )


def populate_synthetic_history(ctx: ApiContext, *, years: float = 1.2) -> None:
    """Run a synthetic paper loop through `ctx`'s manager so the store has real signals/trades.

    Uses the manager already inside the context (which writes to the same store), so the read
    endpoints observe exactly what the loop produced.
    """
    from crypto_trader.backtest.synthetic import generate_dataset

    dataset = generate_dataset([SYMBOL, "ETHUSDT"], years=years)
    trader = PaperTrader(
        ctx.adapter,
        ctx.manager,
        strategy_config=DEFAULT_STRATEGY_CONFIG,
        kill_switch_monitor=ctx.monitor,
    )
    trader.run(dataset)
