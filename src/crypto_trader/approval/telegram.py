"""TelegramApprovalChannel: the v1 human control surface (captain decision 3, PRD 8).

This is real, complete Telegram Bot API code, not a stub. It speaks the Bot API directly over
HTTPS with `requests` (already a project dependency, the same choice BitunixCandleSource makes),
so it needs no external Telegram library to install and its transport is a single small object
that tests replace with a mock. It never touches the network in tests.

Security posture (PRD 8: approval buttons are authenticated actions, not open to any chat that
finds the bot):
- The bot token is loaded once from the environment (crypto_trader.secrets), lives only inside
  the transport, and is never logged (redacted repr) nor written to the database (PRD 9.4).
- Exactly one chat_id is allowlisted. Every callback and command from any other chat is ignored
  (a callback is answered "not authorized"), so no unknown chat can approve a trade.
- request_approval FAILS CLOSED: on a timeout, a transport error, or an unauthenticated actor it
  returns REJECT, never APPROVE, because an approval bypass is a Gate 2 critical failure.

Safe start-up: when no bot token is configured (the expected state in this build and CI), the
channel is not constructed and `from_env` returns None with a clear message. The caller prints
the message and runs against the in-memory channel instead of crashing.

Command vocabulary: inline Approve / Reject / Modify buttons on every trade plan, plus /status
and /profit text commands answered from injected providers. A fuller conversational modify and
the rest of PRD 8's vocabulary (/risk, /daily, /forceexit) build on this same seam; /daily and
the kill switch are Build Order step 5.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from crypto_trader.approval.channel import (
    ApprovalChannel,
    ApprovalDecision,
    ApprovalVerdict,
    ChannelEvent,
)
from crypto_trader.paper.plan import TradePlan
from crypto_trader.secrets import TelegramSecrets, load_telegram_secrets

logger = logging.getLogger("crypto_trader.approval.telegram")

# The Modify button re-proposes the plan at this fraction of the current risk slider: a
# one-tap "same setup, less risk" action. The multiplier is encoded in the callback data so it
# is explicit and testable; a free-entry modify is future NL work (PRD 8).
DEFAULT_MODIFY_MULTIPLIER = 0.5

# How long request_approval waits for a human decision before failing closed (REJECT).
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 3600.0
# Long-poll timeout handed to getUpdates per call.
DEFAULT_POLL_TIMEOUT_SECONDS = 30


class TelegramClient:
    """A thin, mockable wrapper over the Telegram Bot API. Holds the token; never logs it."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        session: requests.Session | None = None,
        request_timeout: float = 35.0,
    ) -> None:
        self._token = token
        self._base = f"{base_url}/bot{token}"
        self._session = session or requests.Session()
        self._timeout = request_timeout

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            f"{self._base}/{method}", json=payload, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return self._post("answerCallbackQuery", payload)

    def get_updates(
        self, offset: int | None = None, timeout: int = DEFAULT_POLL_TIMEOUT_SECONDS
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload)
        return result.get("result", [])

    def __repr__(self) -> str:  # never leak the token through the base url
        return "TelegramClient(base_url=https://api.telegram.org/bot***redacted***)"


