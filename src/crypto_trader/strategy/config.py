"""Strategy parameters for the Volume Profile POC Zone Retest (HTF-gated) v1 strategy.

Captain decision 5 (see AGENTS.md) delegates the exact value of every unspecified
strategy parameter to the builder of this step, to be validated against the future
Gate 1 backtest. Gate 1 does not exist yet, so none of these values are settled
truth: they are reasoned, concrete defaults, documented here and in AGENTS.md, and
every one is an overridable field on this dataclass rather than a magic number
buried in logic.

Read the field-by-field rationale below. Anything a future tuning task changes
should be changed here, not in the modules that consume the config.
"""

from __future__ import annotations

from dataclasses import dataclass

# A 4H candle timeframe yields six candles per UTC day (00:00, 04:00, ... 20:00).
# Used only to translate a lookback expressed in days into a candle count.
H4_CANDLES_PER_DAY = 6


@dataclass(frozen=True)
class StrategyConfig:
    """Every tunable knob of the v1 strategy, with a pinned default and a reason.

    All defaults are PROPOSED, pending Gate 1 out-of-sample validation (a future
    task). Do not treat any of these as validated truth.
    """

    # --- Volume profile construction -------------------------------------------------
    # Bucketing choice: a FIXED NUMBER OF BINS across the lookback range's price span,
    # not a fixed price increment. Rationale: a fixed bin count self-scales to any
    # symbol's price (BTC near 50000 or a sub-dollar alt) with no per-symbol tuning,
    # whereas a fixed price increment would need re-picking per symbol. 24 bins over a
    # 30-90 day 4H window (180-540 candles) is enough resolution to separate real
    # HVN/LVN structure without shattering volume into per-bin noise.
    profile_bins: int = 24

    # Value area percentage. The spec hinted "70%?"; 70% is the conventional
    # TPO/volume-profile default and a reasonable starting point.
    value_area_pct: float = 0.70

    # HVN/LVN numeric definitions, expressed relative to the MEAN bin volume (total
    # volume / bin count, so empty bins correctly drag the mean down and register as
    # voids). A bin is a High Volume Node when its volume is at least 1.3x the mean
    # (a genuine cluster), and a Low Volume Node when its volume is at most 0.5x the
    # mean (a liquidity void). These are percentile-free, deterministic cuts.
    hvn_volume_ratio: float = 1.3
    lvn_volume_ratio: float = 0.5

    # --- Lookback --------------------------------------------------------------------
    # Single PINNED lookback within the allowed 30-90 day range: 60 days, the midpoint.
    # This is deliberately one fixed value, not a swept/tuned parameter. A swept
    # lookback is a tuned parameter and only the future Gate 1 out-of-sample number can
    # validate it honestly (see AGENTS.md).
    lookback_days: int = 60

    # Below this many 4H candles in the profile window, the profile is not trusted and
    # generate_signal returns no entry signal (not enough history yet). ~10 days of 4H.
    min_profile_candles: int = 60

    # --- Separation-then-return setup ------------------------------------------------
    # "Separation": before a return counts as a valid setup, price must have moved at
    # least this fraction beyond the entry boundary (1.5% of the boundary price) ...
    separation_pct: float = 0.015
    # ... and stayed beyond it for at least this many consecutive 4H candles (3 x 4H =
    # 12 hours), so a single wick away from the zone does not qualify as separation.
    separation_min_bars: int = 3

    # --- Entry boundary --------------------------------------------------------------
    # The entry price is the value-area EDGE on the bias side, never the POC centre
    # line: VAL (value area low) for a long, VAH (value area high) for a short. This
    # is structural, not a config number, but is called out here as the pinned rule.

    # --- Stop derivation -------------------------------------------------------------
    # Stop sits in the adjacent LVN just beyond the entry zone (below VAL for a long,
    # above VAH for a short): at the LVN bin centre, nudged this fraction further into
    # the void so an ordinary wick into the low-liquidity gap does not stop us out.
    stop_lvn_buffer_pct: float = 0.001
    # If there is no LVN beyond the entry zone within the window (data edge), fall back
    # to a fixed offset this fraction beyond the entry boundary.
    stop_fallback_pct: float = 0.01

    # --- Take-profit derivation ------------------------------------------------------
    # Take profit sits just BEFORE the next opposing HVN (the first HVN cluster beyond
    # the value area in the trade's direction): at that HVN's near edge, pulled back
    # this fraction so we exit into liquidity rather than through it.
    tp_hvn_buffer_pct: float = 0.002
    # If there is no opposing HVN within the window, fall back to a reward target of
    # this multiple of the initial risk (R = |entry - stop|).
    tp_fallback_rr: float = 2.0

    # --- MA ensemble momentum-shift exit ---------------------------------------------
    # The ensemble is these simple moving averages of 4H closes (the management
    # timeframe): a fast, a medium, and a slow MA. Chosen over a single fast/slow cross
    # so the exit reflects agreement across horizons, not one noisy line.
    ma_ensemble_periods: tuple[int, ...] = (10, 20, 50)
    # Flip rule: MAJORITY VOTE. Count the ensemble MAs the last close has crossed to the
    # wrong side of (below, for a long; above, for a short). The momentum-shift exit
    # fires when at least this many vote against the position. With a 3-MA ensemble, 2
    # is a strict majority. The full ensemble must be computable (enough candles) before
    # any exit is declared; otherwise the position is held.
    ma_exit_min_votes: int = 2

    # --- Trailing stop ---------------------------------------------------------------
    # The trailing stop ARMS once the most-favourable excursion since entry reaches this
    # multiple of the initial risk R (1.0R = the position is up one unit of risk) ...
    trail_activation_rr: float = 1.0
    # ... and then trails this multiple of R behind the best price reached. With both at
    # 1.0, the stop reaches breakeven at +1R and ratchets up (never loosens) from there.
    trail_distance_rr: float = 1.0

    # --- Overtrading governor --------------------------------------------------------
    # One zone, one trade. Two entry boundaries within this fraction of each other are
    # treated as the SAME zone, so a later retest of an already-traded zone is rejected
    # even if the recomputed boundary drifted slightly.
    zone_match_pct: float = 0.005

    # --- Daily bias gate -------------------------------------------------------------
    # Bias is a fast-vs-slow SMA trend filter on daily closes.
    bias_fast_period: int = 20
    bias_slow_period: int = 50
    # The fast/slow SMA separation must exceed this fraction of the slow SMA to count as
    # a real trend; inside the band the bias is neutral (no trade). Prevents a razor-thin
    # cross from declaring a directional bias.
    bias_neutral_band: float = 0.002

    def __post_init__(self) -> None:
        if self.profile_bins < 1:
            raise ValueError("profile_bins must be >= 1")
        if not 0.0 < self.value_area_pct <= 1.0:
            raise ValueError("value_area_pct must be in (0, 1]")
        if self.separation_min_bars < 1:
            raise ValueError("separation_min_bars must be >= 1")
        if not self.ma_ensemble_periods:
            raise ValueError("ma_ensemble_periods must not be empty")
        if not 1 <= self.ma_exit_min_votes <= len(self.ma_ensemble_periods):
            raise ValueError("ma_exit_min_votes must be within [1, len(ensemble)]")
        if self.bias_fast_period >= self.bias_slow_period:
            raise ValueError("bias_fast_period must be shorter than bias_slow_period")

    @property
    def lookback_candles(self) -> int:
        """Lookback expressed as a 4H candle count (lookback_days x 6)."""
        return self.lookback_days * H4_CANDLES_PER_DAY


# A ready-made default instance for callers that do not need to override anything.
DEFAULT_STRATEGY_CONFIG = StrategyConfig()
