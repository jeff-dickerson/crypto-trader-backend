"""KillSwitchMonitor: evaluates PRD 9.1's auto-triggers each cycle and drives the flatten.

Call `check_cycle(now=...)` once per decision cycle (each newly-closed bar), before the position
manager acts on that bar, so a fresh trigger halts new entries on the very same bar (PositionManager
checks `kill_switch.is_armed` in `_maybe_seek_entry`; see AGENTS.md). Each cycle:

1. If already triggered and not yet confirmed flat, retry the flatten (this is what makes the
   degraded-mode fallback keep retrying ACROSS cycles, not just within one burst of attempts) and
   return; no auto-trigger re-evaluation happens while halted.
2. Otherwise read account state from the adapter (exchange is the single source of truth,
   governing principle 1) and evaluate, in order: consecutive API failures, daily loss, max
   drawdown. The first breach fires `_trigger`, which halts immediately and then attempts to
   flatten.

Auto-triggers implemented here (PRD 9.1): 6% daily loss, 15% max drawdown, 5 consecutive API
failures. The fourth PRD 9.1 auto-trigger, "websocket dead more than 5 minutes with open
positions", is NOT evaluated here: the paper/DryRun adapter has no real websocket (AGENTS.md), so
there is no genuine heartbeat timestamp to check, and this task does not fabricate one.
`websocket_dead_trigger` below implements the trigger CONDITION so step 6's live adapter can call
it once it has a real heartbeat; see its docstring and AGENTS.md for the still-open judgment call
(auto-flatten vs. alert-and-hold) that step 6 must resolve explicitly.

Degraded-mode fallback (the PRD 9.1 open risk this class resolves): `_attempt_flatten` closes
every open position at market first, then cancels remaining resting orders, catching
ExchangeConnectionError from each call. On a failure it alerts through the ApprovalChannel
(KILL_SWITCH_DEGRADED, "repeatedly" because every failed attempt, in this cycle or a later one,
sends another) and backs off before retrying; it never gives up permanently; a bounded number of
attempts run within one cycle, and if none confirms flat, the next cycle's early-return branch
above picks the retry back up. Throughout, the ALREADY-PLACED on-exchange resting stops
(principle 4) remain the safety floor: this class never assumes a position is closed just
because a cancel/market-close call was accepted, only once `_confirm_flat` reads back empty
positions and orders from the exchange itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from time import sleep as _real_sleep

from crypto_trader.approval.channel import ApprovalChannel, ChannelEvent, EventKind
from crypto_trader.exchange.adapter import ExchangeAdapter, ExchangeConnectionError
from crypto_trader.exchange.types import MarketOrderRequest, OrderSide
from crypto_trader.paper.persistence import NULL_SINK, PersistenceSink
from crypto_trader.safety.equity import EquityTracker
from crypto_trader.safety.kill_switch import KillSwitch, KillSwitchReason


@dataclass(frozen=True)
class KillSwitchConfig:
    """Auto-trigger thresholds and the flatten retry policy. All overridable (PRD 9.1 defaults)."""

    daily_loss_fraction: float = 0.06
    max_drawdown_fraction: float = 0.15
    max_consecutive_api_failures: int = 5
    websocket_dead_seconds: float = 5 * 60.0  # see websocket_dead_trigger; not wired for paper
    flatten_max_attempts: int = 5
    flatten_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 0.0 < self.daily_loss_fraction < 1.0:
            raise ValueError("daily_loss_fraction must be in (0, 1)")
        if not 0.0 < self.max_drawdown_fraction < 1.0:
            raise ValueError("max_drawdown_fraction must be in (0, 1)")
        if self.max_consecutive_api_failures < 1:
            raise ValueError("max_consecutive_api_failures must be >= 1")
        if self.flatten_max_attempts < 1:
            raise ValueError("flatten_max_attempts must be >= 1")
        if self.flatten_backoff_seconds < 0.0:
            raise ValueError("flatten_backoff_seconds must be >= 0")


DEFAULT_KILL_SWITCH_CONFIG = KillSwitchConfig()


class KillSwitchState(str, Enum):
    """The single named state the REST API renders (the reconciliation note in the task brief).

    Derived from the two real booleans on KillSwitch plus the monitor's flatten-attempt tracking,
    rather than stored as a parallel field on KillSwitch:

    - ARMED           `is_armed=True`: trading is live, the kill switch has not engaged.
    - FLATTENING      `is_armed=False, flatten_confirmed=False`, and the last flatten round has not
                      yet exhausted its attempt budget: engaged and actively closing out.
    - FLAT_CONFIRMED  `is_armed=False, flatten_confirmed=True`: engaged and the exchange has
                      confirmed no positions or orders remain.
    - FLATTEN_FAILED  `is_armed=False, flatten_confirmed=False`, and the last flatten round
                      exhausted its attempts without confirming flat: the honest proxy the brief
                      asked for. The monitor still keeps retrying across cycles (the on-exchange
                      resting stops are the floor meanwhile), but the API surfaces this so `rearm`
                      can act on it and the operator sees that automatic flattening stalled.
    """

    ARMED = "armed"
    FLATTENING = "flattening"
    FLAT_CONFIRMED = "flat_confirmed"
    FLATTEN_FAILED = "flatten_failed"


class KillSwitchMonitor:
    """Watches one ExchangeAdapter's account state and drives one KillSwitch."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        approval: ApprovalChannel,
        kill_switch: KillSwitch,
        *,
        config: KillSwitchConfig = DEFAULT_KILL_SWITCH_CONFIG,
        sleep_fn: Callable[[float], None] = _real_sleep,
        sink: PersistenceSink = NULL_SINK,
    ) -> None:
        self.adapter = adapter
        self.approval = approval
        self.kill_switch = kill_switch
        self.config = config
        self.equity = EquityTracker()
        self._sleep = sleep_fn
        # Persistence + observability for the REST /kill-switch endpoint (no-op with NULL_SINK).
        self._sink = sink
        # Flatten-attempt tracking, so the API can derive FLATTENING vs FLATTEN_FAILED and render
        # "Flattening: N of M closed" without KillSwitch carrying a parallel state field.
        self.flatten_attempt_count = 0
        self._last_flatten_exhausted = False
        self._flatten_target_count = 0

    def check_cycle(self, *, now: datetime) -> None:
        if not self.kill_switch.is_armed:
            if not self.kill_switch.flatten_confirmed:
                self._attempt_flatten(now)
            return

        equity = self._safe_get_equity()
        failures = self._safe_consecutive_failures()
        if failures is not None and failures >= self.config.max_consecutive_api_failures:
            self._trigger(
                KillSwitchReason.API_FAILURES,
                f"{failures} consecutive API failures reaching the exchange",
                now,
                {"consecutive_failures": failures},
            )
            return
        if equity is None:
            return  # could not read equity this cycle; the failure above already recorded it

        self.equity.observe(equity, now)

        day_start = self.equity.day_start_equity
        if day_start is not None and day_start > 0:
            loss_fraction = (day_start - equity) / day_start
            if loss_fraction >= self.config.daily_loss_fraction:
                self._trigger(
                    KillSwitchReason.DAILY_LOSS,
                    f"daily loss {loss_fraction:.1%} >= {self.config.daily_loss_fraction:.1%} "
                    "threshold",
                    now,
                    {"daily_loss_fraction": loss_fraction},
                )
                return

        peak = self.equity.peak_equity
        if peak is not None and peak > 0:
            drawdown_fraction = (peak - equity) / peak
            if drawdown_fraction >= self.config.max_drawdown_fraction:
                self._trigger(
                    KillSwitchReason.MAX_DRAWDOWN,
                    f"drawdown {drawdown_fraction:.1%} >= "
                    f"{self.config.max_drawdown_fraction:.1%} threshold",
                    now,
                    {"drawdown_fraction": drawdown_fraction},
                )
                return

    # ------------------------------------------------------------------------- internals

    def _safe_get_equity(self) -> float | None:
        try:
            return self.adapter.get_balance().total
        except ExchangeConnectionError:
            return None

    def _safe_consecutive_failures(self) -> int | None:
        try:
            return self.adapter.consecutive_api_failures()
        except ExchangeConnectionError:
            return None

    def _trigger(
        self,
        reason: KillSwitchReason,
        message: str,
        now: datetime,
        detail: dict[str, object],
        *,
        flatten_once: bool = False,
    ) -> None:
        self.kill_switch.trigger(reason, message, now=now, detail=detail)
        # Capture how many positions the flatten must close, so the API can render progress.
        self._flatten_target_count = self._safe_position_count()
        self.flatten_attempt_count = 0
        self._last_flatten_exhausted = False
        self.approval.notify(
            ChannelEvent(
                kind=EventKind.KILL_SWITCH_FIRED,
                message=f"KILL SWITCH FIRED: {message}",
                detail={"reason": reason.value, **detail},
            )
        )
        if flatten_once:
            self._attempt_flatten_once(now)
        else:
            self._attempt_flatten(now)
        self._sink.record_kill_switch_event(
            source=reason.value,
            outcome=self.state().value,
            at=now,
            detail={"message": message, **detail},
        )

    def _safe_position_count(self) -> int:
        try:
            return len(self.adapter.get_positions())
        except ExchangeConnectionError:
            return self._flatten_target_count

    def _attempt_flatten(self, now: datetime) -> None:
        """Cancel and close everything, retrying with backoff; never reports flat on hope alone."""
        for attempt in range(1, self.config.flatten_max_attempts + 1):
            self.flatten_attempt_count += 1
            try:
                self._cancel_and_close_all()
            except ExchangeConnectionError as exc:
                self.approval.notify(
                    ChannelEvent(
                        kind=EventKind.KILL_SWITCH_DEGRADED,
                        message=(
                            f"kill switch degraded mode: flatten attempt {attempt} could not "
                            f"reach the exchange ({exc}); on-exchange resting stops remain the "
                            "safety floor, retrying"
                        ),
                        detail={"attempt": attempt},
                    )
                )
                self._sleep(self.config.flatten_backoff_seconds * attempt)
                continue
            if self._confirm_flat():
                self.kill_switch.mark_flatten_confirmed()
                self._last_flatten_exhausted = False
                self.approval.notify(
                    ChannelEvent(
                        kind=EventKind.KILL_SWITCH_FLATTENED,
                        message=(
                            "kill switch confirmed flat: the exchange reports no open "
                            "positions or orders"
                        ),
                    )
                )
                return
            self._sleep(self.config.flatten_backoff_seconds * attempt)
        # The attempt budget for this cycle is exhausted without confirmation: surface it as
        # FLATTEN_FAILED for the API (the monitor still retries next cycle; the resting stops hold).
        self._last_flatten_exhausted = True
        self.approval.notify(
            ChannelEvent(
                kind=EventKind.KILL_SWITCH_DEGRADED,
                message=(
                    f"kill switch: {self.config.flatten_max_attempts} flatten attempt(s) "
                    "exhausted this cycle without exchange confirmation; on-exchange stops "
                    "remain the floor, will keep retrying next cycle"
                ),
            )
        )

    def _attempt_flatten_once(self, now: datetime) -> None:
        """One non-sleeping flatten attempt, for the manual (API) arm path.

        `manual_trigger` (POST /kill-switch/arm) must return immediately with no blocking I/O in
        the handler thread, so unlike `_attempt_flatten` it never sleeps between attempts and
        never loops. It shares the same `flatten_attempt_count` / `_last_flatten_exhausted`
        bookkeeping, so a repeated manual arm while still flattening counts toward the same
        `flatten_max_attempts` budget and eventually reaches FLATTEN_FAILED exactly like the
        auto-trigger path, keeping rearm reachable. The `now` parameter is accepted for symmetry
        with `_attempt_flatten` even though this attempt does not itself need it.
        """
        del now
        attempt = self.flatten_attempt_count + 1
        self.flatten_attempt_count = attempt
        exhausted = attempt >= self.config.flatten_max_attempts
        try:
            self._cancel_and_close_all()
        except ExchangeConnectionError as exc:
            self.approval.notify(
                ChannelEvent(
                    kind=EventKind.KILL_SWITCH_DEGRADED,
                    message=(
                        f"kill switch degraded mode: flatten attempt {attempt} could not "
                        f"reach the exchange ({exc}); on-exchange resting stops remain the "
                        "safety floor, retrying"
                    ),
                    detail={"attempt": attempt},
                )
            )
            self._last_flatten_exhausted = exhausted
            return
        if self._confirm_flat():
            self.kill_switch.mark_flatten_confirmed()
            self._last_flatten_exhausted = False
            self.approval.notify(
                ChannelEvent(
                    kind=EventKind.KILL_SWITCH_FLATTENED,
                    message=(
                        "kill switch confirmed flat: the exchange reports no open "
                        "positions or orders"
                    ),
                )
            )
            return
        self._last_flatten_exhausted = exhausted
        if exhausted:
            self.approval.notify(
                ChannelEvent(
                    kind=EventKind.KILL_SWITCH_DEGRADED,
                    message=(
                        f"kill switch: {self.config.flatten_max_attempts} flatten attempt(s) "
                        "exhausted without exchange confirmation; on-exchange stops remain the "
                        "floor, will keep retrying next cycle"
                    ),
                )
            )

    def _cancel_and_close_all(self) -> None:
        """Close positions at market before cancelling resting orders.

        Closing first means a protective on-exchange stop is never cancelled until the
        position it protects is already closed, so an interruption partway through this
        method (an ExchangeConnectionError on a later call) never leaves a position both
        unprotected and open.
        """
        for position in self.adapter.get_positions():
            exit_side = OrderSide.SELL if position.side is OrderSide.BUY else OrderSide.BUY
            self.adapter.place_market_order(
                MarketOrderRequest(
                    symbol=position.symbol,
                    side=exit_side,
                    quantity=position.quantity,
                    reduce_only=True,
                )
            )
        for order in self.adapter.get_open_orders():
            try:
                self.adapter.cancel_order(order.symbol, order.order_id)
            except KeyError:
                pass  # already gone; not a connectivity failure

    def _confirm_flat(self) -> bool:
        try:
            return not self.adapter.get_positions() and not self.adapter.get_open_orders()
        except ExchangeConnectionError:
            return False

    # --------------------------------------------------------------- REST /kill-switch surface

    def state(self) -> KillSwitchState:
        """The single named state the API renders, derived from the two real booleans."""
        if self.kill_switch.is_armed:
            return KillSwitchState.ARMED
        if self.kill_switch.flatten_confirmed:
            return KillSwitchState.FLAT_CONFIRMED
        if self._last_flatten_exhausted:
            return KillSwitchState.FLATTEN_FAILED
        return KillSwitchState.FLATTENING

    def flatten_target_count(self) -> int:
        """How many positions the current flatten set out to close (0 when armed/none)."""
        return self._flatten_target_count

    def positions_remaining(self) -> int:
        """Open positions still on the exchange (best effort; 0 when unreachable)."""
        try:
            return len(self.adapter.get_positions())
        except ExchangeConnectionError:
            return self._flatten_target_count

    def status_line(self) -> str:
        """One compressed status line for the UI (conduct rule 8)."""
        st = self.state()
        if st is KillSwitchState.ARMED:
            return "Armed (trading live)"
        if st is KillSwitchState.FLAT_CONFIRMED:
            return "Flat confirmed (kill switch engaged)"
        target = self._flatten_target_count
        closed = max(0, target - self.positions_remaining())
        if st is KillSwitchState.FLATTEN_FAILED:
            return f"Flatten stalled: {closed} of {target} closed, retrying"
        return f"Flattening: {closed} of {target} closed"

    def manual_trigger(self, *, now: datetime) -> KillSwitchState:
        """Engage the kill switch from the API (POST /kill-switch/arm). Idempotent.

        Engaging while ARMED triggers the MANUAL halt and makes exactly ONE non-sleeping flatten
        attempt, then returns immediately: no blocking I/O in the handler thread (the multi-attempt
        backoff loop in `_attempt_flatten` stays reserved for the auto-trigger path driven by
        `check_cycle` in the bot-loop thread). Engaging while already halted does not re-trigger
        (nothing new to record); it makes one more non-sleeping flatten attempt, so a repeated
        POST /arm while FLATTENING/FLATTEN_FAILED keeps making progress (and can still reach
        FLATTEN_FAILED once the cumulative attempt budget is exhausted) rather than erroring or
        blocking. Returns the resulting state, which the endpoint reports immediately.
        """
        if self.kill_switch.is_armed:
            self._trigger(
                KillSwitchReason.MANUAL,
                "manual kill switch engaged via the REST API",
                now,
                {"source": "api"},
                flatten_once=True,
            )
        else:
            self._attempt_flatten_once(now)
        return self.state()

    def rearm(self, *, now: datetime) -> KillSwitchState:
        """Resume trading (POST /kill-switch/rearm). Valid only from FLAT_CONFIRMED/FLATTEN_FAILED.

        Raises ValueError from ARMED (nothing to rearm) or FLATTENING (still actively closing out);
        the endpoint maps that to a 409 conflict.
        """
        current = self.state()
        if current not in (KillSwitchState.FLAT_CONFIRMED, KillSwitchState.FLATTEN_FAILED):
            raise ValueError(f"cannot rearm from {current.value}")
        self.kill_switch.rearm()
        self.flatten_attempt_count = 0
        self._last_flatten_exhausted = False
        self._flatten_target_count = 0
        self.approval.notify(
            ChannelEvent(
                kind=EventKind.KILL_SWITCH_REARMED,
                message="kill switch re-armed: trading resumed by the operator",
            )
        )
        self._sink.record_kill_switch_event(
            source=KillSwitchReason.MANUAL.value, outcome="rearmed", at=now,
            detail={"from_state": current.value},
        )
        return self.state()


