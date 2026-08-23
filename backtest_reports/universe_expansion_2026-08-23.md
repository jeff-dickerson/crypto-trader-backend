# Universe Expansion: Gate 1 Re-run on an Enlarged Sample

Generated at 2026-08-23T17:54:26.020763+00:00.

This is a data-expansion study, not a strategy change.
The strategy core, cost model, and Gate 1 methodology (in-sample/out-of-sample split, freeze-on-final-third, select-on-in-sample-only) are the unmodified versions used in `gate1_real_2026-08-21.md`.
StrategyConfig was not touched.
The question is narrow: does the prior negative real-data result hold up on a larger, more statistically meaningful sample?

## 1. The rule-defined universe selector

The universe is now rule-defined and self-updating (built in `crypto_trader.ingest.universe`), replacing the prior hand-picked 14-symbol list.
The rule, run against Bitunix's public market-data endpoints, is applied in order:

1. Tradability: symbolStatus OPEN, isApiSupported true, quoted in USDT.
2. 24h volume floor: public 24h quoteVol at or above 2,000,000 USDT.
3. Order-book depth floor: summed bid+ask notional within +-0.5% of mid at or above 50,000 USDT (depth endpoint, 50 levels).

Funnel on 2026-08-23: 724 listed pairs, 625 tradable USDT perps, 61 clear the volume floor, 60 also clear the depth floor.

