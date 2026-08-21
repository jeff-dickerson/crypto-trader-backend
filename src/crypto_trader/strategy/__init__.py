"""Pure strategy core: Volume Profile POC Zone Retest (HTF-gated), v1.

This package is Build Order step 2: pure, deterministic decision logic only. It builds
no backtest replay engine, no paper/live adapter, and no interface (all future steps).

Public surface:
- generate_signal: the pure decision core.
- Signal, SignalAction: its explicit result type.
- PositionState, PositionStatus, PositionSide: the position-state contract it consumes.
- Bias, compute_bias: the daily bias gate.
- build_volume_profile, VolumeProfile, VolumeBin: the volume profile.
- StrategyConfig, DEFAULT_STRATEGY_CONFIG: every pinned, overridable parameter.
"""

from __future__ import annotations

from crypto_trader.strategy.bias import Bias, compute_bias
from crypto_trader.strategy.config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from crypto_trader.strategy.position import (
    PositionSide,
    PositionState,
    PositionStatus,
)
from crypto_trader.strategy.signal import Signal, SignalAction, generate_signal
from crypto_trader.strategy.volume_profile import (
    VolumeBin,
    VolumeProfile,
    build_volume_profile,
)
from crypto_trader.strategy.zones import EntrySetup, detect_entry_setup

__all__ = [
    "Bias",
    "compute_bias",
    "DEFAULT_STRATEGY_CONFIG",
    "StrategyConfig",
    "PositionSide",
    "PositionState",
    "PositionStatus",
    "Signal",
    "SignalAction",
    "generate_signal",
    "VolumeBin",
    "VolumeProfile",
    "build_volume_profile",
    "EntrySetup",
    "detect_entry_setup",
]
