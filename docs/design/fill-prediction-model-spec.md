# Fill Prediction Model — Spec

## Purpose

Predict the probability that a proposed resting limit order will be filled, conditioned on whether the market outcome pays off or not. Used by the trader to compute EV and decide whether to place an order.

## Interface

**Inputs** — all known at trade decision time:

| Input | Type | Source at trade time | Source at calibration time |
|-------|------|---------------------|--------------------------|
| side | yes/no | trader decision | simulated |
| limit_price | cents | trader decision | simulated |
| quantity | contracts | trader decision | simulated |
| bid | cents | API | candle data |
| ask | cents | API | candle data |
| hours_to_settlement | float | API (expiration time) | computed from settlement |
| generating_process | string | classification | classification |
| topic | string | classification | classification |
| trailing_volume_24h | int | API (volume_24h) | trade tape |
| open_interest | int | API | candle data |

Derived inputs (computed from the above, not passed separately):
- `relative_price = (limit_price - bid) / (ask - bid)` — 0.0 at bid, 1.0 at ask
- `spread = ask - bid`

For NO side, the book is flipped before computing relative_price: `bid, ask = 100 - ask, 100 - bid`. This makes relative_price mean "how aggressive is this order" regardless of side.

**Outputs:**
- `P(fill | pays_off)` — probability the order fills given the outcome is in our favor
- `P(fill | ¬pays_off)` — probability the order fills given the outcome is against us

The caller supplies `P(pays_off)` from the EventRateEstimator and reconstructs the full joint:

```
P(fill ∧ pays_off)     = P(fill | pays_off)  × P(pays_off)
P(fill ∧ ¬pays_off)    = P(fill | ¬pays_off) × P(¬pays_off)
P(¬fill ∧ pays_off)    = (1 - P(fill | pays_off))  × P(pays_off)
P(¬fill ∧ ¬pays_off)   = (1 - P(fill | ¬pays_off)) × P(¬pays_off)
```

**Missing inputs:** Any input may be unavailable. The model integrates it out (falls back to a coarser bin that marginalizes over that dimension).

## Calibration

The simulator is the ground truth. For each settled market with trade tape and candle data:

1. At each hourly candle, read (bid, ask, open_interest, trailing_volume_24h)
2. For each (side, relative_price_step, quantity_step), simulate a virtual order:
   - Compute absolute limit_price from relative_price and bid/ask
   - Walk the trade tape from placement to settlement
   - Count opposing flow; record whether flow ≥ quantity
3. Record (all_inputs, filled_or_not, paid_off_or_not) as one observation

Use all available tickers. Use all available trades. Burn the CPU.

## Model architecture

Gradient boosted trees (two models):
- Model A: trained on observations where pays_off=True, predicts P(fill | pays_off)
- Model B: trained on observations where pays_off=False, predicts P(fill | ¬pays_off)

Features: relative_price, quantity, hours_to_settlement, generating_process (categorical), topic (categorical), trailing_volume_24h, open_interest, spread.

Side is folded into relative_price via the bid/ask flip — not a separate feature.

## Validation

Self-contained test that does not depend on the trading engine or replay:

1. Split tickers into calibration set and holdout set
2. Generate training data from simulator on calibration tickers
3. Train model
4. Generate test data from simulator on holdout tickers
5. Predict with model on test data
6. Aggregate and compare predicted vs actual fill rates — by category, relative_price, volume, quantity, etc.

This runs as a standard test. The model must predict its own simulator on held-out data within acceptable tolerances before it can be used in production.
