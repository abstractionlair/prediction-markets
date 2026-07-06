# Fill Model Spec: Expected Contracts Filled

**Date:** 2026-04-19
**Status:** Draft 2 (post-review)

## Purpose

Predict the expected number of contracts filled `E[K | features, pays_off]`
for a maker order placed at a given market state, for use in strategy EV
computation. Ground truth = event-driven fill simulator scored against the
historical trade tape, with Q_ahead observed from orderbook depth snapshots.

## Why E[K] (not hazard or binary classifier)

For risk-neutral EV only the first moment matters:

```
edge_win  = 100 − P − fee     (per contract, if pays_off=True)
edge_lose =    − P − fee       (per contract, if pays_off=False)

E[PnL] = P(pays_off) · E[K|pays_off]  · edge_win
       + P(~pays_off) · E[K|~pays_off] · edge_lose
```

Higher moments of K don't enter. A binary classifier on `fully_filled`
would lose partial-fill information (~9% of v2 maker orders). A hazard
model could be added later for capital lockup / variance modeling.

## Target: fill_fraction, not raw count

**Target: `fill_fraction = contracts_filled / quantity` ∈ [0, 1]**
(not raw `contracts_filled`).

Reviewers flagged that regressing on raw counts with Huber loss lets the
model's loss be dominated by large-quantity rows, and it ends up learning
`Q × global_mean_rate` rather than real microstructure.

At inference: `E[K] = quantity × predicted_fill_fraction`. Multiplying back
by quantity recovers the target we actually want.

**Loss: cross-entropy** (LightGBM `objective='cross_entropy'`). Natural
handling of continuous targets in [0,1]; no clipping needed.

## Training data

Source: `work/training_data_v2.csv` — 87K virtual orders from the depth era
(2026-04-12+). Filter: `would_cross_spread=False` (exclude marketable
orders). Expected training set: ~66K rows.

## Features

### Order
- `side` (categorical: yes/no)
- `quantity` and `log1p(quantity)` (tree splits prefer log-scale for count-like features)
- DROP: `limit_price_cents` (reviewer: noisy; absolute price is less informative
  than distance/queue features we already have)

### Queue state (from depth snapshot)
- `same_price_depth` (aka q_ahead)
- `better_price_depth`
- `is_level_populated` (boolean — captures the q_ahead=0 non-monotonicity)
- `is_new_best_price` (boolean)
- `gap_to_nearest_populated`
- `distance_to_touch`
- `is_at_touch` (boolean, `distance_to_touch == 0`)
- **Queue-relative size ratios** (new):
  - `qty_over_same_depth = quantity / (1 + same_price_depth)`
  - `qty_over_total_at_or_better = quantity / (1 + same_price_depth + better_price_depth)`
  These normalize our request size against the depth we'd have to clear.

### Market state
- `yes_bid`, `yes_ask`, `spread`
- `volume`, `open_interest`
- `snapshot_age_seconds`

### Recent activity
- `vol_5m_before`, `vol_30m_before`, `vol_2h_before`
- `time_since_trade_at_level_seconds`

### Time
- `hours_to_close` (raw)
- `lifecycle_fraction` (where in the market's life we placed the order;
  computed at sampling time)
- `day_of_week`, `hour_of_day` (UTC) — context for the single-day holdout

### Classification
- `generating_process`, `topic`, `payoff_type` (categorical)

### Outcome (for outcome-conditioning)
- `pays_off` (boolean) — one model, called twice at inference with both
  values.

### Explicitly excluded (leakage)
- `contracts_filled`, `fully_filled`, `fill_fraction` (target)
- `time_to_first_fill_seconds`, `time_to_full_fill_seconds` (outcome of simulation)
- `n_trades_in_horizon`, `num_fill_events` (outcome-dependent)
- `market_result` (the underlying outcome; use `pays_off` derived view)

## Model

**LightGBM regression, cross-entropy loss on fill_fraction.**

**Starting hyperparameters**:
- `objective='cross_entropy'`
- `num_leaves=63`, `learning_rate=0.05`
- `n_estimators=1000` with early stopping (patience=50)
- `min_data_in_leaf=50` (larger than v1 to control overfit on 66K rows)
- `feature_fraction=0.8`, `bagging_fraction=0.8`, `bagging_freq=5`

**Monotonic constraints** (sanity check; verify via held-out eval whether
they help or hurt):
- `log1p(quantity)`: should be flat or decreasing (larger orders fill less
  often as a fraction). Use constraint = -1 or 0.
- `qty_over_same_depth`: decreasing (−1) — being big relative to the queue
  means lower fill fraction.
- `qty_over_total_at_or_better`: decreasing (−1)

