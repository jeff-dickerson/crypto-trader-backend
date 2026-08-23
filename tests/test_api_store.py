"""ApiStore: the five API tables exist, and every writer/reader round-trips.

These prove the persistence half of the REST API build (build-order steps 1-2): the 002 migration
creates the tables, and the PersistenceSink writers the paper loop calls are readable back through
the same store, including the signal-to-plan link, the plan status machine, and the FAILED/VOID
reason columns. No network, in-memory SQLite only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from crypto_trader.api.pagination import PageRequest
from crypto_trader.api.store import ApiStore
from crypto_trader.paper.plan import PlanStatus, TradePlan
from crypto_trader.paper.position_manager import ClosedPaperTrade, PaperExitReason
from crypto_trader.strategy.bias import Bias
from crypto_trader.strategy.position import PositionSide
from crypto_trader.strategy.signal import Signal, SignalAction

_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)

_API_TABLES = {"signals", "trade_plans", "plan_decisions", "closed_trades", "kill_switch_events"}


def _plan(plan_id: str = "BTCUSDT-000001", status: PlanStatus = PlanStatus.PROPOSED) -> TradePlan:
    return TradePlan(
        plan_id=plan_id,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=100.0,
        stop_price=95.0,
        take_profit_price=110.0,
        quantity=1.5,
        notional=150.0,
        equity=2000.0,
        tier_risk_fraction=0.02,
        slider_multiplier=1.0,
        effective_risk_fraction=0.02,
        risk_amount=40.0,
        status=status,
    )


def _signal() -> Signal:
    return Signal(
        action=SignalAction.ENTER_LONG,
        symbol="BTCUSDT",
        entry_price=100.0,
        stop_price=95.0,
        take_profit_price=110.0,
        zone_boundary=100.0,
        bias=Bias.LONG,
        reason="first-touch value-area boundary retest",
    )


def test_migration_creates_the_five_api_tables() -> None:
    store = ApiStore.open(":memory:")
    tables = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert _API_TABLES <= tables


def test_signal_and_plan_link_round_trip() -> None:
    store = ApiStore.open(":memory:")
    signal_id = store.record_signal(_signal(), symbol="BTCUSDT", at=_AT)
    store.insert_plan(_plan(), at=_AT, signal_id=signal_id)

    signals, _ = store.list_signals(PageRequest(limit=10, before_id=None))
    assert len(signals) == 1
    row = signals[0]
    assert row["action"] == "enter_long"
    assert row["bias"] == "long"
    assert row["zone_boundary"] == 100.0
    # The signal was linked to the resulting plan (the nullable FK the plan doc calls for).
    assert row["plan_id"] == "BTCUSDT-000001"

    plan = store.get_plan("BTCUSDT-000001")
    assert plan is not None
    assert plan["status"] == "proposed"
    assert plan["signal_id"] == signal_id


def test_update_plan_status_moves_through_the_machine_and_records_reasons() -> None:
    store = ApiStore.open(":memory:")
    store.insert_plan(_plan(), at=_AT)

    store.update_plan_status("BTCUSDT-000001", PlanStatus.APPROVED, at=_AT)
    assert store.get_plan("BTCUSDT-000001")["status"] == "approved"

    store.update_plan_status("BTCUSDT-000001", PlanStatus.SUBMITTED, at=_AT)
    assert store.get_plan("BTCUSDT-000001")["status"] == "submitted"

    # FAILED writes the exchange reason into failure_reason (kept distinct from void_reason).
    store.update_plan_status(
        "BTCUSDT-000001", PlanStatus.FAILED, at=_AT, void_reason="exchange unreachable"
    )
    failed = store.get_plan("BTCUSDT-000001")
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "exchange unreachable"
    assert failed["void_reason"] is None


def test_plan_decisions_and_kill_switch_events_are_appended() -> None:
    store = ApiStore.open(":memory:")
    store.insert_plan(_plan(), at=_AT)
    store.record_plan_decision("BTCUSDT-000001", "approve", at=_AT, actor="operator", note="ok")
    rows = store.connection.execute("SELECT * FROM plan_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "approve"
    assert rows[0]["actor"] == "operator"

    store.record_kill_switch_event(
        source="daily_loss", outcome="flat_confirmed", at=_AT, detail={"loss": 0.07}
    )
    events = store.list_kill_switch_events(limit=10)
    assert len(events) == 1
    assert events[0]["trigger_source"] == "daily_loss"
    assert events[0]["outcome"] == "flat_confirmed"
    assert "0.07" in events[0]["detail"]


def test_closed_trade_round_trip_computes_r_multiple() -> None:
    store = ApiStore.open(":memory:")
    trade = ClosedPaperTrade(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        plan_id="BTCUSDT-000001",
        entry_price=100.0,
        exit_price=110.0,
        initial_stop=95.0,
        take_profit=110.0,
        quantity=2.0,
        exit_reason=PaperExitReason.TAKE_PROFIT,
        realized_pnl=20.0,
        entry_time=_AT,
        exit_time=_AT,
    )
    store.record_closed_trade(trade, at=_AT)
    rows, _ = store.list_closed_trades(PageRequest(limit=10, before_id=None))
    assert len(rows) == 1
    # R = realized / (|entry-stop| * qty) = 20 / (5 * 2) = 2.0
    assert rows[0]["r_multiple"] == 2.0
    assert rows[0]["exit_reason"] == "take_profit"
