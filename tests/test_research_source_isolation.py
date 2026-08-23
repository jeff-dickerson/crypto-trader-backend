"""Guard: the research-only Binance source can never reach a live/paper code path.

PRD section 10 permits a deep-history supplement from a major venue for research/backtest
ONLY, never for live decisions. This test statically proves the boundary: no module in any
live or paper trading package may import or even name the Binance source. If a future change
wires Binance into one of these packages, this test fails loudly.

The check is a source-text scan (not a runtime import graph) so it catches the reference even
behind a lazy import or a string, and needs no network or execution of the live code.
"""

from __future__ import annotations

from pathlib import Path

import crypto_trader

# Packages that make or drive real/paper trading decisions. The backtest package is
# deliberately NOT here: it reads candles from a database and is the one legitimate consumer
# of research data.
LIVE_PACKAGES = ("paper", "exchange", "safety", "approval", "api")

# Tokens that would indicate a live/paper module reaching for Binance data.
FORBIDDEN_TOKENS = ("binance_source", "BinanceUSDMCandleSource", "binance")

_SRC_ROOT = Path(crypto_trader.__file__).parent


def _python_files(package: str) -> list[Path]:
    return sorted((_SRC_ROOT / package).rglob("*.py"))


def test_no_live_or_paper_module_references_binance():
    offenders: list[str] = []
    for package in LIVE_PACKAGES:
        for path in _python_files(package):
            text = path.read_text(encoding="utf-8").lower()
            for token in FORBIDDEN_TOKENS:
                if token.lower() in text:
                    offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}: contains {token!r}")
    assert not offenders, (
        "Binance is research/backtest-only and must not be reachable from a live/paper "
        "path:\n" + "\n".join(offenders)
    )


def test_live_packages_exist_so_the_guard_is_real():
    # Guard against a silent typo making the scan vacuously pass.
    for package in LIVE_PACKAGES:
        assert (_SRC_ROOT / package).is_dir(), f"expected package {package} to exist"
        assert _python_files(package), f"expected .py files under {package}"
