"""Approval-channel tests: the in-memory fake, the Telegram surface, and secrets hygiene.

The Telegram tests use a fake client and a fake clock, so nothing touches the network or sleeps.
They pin the security-critical behaviour: only the allowlisted chat can decide, a timeout fails
CLOSED (REJECT, never APPROVE), callbacks for the wrong plan are ignored, the card is one clear
risk number in valid markup, and the bot token is never rendered.
"""

from __future__ import annotations

from typing import Any

from crypto_trader.approval.channel import (
    ApprovalDecision,
    ApprovalVerdict,
    ChannelEvent,
    EventKind,
)
from crypto_trader.approval.memory import InMemoryApprovalChannel
from crypto_trader.approval.telegram import (
    TelegramApprovalChannel,
    TelegramClient,
    approval_keyboard,
    format_approval_card,
    parse_callback_data,
)
from crypto_trader.paper.plan import TradePlan
from crypto_trader.secrets import TelegramSecrets, load_telegram_secrets
from crypto_trader.strategy.position import PositionSide

ALLOWED = 4242
INTRUDER = 9999


def _plan(plan_id: str = "BTCUSDT-000001") -> TradePlan:
    return TradePlan(
        plan_id=plan_id,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=68_500.0,
        stop_price=67_200.0,
        take_profit_price=71_000.0,
        quantity=0.01,
        notional=685.0,
        equity=500.0,
        tier_risk_fraction=0.04,
        slider_multiplier=1.0,
        effective_risk_fraction=0.04,
        risk_amount=20.0,
    )


class FakeTelegramClient:
    """Records outbound calls and replays queued getUpdates batches. No network."""

    def __init__(self, update_batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self._batches = list(update_batches or [])

    def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append({"id": callback_query_id, "text": text})
        return {"ok": True}

    def get_updates(self, offset=None, timeout=0):
        return self._batches.pop(0) if self._batches else []


def _advancing_clock(step: float = 1.0):
    state = {"t": 0.0}

    def clock() -> float:
        t = state["t"]
        state["t"] += step
        return t

    return clock


def _callback_update(update_id: int, data: str, chat_id: int = ALLOWED) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"q{update_id}",
            "data": data,
            "message": {"chat": {"id": chat_id}},
        },
    }


def _channel(batches, **overrides) -> tuple[TelegramApprovalChannel, FakeTelegramClient]:
    client = FakeTelegramClient(batches)
    ch = TelegramApprovalChannel(
        client, ALLOWED, approval_timeout_seconds=100.0, clock=_advancing_clock(), **overrides
    )
    return ch, client


# --------------------------------------------------------------------------- in-memory channel


def test_in_memory_channel_records_and_scripts_decisions() -> None:
    ch = InMemoryApprovalChannel(default_verdict=ApprovalVerdict.REJECT)
    ch.enqueue(ApprovalDecision(plan_id="ignored", verdict=ApprovalVerdict.APPROVE))
    plan = _plan()
    first = ch.request_approval(plan)
    assert first.verdict is ApprovalVerdict.APPROVE  # scripted
    assert first.plan_id == plan.plan_id  # rebound to the real plan id
    second = ch.request_approval(plan)
    assert second.verdict is ApprovalVerdict.REJECT  # falls back to the default
    assert len(ch.requests) == 2 and len(ch.decisions) == 2


def test_in_memory_channel_records_notifications() -> None:
    ch = InMemoryApprovalChannel()
    ch.notify(ChannelEvent(kind=EventKind.ENTRY_FILLED, symbol="BTCUSDT", message="filled"))
    assert ch.events_of(EventKind.ENTRY_FILLED)


# --------------------------------------------------------------------------- formatting


def test_approval_card_shows_one_risk_number_and_valid_html() -> None:
    card = format_approval_card(_plan())
    assert "Risk: 4% tier, $20.00 on this trade" in card
    assert card.count("Risk:") == 1  # exactly one risk number
    assert "tier x" not in card  # no clamp/slider math leaks
    # Balanced simple HTML tags (no broken markup).
    assert card.count("<b>") == card.count("</b>")
    assert card.count("<code>") == card.count("</code>")


def test_approval_keyboard_has_approve_reject_modify() -> None:
    kb = approval_keyboard("P1")
    flat = [b for row in kb["inline_keyboard"] for b in row]
    actions = {b["callback_data"].split(":")[0] for b in flat}
    assert actions == {"approve", "reject", "modify"}


def test_parse_callback_data_round_trips_and_rejects_garbage() -> None:
    assert parse_callback_data("approve:P1").action == "approve"
    assert parse_callback_data("modify:P1:0.5").multiplier == 0.5
    assert parse_callback_data("nonsense") is None
    assert parse_callback_data("modify:P1") is None  # modify needs a multiplier


