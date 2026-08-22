"""Secret loading for the paper/live control surfaces (PRD 9.4).

This is the structural half of PRD 9.4's four-layer secrets plan: secrets load
once from the environment into a small in-memory object, and the config plane
(StrategyConfig, RiskConfig, BitunixSourceConfig, and friends) structurally cannot
contain them, so a config dump can never leak a token. A redaction __repr__ on the
loaded object is defense in depth (layer 3): even if one of these objects is logged
or lands in a traceback, the token value never renders.

Only the Telegram bot token is needed at Build Order step 4: the paper loop places
no real orders and uses no exchange credentials. The live Bitunix API key
(trade-only, withdrawals disabled, IP-allowlisted) is a step-6 concern and gets its
own loader when that task lands; the same pattern applies.

No value here is ever passed to an unrelated service, logged, or written to the
database (PRD 9.4: "the database never stores secrets").
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Environment variable names. The token follows the existing os.environ convention
# used for CRYPTO_TRADER_DB_PATH in crypto_trader.config; it is never hardcoded and
# never committed.
TELEGRAM_BOT_TOKEN_ENV = "CRYPTO_TRADER_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "CRYPTO_TRADER_TELEGRAM_ALLOWED_CHAT_ID"

_REDACTED = "***redacted***"


@dataclass(frozen=True)
class TelegramSecrets:
    """The Telegram bot token plus the single allowlisted chat id.

    `allowed_chat_id` is the pinned, allowlisted chat the bot will accept approval
    actions from (PRD section 8: approval buttons are authenticated actions, not open
    to any chat that finds the bot). It is not itself a secret in the same sense as
    the token, but it is a security control, so it lives here alongside the token.

    The token is NEVER included in repr/str output, so this object is safe to log or
    to let surface in a traceback without leaking the credential (PRD 9.4 layer 3).
    Read `bot_token` explicitly and only where the value is actually needed.
    """

    bot_token: str
    allowed_chat_id: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security-load-bearing
        chat = self.allowed_chat_id if self.allowed_chat_id is not None else "unset"
        return f"TelegramSecrets(bot_token={_REDACTED}, allowed_chat_id={chat})"

    __str__ = __repr__


def load_telegram_secrets(
    env: dict[str, str] | None = None,
) -> TelegramSecrets | None:
    """Load Telegram secrets from the environment, or None if no token is configured.

    Returning None (rather than raising) lets the caller fail clearly and safely: the
    paper loop can print a plain "Telegram not configured" startup message and run
    against the in-memory approval channel instead of crashing, which is exactly what
    Build Order step 4 requires when no real token exists in the environment.

    `env` is injectable so tests never touch the real process environment.
    """
    source = os.environ if env is None else env
    token = source.get(TELEGRAM_BOT_TOKEN_ENV, "").strip()
    if not token:
        return None
    raw_chat = source.get(TELEGRAM_CHAT_ID_ENV, "").strip()
    chat_id: int | None = None
    if raw_chat:
        try:
            chat_id = int(raw_chat)
        except ValueError:
            # A malformed chat id is a configuration error, but it must never take the
            # process down or leak the token: treat it as "no allowlist pinned" so the
            # channel refuses every action rather than trusting an unparseable id.
            chat_id = None
    return TelegramSecrets(bot_token=token, allowed_chat_id=chat_id)
