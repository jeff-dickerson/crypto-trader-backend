"""The approval seam: every trade plan is approved by a human before it is submitted.

ApprovalChannel (channel.py) is the abstract contract, mirroring the ExchangeAdapter
honesty pattern: a money-adjacent, multi-method boundary implemented by exactly two named
classes (an in-memory fake for tests, a Telegram surface for v1), so it is an abc.ABC that
fails loudly at construction if a method is missing. InMemoryApprovalChannel (memory.py) is
the fake the whole paper loop is testable against with no network or bot token.
TelegramApprovalChannel (telegram.py) is the real v1 control surface (captain decision 3).
"""

from __future__ import annotations

from crypto_trader.approval.channel import (
    ApprovalChannel,
    ApprovalDecision,
    ApprovalVerdict,
    ChannelEvent,
    EventKind,
)
from crypto_trader.approval.memory import InMemoryApprovalChannel

__all__ = [
    "ApprovalChannel",
    "ApprovalDecision",
    "ApprovalVerdict",
    "ChannelEvent",
    "EventKind",
    "InMemoryApprovalChannel",
]