Apply monotonic constraints only after checking that the unconstrained
model shows the right sign. If adding the constraint costs held-out
performance, drop it.

**Serialization**: LightGBM's native `save_model` / `load_model`
(text format; no pickle).

## Validation strategy

**Rolling blocked CV + final temporal hold-out.**

Days in the depth era: 1-7 (2026-04-12 through 2026-04-18).

Rolling folds (model selection):
- Fold A: train [1-3], val [4]
- Fold B: train [1-4], val [5]
- Fold C: train [1-5], val [6]

Final test: train on [1-6] with best hyperparameters from rolling CV,
evaluate once on day 7. Single reported number.

Rationale: 7 days is too short for robust temporal validation (flagged by
both reviewers). Rolling CV measures stability across days; final test
catches leakage if hyperparameters aren't tuned on it.

**Plan to re-validate as depth data accumulates.** Target: ≥4 weeks of
depth data before live deployment.

## Metrics and success criteria

### Reported on day-7 test (single evaluation)

**Primary: calibration (stratified)**
Average predicted E[K] vs observed mean contracts_filled, within each
stratum. Strata:
- `pays_off` × quantity bucket (1, 5, 10, 25, 100) × side (yes/no)
- Additionally: distance-to-touch bucket (behind, at, inside)

Target: absolute error ≤ 10% of observed mean per stratum (with enough
support). Unsupported strata flagged.

**Secondary: discrimination**
- MAE on fill_fraction (not R²). Gemini: R² is poor for [0,1] with mass
  near boundaries.
- Baseline: per-quantity mean fill rate. Must beat baseline MAE by ≥15%.

**Tertiary: consistency with simulator**
Average model-predicted E[K] vs simulator average on a fresh seed of
virtual orders, within 5%. Stratified by generating_process.

**Outcome-conditional sign check**
`E[K|pays_off] - E[K|~pays_off]` must be negative (adverse selection)
on the test fold, consistent across generating_process classes. Positive
sign = investigate; probable bug.

### Minimum support for per-stratum reporting
Any stratum with fewer than 50 rows on the test fold: mark "low support,
not measured." Don't silently fail gates.

## Known deployment risks

Explicitly documented so they're not forgotten:

1. **Simulator understates adverse selection.** When toxic flow arrives,
   smart money ahead of us cancels, leaving us at the top of the book to
   absorb the loss. The static-queue simulator can't reproduce this. Real
   fill rates conditional on losing will be *higher* than this model
   predicts — our edge estimates are optimistic.

2. **No real-order ground truth yet.** The training labels come from
   simulator counterfactuals, not live orders. Must accumulate real
   orders (via the order_log table added in trader.py) and validate
   before trusting absolute EV.

3. **7-day training window.** May miss rare regimes (holidays, news,
   Sunday sports bursts). Re-train as data accumulates.

4. **Quantity extrapolation.** Training grid is {1, 5, 10, 25, 100}.
   Strategy must clip requested quantity to [1, 100] until coverage
   expands.

5. **Outcome-model error compounds.** The `pays_off` condition is
   supplied by a separate estimator (EventRateEstimator / binned P(YES)).
   A 5% error there, multiplied by a 37pp adverse-selection spread,
   produces meaningful EV bias. Monitor jointly.

6. **Point forecast, no uncertainty.** LightGBM point prediction has no
   natural confidence band. Consider quantile regression (or bootstrapped
   predictions) as a v2 improvement.

7. **Low-support strata in outcome-conditioning.** Some (ticker class,
   distance-to-touch, pays_off) cells may have thin training support.
   Predictions there are extrapolation. Flag via support checks at
   inference time (future work).

8. **Margin lockup not modeled.** A model predicting E[K]=5 on a
   100-contract request locks capital for days. The portfolio layer
   needs a time-in-market guard independent of this model.

## Integration plan

1. `research/fill_expected_fills.py` — training script. Loads v2 CSV,
   constructs features, runs rolling CV + final test, saves LightGBM
   booster via `save_model`. Outputs metrics report.

2. `trading/fill_expected_fills.py` — inference wrapper. Loads the
   saved booster and exposes `predict(features: dict, pays_off: bool)
   -> float` returning E[K].

3. `framework/` integration — FillModelFeature that loads the booster
   and is composed into the View. Replaces FillRateEstimator in the
   EV strategy path.

4. Backtest — rerun replay with the new fill model; compare predicted
   EV vs realized on held-out settled markets.

## Out of scope (future)

- Time-to-fill distribution (hazard model)
- Variance of K, quantile regression
- Dynamic order management (amend / cancel)
- Per-level queue dynamics within an order's life
- Multi-leg (paired YES+NO) orders
