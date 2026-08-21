# Gate 1 Backtest Report

Generated at 2026-08-21T20:26:42.744055+00:00.

Data source: deterministic synthetic data (2.5 years, seed 12345).

This run uses SYNTHETIC data, so it proves only that the backtest lab runs end to end; it does not validate the trading edge.


## Gate 1 verdict

**NOT PROVEN (synthetic data)**

The data for this run is synthetic, so this run validates only that the Gate 1 harness works end to end, not the strategy edge.
Gate 1 remains not proven and requires a rerun on real ingested candles.


## Denominators (pinned, per PRD 6.1)

A setup that fires an entry order, an order that actually fills, and a trade that closes are three different counts, so each is reported separately and every statistic states which one it uses.

- Signals: setups that fired a resting entry order, after the funding and minimum-notional filters.
- Fills: signals whose resting limit actually traded through (modelled as full fills; partial fills need order-book depth the 4H/daily data plan does not carry).
- Closed trades: fills that reached a terminal exit; the expectancy denominator. Fills still open when the data ends are counted separately, never as closed trades.


## Run setup

- Symbols: SYN01USDT, SYN02USDT, SYN03USDT, SYN04USDT, SYN05USDT, SYN06USDT, SYN07USDT, SYN08USDT, SYN09USDT, SYN10USDT, SYN11USDT, SYN12USDT, SYN13USDT, SYN14USDT, SYN15USDT, SYN16USDT, SYN17USDT, SYN18USDT.

- Timeline: 2021-01-01T00:00:00+00:00 to 2023-11-20T00:00:00+00:00.

- In-sample / out-of-sample split at 2022-12-04T00:00:00+00:00 (first two-thirds tune, final third is the frozen score).

- The chosen combination is selected on IN-SAMPLE mean R only; its OUT-OF-SAMPLE figures are the honest gate number, and selecting on the out-of-sample score would be cheating.

- Costs (placeholders, MUST-VERIFY per PRD 9.3): maker 0.020%, taker 0.060%, slippage 0.050% on market exits, minimum notional 5.0, sizing at 2.0% risk on 2000.0 assumed equity.

- Funding: synthetic placeholder derived from 24h momentum, 8h interval, positive in rising markets so longs pay (PRD 9.3 MUST-VERIFY).


## Swept-parameter comparison

The lookback is swept inside the spec's 30-90 day bound and the captain-decision-4 funding filter is run both off and on; the chosen combination is marked with an asterisk.


| Lookback (d) | Funding filter | Signals | Fills | Closed | IS mean R | OOS mean R | OOS 95% CI (boot) | OOS excl. 0 | OOS win% | OOS payoff | OOS maxDD% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 45 | off | 180 | 175 | 175 | 0.370 | 0.369 | [0.043, 0.696] | yes | 67.5% | 1.04 | 4.3% |
| 45 | on | 86 | 84 | 84 | -0.531 | -0.745 | [-0.946, -0.516] | yes | 15.0% | 0.18 | 26.1% |
| 60 | off | 123 | 115 | 115 | 0.967 | 1.010 | [0.788, 1.236] | yes | 92.6% | 22.39 | 0.2% |
| 60 | on | 95 | 93 | 93 | 0.854 | 0.771 | [0.538, 1.005] | yes | 85.7% | 21.74 | 0.2% |
| 75 | off | 65 | 58 | 58 | 1.275 | 1.326 | [1.098, 1.545] | yes | 100.0% | n/a | 0.0% |
| 75 * | on | 57 | 51 | 51 | 1.276 | 1.227 | [0.963, 1.482] | yes | 100.0% | n/a | 0.0% |

## Chosen combination detail

Lookback 75 days, funding filter on, selected by in-sample mean R.

Whole-run funnel: 66 setups, 9 funding-filtered, 0 minimum-notional-voided, 57 signals, 51 fills, 51 closed, 0 still open at data end.


### In-sample (tuning window, not the gate)

- Closed trades (n): 41
- Mean expectancy: 1.276 R per closed trade
- Standard deviation: 0.510 R; standard error: 0.080 R; t-stat: 16.01
- 95% CI (bootstrap, seeded): [1.126, 1.426] R
- 95% CI (normal approx): [1.120, 1.433] R
- Confidence interval excludes zero: yes
- Win rate: 97.6%; payoff (avg win / avg loss): 94.19
- Total: 52.329 R; max drawdown: 0.014 R (0.0% compounding)
- Exit mix: {'stop': 0, 'trailing_stop': 0, 'take_profit': 7, 'momentum_shift': 34}


### Out-of-sample (frozen score, the gate)

- Closed trades (n): 10
- Mean expectancy: 1.227 R per closed trade
- Standard deviation: 0.436 R; standard error: 0.138 R; t-stat: 8.91
- 95% CI (bootstrap, seeded): [0.963, 1.482] R
- 95% CI (normal approx): [0.957, 1.497] R
- Confidence interval excludes zero: yes
- Win rate: 100.0%; payoff (avg win / avg loss): n/a
- Total: 12.270 R; max drawdown: 0.000 R (0.0% compounding)
- Exit mix: {'stop': 0, 'trailing_stop': 0, 'take_profit': 1, 'momentum_shift': 9}

