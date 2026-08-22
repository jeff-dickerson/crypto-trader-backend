"""The paper loop: Gate 2's machine (Build Order step 4).

This package is the "prove the machine, not the edge" layer (PRD 6.2). It wires the
pure strategy core and the ExchangeAdapter seam into a runnable, approve-then-execute
paper-trading loop:

- risk.py           tiered risk sizing, the slider, and the kill-switch clamp (PRD 4.1/4.2)
- plan.py           the TradePlan a human approves, with its single human-facing risk number
- position_manager.py  signal consumption, plan sizing/approval/submission, and the
                       four-state position lifecycle driven by exchange-reported truth
- reconciliation.py  the drift check with a documented tolerance, freeze-on-drift
- loop.py           the PaperTrader orchestrator that ties adapter + manager + approval +
                    reconciliation together bar by bar

The DryRun (paper) ExchangeAdapter itself lives with the other adapters in
crypto_trader.exchange.dryrun; the approval channels live in crypto_trader.approval.
"""

from __future__ import annotations