- Live trading universe (spec's top 10-15): the top 15 by 24h volume: ETHUSDT, BTCUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, TRUMPUSDT, HYPEUSDT, ENAUSDT, TUTUSDT, PUMPFUNUSDT, BNBUSDT, 1000PEPEUSDT, AAVEUSDT, SUIUSDT, LINKUSDT.
- Research universe (this study's N): all 60 survivors.
- The larger research N is a backtest-research decision only; the live trading universe size stays the separate captain decision it always was.

- Cleared volume but failed depth (excluded): TACUSDT (depth 18,424).

## 2. Data sources

- Bitunix (live venue, source of truth): the full research universe ingested from Bitunix's own public kline endpoint, at full reachable depth, the live-representative dataset and the direct larger-N comparison to the prior 14-symbol run.
- Binance spot (RESEARCH ONLY, PRD section 10): the 45 of 60 universe symbols available on Binance spot, ingested from the public data-vision mirror with deep multi-year history.
- Binance's futures API is geo-blocked (HTTP 451) from this environment, the spot mirror is not, and spot OHLCV of these majors is a faithful proxy (see the parity check below).
- The Binance source is import-isolated from every live/paper path, proven by `tests/test_research_source_isolation.py`.

- Binance-missing universe symbols (15, futures-only or scaled names): HYPEUSDT, PUMPFUNUSDT, 1000PEPEUSDT, FARTCOINUSDT, 1000BONKUSDT, 1000SHIBUSDT, LIGHTERUSDT, SPCXUSDT, BEATUSDT, BTWUSDT, UAIUSDT, KORUUSDT, SNXXUSDT, MELANIAUSDT, AKEUSDT.

## 3. Cross-source parity spot-check (BTCUSDT 4H)

The design review flagged a parity risk if two sources disagree.
Comparing every overlapping BTCUSDT 4H candle in the two ingested DBs (9505 bars):

- open: mean |diff| 0.0447%, max |diff| 1.8749%.

- high: mean |diff| 0.0467%, max |diff| 3.6759%.

- low: mean |diff| 0.0549%, max |diff| 2.7181%.

- close: mean |diff| 0.0447%, max |diff| 1.8750%.

Agreement is well within a fraction of a percent on the body of the bar.
The only larger gaps are occasional single-wick extremes, the expected microstructure difference between a perp and its spot index.
Binance spot is a sound deep-history proxy for this study.

## 4. Run A: Bitunix-native, expanded universe

Data source: real ingested candles from Bitunix for 60 symbols.

- Symbols with data: 60.

- 4H history span across the universe: 2022-04-17 to 2026-08-23.

- Timeline: 2022-04-17T16:00:00+00:00 to 2026-08-23T16:00:00+00:00.

- In-sample / out-of-sample split at 2025-03-12T00:00:00+00:00 (first two-thirds tune, final third is the frozen score).


**Gate 1 verdict: NOT PASSED**

Gate 1 is not passed because the out-of-sample confidence interval does not exclude zero on the positive side; the out-of-sample max drawdown (62.5%) breaches the 15% bar.


### Swept-parameter comparison

| Lookback (d) | Funding filter | Signals | Fills | Closed | IS mean R | OOS mean R | OOS 95% CI (boot) | OOS excl. 0 | OOS win% | OOS payoff | OOS maxDD% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 45 * | off | 508 | 422 | 422 | -0.245 | -0.239 | [-0.342, -0.125] | yes | 33.2% | 0.90 | 62.5% |
| 45 | on | 508 | 422 | 422 | -0.245 | -0.239 | [-0.342, -0.125] | yes | 33.2% | 0.90 | 62.5% |
| 60 | off | 224 | 176 | 176 | -0.288 | -0.273 | [-0.426, -0.109] | yes | 33.0% | 0.84 | 40.5% |
| 60 | on | 224 | 176 | 176 | -0.288 | -0.273 | [-0.426, -0.109] | yes | 33.0% | 0.84 | 40.5% |
| 75 | off | 171 | 131 | 131 | -0.350 | -0.230 | [-0.386, -0.056] | yes | 36.5% | 0.66 | 31.0% |
| 75 | on | 171 | 131 | 131 | -0.350 | -0.230 | [-0.386, -0.056] | yes | 36.5% | 0.66 | 31.0% |


### Chosen combination detail (lookback 45 d, funding filter off, selected in-sample)

Whole-run funnel: 508 setups, 0 funding-filtered, 0 minimum-notional-voided, 508 signals, 422 fills, 422 closed, 0 still open at data end.


### In-sample (tuning window, not the gate)

- Closed trades (n): 223
- Mean expectancy: -0.245 R per closed trade
- Standard deviation: 0.748 R; standard error: 0.050 R; t-stat: -4.89
- 95% CI (bootstrap, seeded): [-0.345, -0.145] R
- 95% CI (normal approx): [-0.343, -0.147] R
- Confidence interval excludes zero: yes
- Win rate: 37.2%; payoff (avg win / avg loss): 0.72
- Total: -54.613 R; max drawdown: 54.674 R (67.4% compounding)
- Exit mix: {'stop': 73, 'trailing_stop': 0, 'take_profit': 9, 'momentum_shift': 141}



### Out-of-sample (frozen score, the gate)

- Closed trades (n): 199
- Mean expectancy: -0.239 R per closed trade
- Standard deviation: 0.789 R; standard error: 0.056 R; t-stat: -4.28
- 95% CI (bootstrap, seeded): [-0.342, -0.125] R
- 95% CI (normal approx): [-0.349, -0.130] R
- Confidence interval excludes zero: yes
- Win rate: 33.2%; payoff (avg win / avg loss): 0.90
- Total: -47.655 R; max drawdown: 47.655 R (62.5% compounding)
- Exit mix: {'stop': 61, 'trailing_stop': 0, 'take_profit': 10, 'momentum_shift': 128}


### Per-symbol 4H coverage

| Symbol | 4H candles | oldest | newest |
|---|---|---|---|
| ETHUSDT | 9505 | 2022-04-17 | 2026-08-23 |
| BTCUSDT | 9505 | 2022-04-17 | 2026-08-23 |
| SOLUSDT | 9458 | 2022-04-17 | 2026-08-23 |
| XRPUSDT | 9488 | 2022-04-17 | 2026-08-23 |
| DOGEUSDT | 9476 | 2022-04-17 | 2026-08-23 |
| TRUMPUSDT | 3457 | 2025-01-18 | 2026-08-23 |
| HYPEUSDT | 3061 | 2025-03-27 | 2026-08-23 |
| ENAUSDT | 5149 | 2024-04-09 | 2026-08-23 |
| TUTUSDT | 3079 | 2025-03-20 | 2026-08-23 |
| PUMPFUNUSDT | 2411 | 2025-07-13 | 2026-08-23 |
| BNBUSDT | 9464 | 2022-04-17 | 2026-08-23 |
| 1000PEPEUSDT | 5730 | 2024-01-01 | 2026-08-23 |
| AAVEUSDT | 6680 | 2023-07-25 | 2026-08-23 |
| SUIUSDT | 6666 | 2023-07-31 | 2026-08-23 |
| LINKUSDT | 6307 | 2023-09-21 | 2026-08-23 |
| ADAUSDT | 6352 | 2023-09-21 | 2026-08-23 |
| UNIUSDT | 9459 | 2022-04-17 | 2026-08-23 |
| NEARUSDT | 6707 | 2023-07-25 | 2026-08-23 |
| TAOUSDT | 5072 | 2024-04-19 | 2026-08-23 |
| FARTCOINUSDT | 3534 | 2025-01-06 | 2026-08-23 |
| ZROUSDT | 4621 | 2024-07-02 | 2026-08-23 |
| ONDOUSDT | 5625 | 2024-01-20 | 2026-08-23 |
| BCHUSDT | 6505 | 2023-08-19 | 2026-08-23 |
| XLMUSDT | 9439 | 2022-04-17 | 2026-08-23 |
| WLDUSDT | 6291 | 2023-09-30 | 2026-08-23 |
| STXUSDT | 6656 | 2023-07-27 | 2026-08-23 |
| PENGUUSDT | 3641 | 2024-12-18 | 2026-08-23 |
| AVAXUSDT | 9438 | 2022-04-17 | 2026-08-23 |
| XPLUSDT | 2164 | 2025-08-25 | 2026-08-23 |
| 1000BONKUSDT | 5287 | 2024-03-13 | 2026-08-23 |
| 1000SHIBUSDT | 5730 | 2024-01-01 | 2026-08-23 |
| BOMEUSDT | 5279 | 2024-03-16 | 2026-08-23 |
| LIGHTERUSDT | 1389 | 2025-12-31 | 2026-08-23 |
| LTCUSDT | 9474 | 2022-04-17 | 2026-08-23 |
| DOTUSDT | 9472 | 2022-04-17 | 2026-08-23 |
| ETCUSDT | 9441 | 2022-04-17 | 2026-08-23 |
| WLFIUSDT | 2163 | 2025-08-24 | 2026-08-23 |
| ASTERUSDT | 2004 | 2025-09-19 | 2026-08-23 |
| SPCXUSDT | 549 | 2026-05-22 | 2026-08-23 |
| ICPUSDT | 6201 | 2023-09-28 | 2026-08-23 |
| HBARUSDT | 5732 | 2024-01-01 | 2026-08-23 |
| FILUSDT | 9476 | 2022-04-17 | 2026-08-23 |
| ETHFIUSDT | 5211 | 2024-03-29 | 2026-08-23 |
| POLUSDT | 4142 | 2024-09-20 | 2026-08-23 |
| WIFUSDT | 5628 | 2024-01-18 | 2026-08-23 |
| BEATUSDT | 1681 | 2025-11-14 | 2026-08-23 |
| BTWUSDT | 455 | 2026-06-08 | 2026-08-23 |
| CRVUSDT | 9469 | 2022-04-17 | 2026-08-23 |
| COTIUSDT | 4374 | 2024-08-14 | 2026-08-23 |
| PENDLEUSDT | 5703 | 2024-01-01 | 2026-08-23 |
| UAIUSDT | 1708 | 2025-11-07 | 2026-08-23 |
| FFUSDT | 1937 | 2025-09-29 | 2026-08-23 |
| INJUSDT | 6030 | 2023-11-09 | 2026-08-23 |
| FETUSDT | 5727 | 2024-01-01 | 2026-08-23 |
| KORUUSDT | 187 | 2026-07-23 | 2026-08-23 |
| MORPHOUSDT | 3741 | 2024-11-27 | 2026-08-23 |
| PORTALUSDT | 3515 | 2025-01-08 | 2026-08-23 |
| SNXXUSDT | 186 | 2026-07-23 | 2026-08-23 |
| MELANIAUSDT | 3422 | 2025-01-20 | 2026-08-23 |
| AKEUSDT | 1933 | 2025-09-30 | 2026-08-23 |

## 5. Run B: Binance-spot deep history (research)

Data source: Binance spot (data-vision) for 45 symbols, deep history.

RESEARCH / BACKTEST ONLY: this dataset comes from Binance spot via the public data-vision mirror and must never feed a live or paper decision (PRD section 10).
It exists only to widen the Gate 1 sample with deep, multi-regime history.

- Symbols with data: 45.

- 4H history span across the universe: 2017-08-17 to 2026-08-23.

- Timeline: 2017-08-17T04:00:00+00:00 to 2026-08-23T16:00:00+00:00.

- In-sample / out-of-sample split at 2023-08-21T20:00:00+00:00 (first two-thirds tune, final third is the frozen score).


**Gate 1 verdict: NOT PASSED**

Gate 1 is not passed because the out-of-sample closed-trade count (62) is below the 150-signal target; the out-of-sample confidence interval does not exclude zero on the positive side; the out-of-sample max drawdown (35.1%) breaches the 15% bar.


### Swept-parameter comparison

| Lookback (d) | Funding filter | Signals | Fills | Closed | IS mean R | OOS mean R | OOS 95% CI (boot) | OOS excl. 0 | OOS win% | OOS payoff | OOS maxDD% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 45 | off | 759 | 625 | 625 | -0.270 | -0.243 | [-0.330, -0.157] | yes | 37.0% | 0.73 | 75.6% |
| 45 | on | 759 | 625 | 625 | -0.270 | -0.243 | [-0.330, -0.157] | yes | 37.0% | 0.73 | 75.6% |
| 60 | off | 287 | 214 | 214 | -0.294 | -0.218 | [-0.373, -0.049] | yes | 42.9% | 0.69 | 34.1% |
| 60 | on | 287 | 214 | 214 | -0.294 | -0.218 | [-0.373, -0.049] | yes | 42.9% | 0.69 | 34.1% |
| 75 * | off | 197 | 150 | 150 | -0.228 | -0.342 | [-0.513, -0.151] | yes | 29.0% | 0.76 | 35.1% |
| 75 | on | 197 | 150 | 150 | -0.228 | -0.342 | [-0.513, -0.151] | yes | 29.0% | 0.76 | 35.1% |


### Chosen combination detail (lookback 75 d, funding filter off, selected in-sample)

Whole-run funnel: 197 setups, 0 funding-filtered, 0 minimum-notional-voided, 197 signals, 150 fills, 150 closed, 0 still open at data end.


### In-sample (tuning window, not the gate)

- Closed trades (n): 88
- Mean expectancy: -0.228 R per closed trade
- Standard deviation: 0.851 R; standard error: 0.091 R; t-stat: -2.51
- 95% CI (bootstrap, seeded): [-0.399, -0.041] R
- 95% CI (normal approx): [-0.405, -0.050] R
- Confidence interval excludes zero: yes
- Win rate: 39.8%; payoff (avg win / avg loss): 0.77
- Total: -20.043 R; max drawdown: 21.121 R (35.3% compounding)
- Exit mix: {'stop': 34, 'trailing_stop': 0, 'take_profit': 4, 'momentum_shift': 50}



### Out-of-sample (frozen score, the gate)

- Closed trades (n): 62
- Mean expectancy: -0.342 R per closed trade
- Standard deviation: 0.764 R; standard error: 0.097 R; t-stat: -3.52
- 95% CI (bootstrap, seeded): [-0.513, -0.151] R
- 95% CI (normal approx): [-0.532, -0.152] R
- Confidence interval excludes zero: yes
- Win rate: 29.0%; payoff (avg win / avg loss): 0.76
- Total: -21.206 R; max drawdown: 21.206 R (35.1% compounding)
- Exit mix: {'stop': 24, 'trailing_stop': 0, 'take_profit': 3, 'momentum_shift': 35}


### Per-symbol 4H coverage

| Symbol | 4H candles | oldest | newest |
|---|---|---|---|
| ETHUSDT | 19745 | 2017-08-17 | 2026-08-23 |
| BTCUSDT | 19745 | 2017-08-17 | 2026-08-23 |
| SOLUSDT | 13221 | 2020-08-11 | 2026-08-23 |
| XRPUSDT | 18191 | 2018-05-04 | 2026-08-23 |
| DOGEUSDT | 15635 | 2019-07-05 | 2026-08-23 |
| TRUMPUSDT | 3488 | 2025-01-19 | 2026-08-23 |
| ENAUSDT | 5240 | 2024-04-02 | 2026-08-23 |
| TUTUSDT | 3083 | 2025-03-27 | 2026-08-23 |
| BNBUSDT | 19260 | 2017-11-06 | 2026-08-23 |
| AAVEUSDT | 12832 | 2020-10-15 | 2026-08-23 |
| SUIUSDT | 7249 | 2023-05-03 | 2026-08-23 |
| LINKUSDT | 16653 | 2019-01-16 | 2026-08-23 |
| ADAUSDT | 18294 | 2018-04-17 | 2026-08-23 |
| UNIUSDT | 13000 | 2020-09-17 | 2026-08-23 |
| NEARUSDT | 12837 | 2020-10-14 | 2026-08-23 |
| TAOUSDT | 5185 | 2024-04-11 | 2026-08-23 |
| ZROUSDT | 4765 | 2024-06-20 | 2026-08-23 |
| ONDOUSDT | 2995 | 2025-04-11 | 2026-08-23 |
| BCHUSDT | 14761 | 2019-11-28 | 2026-08-23 |
| XLMUSDT | 18029 | 2018-05-31 | 2026-08-23 |
| WLDUSDT | 6758 | 2023-07-24 | 2026-08-23 |
| STXUSDT | 14966 | 2019-10-25 | 2026-08-23 |
| PENGUUSDT | 3685 | 2024-12-17 | 2026-08-23 |
| AVAXUSDT | 12969 | 2020-09-22 | 2026-08-23 |
| XPLUSDT | 1993 | 2025-09-25 | 2026-08-23 |
| BOMEUSDT | 5341 | 2024-03-16 | 2026-08-23 |
| LTCUSDT | 19038 | 2017-12-13 | 2026-08-23 |
| DOTUSDT | 13175 | 2020-08-18 | 2026-08-23 |
| ETCUSDT | 17959 | 2018-06-12 | 2026-08-23 |
| WLFIUSDT | 2137 | 2025-09-01 | 2026-08-23 |
| ASTERUSDT | 1927 | 2025-10-06 | 2026-08-23 |
| ICPUSDT | 11584 | 2021-05-11 | 2026-08-23 |
| HBARUSDT | 15122 | 2019-09-29 | 2026-08-23 |
| FILUSDT | 12828 | 2020-10-15 | 2026-08-23 |
| ETHFIUSDT | 5329 | 2024-03-18 | 2026-08-23 |
| POLUSDT | 4256 | 2024-09-13 | 2026-08-23 |
| WIFUSDT | 5407 | 2024-03-05 | 2026-08-23 |
| CRVUSDT | 13197 | 2020-08-15 | 2026-08-23 |
| COTIUSDT | 14221 | 2020-02-26 | 2026-08-23 |
| PENDLEUSDT | 6884 | 2023-07-03 | 2026-08-23 |
| FFUSDT | 1969 | 2025-09-29 | 2026-08-23 |
| INJUSDT | 12795 | 2020-10-21 | 2026-08-23 |
| FETUSDT | 16395 | 2019-02-28 | 2026-08-23 |
| MORPHOUSDT | 1945 | 2025-10-03 | 2026-08-23 |
| PORTALUSDT | 5438 | 2024-02-29 | 2026-08-23 |

## 6. Honest finding

Prior run (14 symbols, Bitunix, gate1_real_2026-08-21): OOS n=33, mean -0.163R, 95% CI [-0.448, 0.156], not passed on both the count bar and the CI bar.

- Run A (Bitunix, 60 symbols): OOS n=199, mean -0.239R, 95% CI [-0.342, -0.125], win 33.2%, payoff 0.90, maxDD 62.5%.

- Run B (Binance deep, 45 symbols): OOS n=62, mean -0.342R, 95% CI [-0.513, -0.151], win 29.0%, payoff 0.76, maxDD 35.1%.

The prior run's ambiguity is resolved, and not in the strategy's favour.
At 14 symbols the out-of-sample sample was too thin (n=33) to separate a weak negative point estimate from noise, so its confidence interval straddled zero.
Expanding the universe removes that ambiguity: every swept combination in both runs now produces an out-of-sample confidence interval that excludes zero, and in every single case it excludes zero on the NEGATIVE side.
Run A reaches n=199 out-of-sample closed trades, clearing the 150-300 count target for the first time on real data, and still lands at -0.239R with a 95% CI of [-0.342, -0.125].
So the structural signal-count shortfall PRD 6.2 flagged as one of the two open constraints is now closed, and what the larger sample reveals underneath it is a confidently negative edge, not a hidden positive one.
Run B, on nine years of deep Binance-spot history spanning multiple market regimes, agrees: its largest combination (lookback 45) scores -0.243R over 625 out-of-sample trades with a CI of [-0.330, -0.157], and the in-sample-selected combination (lookback 75) scores -0.342R over 62 trades with a CI of [-0.513, -0.151].
The deep-history run also shows the sign is not an artifact of the recent 2025-2026 regime, since the negative expectancy holds across a 2023-2026 out-of-sample window that includes distinct up, down, and chop phases.
The funding filter changed nothing (its off and on rows are identical in every combination), consistent with the prior runs: no entry in this universe was with-bias-adverse beyond the 0.05%-per-8h threshold.
The out-of-sample max drawdown is severe in every combination (31% to 76%), which is the expected consequence of compounding a negative-expectancy edge across hundreds of trades at a fixed 2% risk, not an independent finding.
The joint problem is a sub-1.0 payoff (0.66 to 0.90) paired with a 29% to 37% win rate, and the momentum-shift exit still dominates the exit mix exactly as the retune investigation found, consistent with weak entries rather than premature exits.

Conclusion: the negative real-data result holds up, and hardens, at scale.
This is a Gate 1 fail for the strategy as currently parameterized, now on a large, out-of-sample, multi-venue, multi-regime sample rather than a thin one, and it is a strategy finding, not a harness defect or a data-adequacy problem.
Of the two constraints PRD 6.2 left open, the structural signal-count shortfall is resolved (n is ample at larger N) and entry predictiveness is confirmed as the binding one.
Per captain decision 5, changing StrategyConfig to chase a pass is a captain decision and not a unilateral edit, so this study deliberately did not touch it.
The candidate next steps remain a captain call: a materially different entry rule, a different strategy, or accepting that this parameterization does not clear Gate 1.
