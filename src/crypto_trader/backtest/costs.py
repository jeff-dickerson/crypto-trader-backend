"""Realistic per-trade cost model: fees, slippage, funding, and minimum notional.

Every closed trade in the backtest is scored in RISK MULTIPLES (R), where one R is the
initial risk distance |entry - initial_stop|. Fees, slippage, and funding are all charged
as fractions of notional, and notional is price times quantity; the R-multiple divides
by (risk_distance times quantity), so quantity CANCELS and the net R-multiple is
independent of position size. That is exactly why expectancy is reported in R: it is the
size-independent measure of the edge. Minimum notional is the one thing that does NOT
cancel, so it is handled separately as an entry-time feasibility filter using an assumed
account equity and risk fraction (PRD 4.1 tiers), matching the design-review recommendation
to check minimum notional at plan time (PRD 4.2) rather than discovering it after the fact.

Fee model, by order role:
- Entry is a resting LIMIT at the value-area boundary, so it pays the MAKER fee.
- A take-profit exit is a resting LIMIT, so it also pays the MAKER fee and takes no
  slippage (it only fills if the market trades through the limit).
- A stop, momentum-shift, or end-of-data exit is a MARKET exit, so it pays the TAKER fee
  and takes adverse slippage. Per PRD 6.2 (Gate 2), "stops always slip"; the same
  conservative assumption is applied in the backtest here.

Fee and funding RATES are documented placeholders (see the field comments): Bitunix's real
fee schedule and funding cadence are MUST-VERIFY items for the adapter spike (PRD 9.3). The
values here are conventional perpetual-futures figures, and every reported result states
that they are placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_trader.strategy.position import PositionSide


@dataclass(frozen=True)
class CostConfig:
    """Fee, slippage, and minimum-notional parameters, plus the sizing assumptions.

    All rates are fractions (0.0002 = 0.02% = 2 basis points). PROVISIONAL placeholders,
    pending the Bitunix adapter spike (PRD 9.3).
    """

    # Maker fee: charged on the limit entry and on a limit take-profit exit.
    maker_fee_rate: float = 0.0002
    # Taker fee: charged on market exits (stop, momentum-shift, end-of-data).
    taker_fee_rate: float = 0.0006
    # Adverse slippage on market exits only, as a fraction of the exit price. A stop for a
    # long fills BELOW the stop level by this fraction; a take-profit limit takes none.
    slippage_rate: float = 0.0005

    # Minimum order notional in quote currency (e.g. USDT). A planned trade whose sized
    # notional falls below this is VOIDED at entry, never silently filled (PRD 4.2 slider
    # honesty). Placeholder value; confirm against Bitunix's real minimum.
    min_notional: float = 5.0

    # Sizing assumptions used ONLY for the minimum-notional feasibility check and the
    # percentage-drawdown view (the R-multiple expectancy does not depend on them). Equity
    # and per-trade risk fraction follow PRD 4.1's tiered schedule; 2% is the >= $2,000
    # tier. The full risk system is a future task, so these are fixed assumptions here.
    assumed_equity: float = 2000.0
    risk_fraction: float = 0.02

    def __post_init__(self) -> None:
        for name in ("maker_fee_rate", "taker_fee_rate", "slippage_rate", "risk_fraction"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be >= 0")
        if self.assumed_equity <= 0.0:
            raise ValueError("assumed_equity must be > 0")
        if self.min_notional < 0.0:
            raise ValueError("min_notional must be >= 0")


@dataclass(frozen=True)
class CostBreakdown:
    """Per-unit-price cost components of one closed trade, plus the resulting net R.

    Every price-denominated field is per unit of the asset (quantity has cancelled). R is
    the risk multiple. `gross_r` ignores costs; `net_r` subtracts them.
    """

    risk_distance: float
    gross_r: float
    entry_fee_price: float
    exit_fee_price: float
    slippage_price: float
    funding_price: float
    net_r: float


def _effective_exit_price(
    side: PositionSide, exit_price: float, exit_is_maker: bool, config: CostConfig
) -> float:
    """Apply adverse slippage to a market exit; a limit (maker) exit fills at its level."""
    if exit_is_maker:
        return exit_price
    slip = exit_price * config.slippage_rate
    # Slippage always hurts: a long exits lower, a short exits higher.
    return exit_price - slip if side is PositionSide.LONG else exit_price + slip


def compute_costs(
    side: PositionSide,
    entry_price: float,
    exit_price: float,
    initial_stop: float,
    *,
    exit_is_maker: bool,
    funding_fraction: float,
    config: CostConfig,
) -> CostBreakdown:
    """Score one closed trade in R after fees, slippage, and funding.

    `funding_fraction` is the signed funding total over the hold as a fraction of notional
    (positive = funding paid, from crypto_trader.backtest.funding.funding_cost_fraction).
    `exit_is_maker` is True only for a take-profit limit exit; stops and market exits are
    taker exits that also slip. The returned net R is what expectancy is computed from.
    """
    risk_distance = abs(entry_price - initial_stop)
    if risk_distance <= 0.0:
        raise ValueError("initial_stop must differ from entry_price (risk distance > 0)")

    effective_exit = _effective_exit_price(side, exit_price, exit_is_maker, config)

    if side is PositionSide.LONG:
        gross_price = effective_exit - entry_price
    else:
        gross_price = entry_price - effective_exit

    entry_fee_price = entry_price * config.maker_fee_rate
    exit_fee_rate = config.maker_fee_rate if exit_is_maker else config.taker_fee_rate
    exit_fee_price = exit_price * exit_fee_rate
    # Slippage as an explicit component (already reflected in effective_exit); reported so
    # the breakdown sums cleanly. Zero for maker exits.
    slippage_price = 0.0 if exit_is_maker else exit_price * config.slippage_rate
    funding_price = funding_fraction * entry_price

    net_price = gross_price - entry_fee_price - exit_fee_price - funding_price
    return CostBreakdown(
        risk_distance=risk_distance,
        gross_r=(
            (exit_price - entry_price) if side is PositionSide.LONG
            else (entry_price - exit_price)
        ) / risk_distance,
        entry_fee_price=entry_fee_price,
        exit_fee_price=exit_fee_price,
        slippage_price=slippage_price,
        funding_price=funding_price,
        net_r=net_price / risk_distance,
    )


def position_notional(entry_price: float, initial_stop: float, config: CostConfig) -> float:
    """Notional the risk-sized position would carry at entry, in quote currency.

    Quantity is risk_currency / risk_distance, so notional = quantity * entry_price =
    risk_currency * entry_price / risk_distance. Used only for the minimum-notional check.
    """
    risk_distance = abs(entry_price - initial_stop)
    if risk_distance <= 0.0:
        return 0.0
    risk_currency = config.assumed_equity * config.risk_fraction
    quantity = risk_currency / risk_distance
    return quantity * entry_price


def min_notional_ok(entry_price: float, initial_stop: float, config: CostConfig) -> bool:
    """True when the risk-sized trade meets the minimum order notional (else it is voided)."""
    return position_notional(entry_price, initial_stop, config) >= config.min_notional