def websocket_dead_trigger(
    last_message_at: datetime | None,
    now: datetime,
    has_open_positions: bool,
    config: KillSwitchConfig = DEFAULT_KILL_SWITCH_CONFIG,
) -> bool:
    """The PRD 9.1 "websocket dead more than 5 minutes with open positions" trigger CONDITION.

    NOT called anywhere in KillSwitchMonitor.check_cycle: the paper/DryRun adapter has no real
    websocket to report a heartbeat from (AGENTS.md), so wiring this in now would mean feeding it
    a fabricated timestamp, which the task brief explicitly rules out. This function exists so
    step 6's live adapter, once it has a real `last_message_at`, can call it directly.
    `last_message_at=None` (never connected, or N/A for a venue with no stream) returns False:
    "unknown" must never be conflated with "confirmed dead".

    This function answers the trigger CONDITION only, not the response. A separate, still-open
    judgment call the design review flagged (PRD 9.1, AGENTS.md): auto-flattening purely because a
    websocket dropped can fight the "on-exchange stops are the floor" principle when REST and the
    resting stops are actually fine, so the live-adapter task (step 6) must decide explicitly
    whether this condition should auto-flatten (call KillSwitchMonitor's trigger path) or instead
    alert-and-hold. Deciding that here, for a trigger that cannot fire yet, would be exactly the
    silent unilateral pick the brief asks this task not to make.
    """
    if not has_open_positions or last_message_at is None:
        return False
    return (now - last_message_at) >= timedelta(seconds=config.websocket_dead_seconds)