def _esc(text: str) -> str:
    """Escape the only three characters HTML parse mode needs, so markup never breaks."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_approval_card(plan: TradePlan) -> str:
    """The approval card: one clear risk number, aligned rows, valid HTML (conduct rules 5, 8).

    No tier table, slider math, or clamp arithmetic appears: the operator sees the trade and a
    single risk figure. `plan.risk_line` is that figure ("Risk: 4% tier, $18.40 on this trade").
    """
    base = plan.symbol.replace("USDT", "")
    rows = [
        f"<b>Trade plan {_esc(plan.symbol)} {plan.side.value.upper()}</b>",
        f"<code>Entry:  {plan.entry_price:>12,.4f}</code>",
        f"<code>Stop:   {plan.stop_price:>12,.4f}</code>",
        f"<code>Target: {plan.take_profit_price:>12,.4f}</code>",
        f"<code>Size:   {plan.quantity:>12,.4f} {_esc(base)}</code>",
        f"<b>{_esc(plan.risk_line)}</b>",
    ]
    return "\n".join(rows)


def approval_keyboard(plan_id: str, modify_multiplier: float = DEFAULT_MODIFY_MULTIPLIER) -> dict:
    """The inline Approve / Reject / Modify keyboard. Callback data carries the plan id."""
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approve:{plan_id}"},
                {"text": "Reject", "callback_data": f"reject:{plan_id}"},
            ],
            [
                {
                    "text": f"Modify (risk x{modify_multiplier:g})",
                    "callback_data": f"modify:{plan_id}:{modify_multiplier:g}",
                }
            ],
        ]
    }


def format_event(event: ChannelEvent) -> str:
    """Plain, valid-HTML text for a one-way notification."""
    symbol = f"{_esc(event.symbol)} " if event.symbol else ""
    return f"<b>{_esc(event.kind.value)}</b> {symbol}{_esc(event.message)}".rstrip()


@dataclass(frozen=True)
class _ParsedCallback:
    action: str  # approve | reject | modify
    plan_id: str
    multiplier: float | None


def parse_callback_data(data: str) -> _ParsedCallback | None:
    """Parse callback_data 'action:plan_id[:multiplier]'. None if malformed."""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] not in ("approve", "reject", "modify"):
        return None
    multiplier: float | None = None
    if parts[0] == "modify":
        if len(parts) < 3:
            return None
        try:
            multiplier = float(parts[2])
        except ValueError:
            return None
    return _ParsedCallback(action=parts[0], plan_id=parts[1], multiplier=multiplier)


class TelegramApprovalChannel(ApprovalChannel):
    """A Telegram-backed approval channel. Construct via from_env in production."""

    def __init__(
        self,
        client: TelegramClient,
        allowed_chat_id: int,
        *,
        status_provider: Callable[[], str] | None = None,
        profit_provider: Callable[[], str] | None = None,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._allowed_chat_id = allowed_chat_id
        self._status_provider = status_provider
        self._profit_provider = profit_provider
        self._timeout = approval_timeout_seconds
        self._update_offset: int | None = None
        # Injected clock so tests are deterministic and never sleep.
        import time

        self._clock = clock or time.monotonic

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        status_provider: Callable[[], str] | None = None,
        profit_provider: Callable[[], str] | None = None,
        client: TelegramClient | None = None,
    ) -> "TelegramApprovalChannel | None":
        """Build from environment secrets, or return None with a clear log message if unconfigured.

        Returning None (not raising) is the safe start-up the task requires: no token in the
        environment is the expected state here and in CI, so the caller prints the message and
        uses the in-memory channel instead of crashing.
        """
        secrets: TelegramSecrets | None = load_telegram_secrets(env)
        if secrets is None:
            logger.warning(
                "Telegram not configured (%s unset): the paper loop will use the in-memory "
                "approval channel. Set the bot token to enable Telegram approvals.",
                "CRYPTO_TRADER_TELEGRAM_BOT_TOKEN",
            )
            return None
        if secrets.allowed_chat_id is None:
            logger.error(
                "Telegram bot token is set but no valid allowlisted chat id is configured "
                "(%s): refusing to start Telegram approvals, since every action would be "
                "unauthenticated.",
                "CRYPTO_TRADER_TELEGRAM_ALLOWED_CHAT_ID",
            )
            return None
        real_client = client or TelegramClient(secrets.bot_token)
        return cls(
            real_client,
            secrets.allowed_chat_id,
            status_provider=status_provider,
            profit_provider=profit_provider,
        )

    # ------------------------------------------------------------------ ApprovalChannel API

    def request_approval(self, plan: TradePlan) -> ApprovalDecision:
        """Send the card and block until an allowlisted decision arrives, or fail closed."""
        try:
            self._client.send_message(
                self._allowed_chat_id,
                format_approval_card(plan),
                reply_markup=approval_keyboard(plan.plan_id),
            )
        except requests.RequestException:
            logger.exception("failed to send approval card; failing closed (REJECT)")
            return ApprovalDecision(plan_id=plan.plan_id, verdict=ApprovalVerdict.REJECT)

        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            try:
                updates = self._client.get_updates(offset=self._update_offset)
            except requests.RequestException:
                logger.exception("getUpdates failed; failing closed (REJECT)")
                return ApprovalDecision(plan_id=plan.plan_id, verdict=ApprovalVerdict.REJECT)
            for update in updates:
                self._advance_offset(update)
                decision = self._decision_from_update(update, plan.plan_id)
                if decision is not None:
                    return decision
        logger.warning("approval timed out for plan %s; failing closed (REJECT)", plan.plan_id)
        return ApprovalDecision(plan_id=plan.plan_id, verdict=ApprovalVerdict.REJECT)

    def notify(self, event: ChannelEvent) -> None:
        """One-way notification. A transport failure is logged, never raised into the loop."""
        try:
            self._client.send_message(self._allowed_chat_id, format_event(event))
        except requests.RequestException:
            logger.exception("failed to send notification %s", event.kind.value)

    # ------------------------------------------------------------------ update handling

    def _advance_offset(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self._update_offset = update_id + 1

    def _decision_from_update(
        self, update: dict[str, Any], plan_id: str
    ) -> ApprovalDecision | None:
        """Turn one update into a decision for `plan_id`, or handle a command, or ignore it.

        A callback for a DIFFERENT plan id is ignored (returns None), so a stale button press
        cannot resolve the wrong plan. A command (/status, /profit) is answered inline and
        returns None so polling continues.
        """
        callback = update.get("callback_query")
        if callback is not None:
            return self._handle_callback(callback, plan_id)
        message = update.get("message")
        if message is not None:
            self._handle_command(message)
        return None

    def _handle_callback(
        self, callback: dict[str, Any], plan_id: str
    ) -> ApprovalDecision | None:
        chat_id = _chat_id_of(callback.get("message", {}))
        query_id = callback.get("id", "")
        if not self._is_allowed(chat_id):
            if query_id:
                _safe(self._client.answer_callback_query, query_id, "not authorized")
            return None
        parsed = parse_callback_data(callback.get("data", ""))
        if parsed is None or parsed.plan_id != plan_id:
            return None  # malformed or for a different plan: ignore, keep waiting
        if query_id:
            _safe(self._client.answer_callback_query, query_id)
        actor = str(chat_id)
        if parsed.action == "approve":
            return ApprovalDecision(plan_id, ApprovalVerdict.APPROVE, actor=actor)
        if parsed.action == "reject":
            return ApprovalDecision(plan_id, ApprovalVerdict.REJECT, actor=actor)
        return ApprovalDecision(
            plan_id, ApprovalVerdict.MODIFY, modified_slider=parsed.multiplier, actor=actor
        )

    def _handle_command(self, message: dict[str, Any]) -> None:
        chat_id = _chat_id_of(message)
        if not self._is_allowed(chat_id):
            return
        text = (message.get("text") or "").strip()
        if text.startswith("/status") and self._status_provider is not None:
            _safe(self._client.send_message, self._allowed_chat_id, _esc(self._status_provider()))
        elif text.startswith("/profit") and self._profit_provider is not None:
            _safe(self._client.send_message, self._allowed_chat_id, _esc(self._profit_provider()))

    def _is_allowed(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id == self._allowed_chat_id


def _chat_id_of(message: dict[str, Any]) -> int | None:
    chat = message.get("chat")
    if isinstance(chat, dict):
        cid = chat.get("id")
        if isinstance(cid, int):
            return cid
    return None


def _safe(func: Callable[..., Any], *args: Any) -> None:
    """Call a transport method, swallowing transport errors (a notify/answer must never crash)."""
    try:
        func(*args)
    except requests.RequestException:
        logger.exception("telegram transport call failed: %s", getattr(func, "__name__", func))