# --------------------------------------------------------------------------- decisions


def test_approve_button_from_allowlisted_chat_returns_approve() -> None:
    plan = _plan()
    ch, client = _channel([[_callback_update(1, f"approve:{plan.plan_id}")]])
    decision = ch.request_approval(plan)
    assert decision.verdict is ApprovalVerdict.APPROVE
    assert decision.actor == str(ALLOWED)
    assert client.answered  # the callback query was answered
    assert client.sent[0]["reply_markup"] is not None  # the card had buttons


def test_modify_button_carries_the_new_multiplier() -> None:
    plan = _plan()
    ch, _ = _channel([[_callback_update(1, f"modify:{plan.plan_id}:0.5")]])
    decision = ch.request_approval(plan)
    assert decision.verdict is ApprovalVerdict.MODIFY
    assert decision.modified_slider == 0.5


def test_callback_from_an_intruder_chat_is_ignored_then_times_out_closed() -> None:
    plan = _plan()
    ch, client = _channel([[_callback_update(1, f"approve:{plan.plan_id}", chat_id=INTRUDER)]])
    decision = ch.request_approval(plan)
    # The intruder's approve is refused; with no allowlisted decision, it fails closed.
    assert decision.verdict is ApprovalVerdict.REJECT
    assert any(a["text"] == "not authorized" for a in client.answered)


def test_callback_for_a_different_plan_is_ignored() -> None:
    plan = _plan("BTCUSDT-000002")
    ch, _ = _channel([[_callback_update(1, "approve:some-other-plan")]])
    decision = ch.request_approval(plan)
    assert decision.verdict is ApprovalVerdict.REJECT  # never matched -> timed out closed


def test_timeout_fails_closed_with_reject() -> None:
    plan = _plan()
    ch, _ = _channel([])  # no updates ever arrive
    assert ch.request_approval(plan).verdict is ApprovalVerdict.REJECT


def test_status_command_is_answered_from_the_provider_while_waiting() -> None:
    plan = _plan()
    status_calls: list[int] = []

    def status() -> str:
        status_calls.append(1)
        return "flat, equity $500"

    ch, client = _channel(
        [
            [{"update_id": 1, "message": {"chat": {"id": ALLOWED}, "text": "/status"}}],
            [_callback_update(2, f"approve:{plan.plan_id}")],
        ],
        status_provider=status,
    )
    decision = ch.request_approval(plan)
    assert decision.verdict is ApprovalVerdict.APPROVE
    assert status_calls  # the /status command was served during the wait
    assert any("flat, equity" in s["text"] for s in client.sent)


# --------------------------------------------------------------------------- secrets / start-up


def test_secrets_never_render_the_token() -> None:
    secrets = TelegramSecrets(bot_token="123:SECRET-TOKEN", allowed_chat_id=ALLOWED)
    assert "SECRET-TOKEN" not in repr(secrets)
    assert "SECRET-TOKEN" not in str(secrets)


def test_load_telegram_secrets_reads_env_or_returns_none() -> None:
    assert load_telegram_secrets({}) is None  # no token -> None (safe start)
    loaded = load_telegram_secrets(
        {
            "CRYPTO_TRADER_TELEGRAM_BOT_TOKEN": "123:abc",
            "CRYPTO_TRADER_TELEGRAM_ALLOWED_CHAT_ID": str(ALLOWED),
        }
    )
    assert loaded is not None and loaded.allowed_chat_id == ALLOWED


def test_from_env_returns_none_and_does_not_crash_when_unconfigured() -> None:
    assert TelegramApprovalChannel.from_env({}) is None


def test_from_env_refuses_a_token_without_an_allowlisted_chat() -> None:
    # A token with no valid chat id must NOT start: every action would be unauthenticated.
    ch = TelegramApprovalChannel.from_env({"CRYPTO_TRADER_TELEGRAM_BOT_TOKEN": "123:abc"})
    assert ch is None


def test_from_env_builds_a_channel_when_fully_configured() -> None:
    client = FakeTelegramClient()
    ch = TelegramApprovalChannel.from_env(
        {
            "CRYPTO_TRADER_TELEGRAM_BOT_TOKEN": "123:abc",
            "CRYPTO_TRADER_TELEGRAM_ALLOWED_CHAT_ID": str(ALLOWED),
        },
        client=client,  # type: ignore[arg-type]
    )
    assert isinstance(ch, TelegramApprovalChannel)


def test_telegram_client_repr_hides_the_token() -> None:
    client = TelegramClient("123:SUPER-SECRET")
    assert "SUPER-SECRET" not in repr(client)
