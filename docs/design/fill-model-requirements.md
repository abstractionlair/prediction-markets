# Fill Model Requirements

**Draft 2** | 2026-04-12

Parent: `infrastructure-vision.md` (Pillar 2: Models and Estimators)
Supersedes: `fill-model-v2-spec.md`, `fill-prediction-model-spec.md` (both designed
from suspect measurements and may inform future work but are not requirements)

## Why This Exists

The fill model is the bridge between "this trade looks profitable" and "this
trade will actually execute." Every strategy decision depends on it: which
markets to trade, what price to offer, how many contracts to place. If the
fill model is wrong, the strategy optimizes for a world that doesn't exist.

Previous fill models were designed and evaluated using measurements we now know
were flawed (temporal leakage in training data, +17pp simulator-to-reality bias,
model-simulator inconsistency). Rather than patch those models, we start from
requirements and build up from what we can verify.

## Two Interfaces, One Model

The fill model serves two consumers:

### Interface 1: Probability (for strategy decisions)

Given a proposed trade and current market state, return the probability the
order fills, conditioned on outcome.

```
predict(proposed_trade, market_state) -> (P(fill|pays_off), P(fill|~pays_off))
```

Here "fill" means: all Q requested contracts execute before settlement. This
is the probability that matters for the strategy's EV calculation, because
the strategy commits escrow for the full quantity at order time.

"Pays off" means the outcome favors our side: YES resolves YES for a YES
buyer, NO resolves NO for a NO buyer. The caller (EVStrategy) supplies
P(pays_off) from the EventRateEstimator.

The strategy uses these to compute expected PnL:

```
E[PnL] = P(win) * P(fill|win) * (100 - limit_price)   # payoff if filled and won
       - P(lose) * P(fill|lose) * limit_price           # loss if filled and lost
       - P(fill) * fee                                   # fee if filled either way
```

where P(fill) = P(win) * P(fill|win) + P(lose) * P(fill|lose), and all
amounts are in cents per contract.

This interface is used in live trading and in the strategy's scan() method
during replay.

**Partial fills:** The probability interface predicts full-quantity fill
probability. The strategy sizes orders based on this. If partial fills
are common for a given quantity, the model should reflect that by returning
a lower P(fill) for larger Q — the strategy will then choose a smaller Q.
The model is not required to predict expected fill quantity directly, but
the full-fill probability must decrease monotonically with quantity (all
else equal).

### Interface 2: Simulation (for backtesting)

Given a simulated order and historical market state, determine how many
contracts fill in each replay period.

```
simulate(order, market_state, period) -> additional_contracts_filled: int
```

This matches the Runner's FillSimulator protocol:
- `on_order(ticker, order, period)` — called when order is placed
- `check_fills(ticker, order, period)` — returns additional contracts
  filled this period

The simulation is incremental and period-based, not a one-shot boolean.
This matters because:
- **Capital lockup**: escrow is committed at placement but capital is only
  freed when the order fills or the market settles
- **Partial fills**: an order for 8 contracts might fill 3 in one period
  and 5 later (or never complete)
- **Opportunity cost**: capital tied in a pending order can't be used for
  other trades

The simulator must support delayed fills (order placed in period T, fills
in period T+k) and partial fills (fills accumulate across periods).

### Consistency Requirement

These are not independent systems. The simulator must draw from the model's
distribution. Specifically: over a large population of orders with predicted
P(fill|outcome) = p, the simulator should fill approximately fraction p
of those orders by settlement.

This is what makes backtesting trustworthy. If the probability model says
"orders like this fill 40% of the time when the outcome pays off," and the
simulator fills 40% of such orders, then the strategy's EV calculation
(which uses the probability) is consistent with the backtest's P&L (which
uses the simulator).

The consistency check must hold on relevant slices (by category, price
range, quantity, time-to-settlement), not only in global aggregate. A
model that is calibrated overall but systematically overpredicts for sports
and underpredicts for economic data is not consistent.

The previous system violated this: the GBT predicted conservatively
(queue-aware, ~40% fill rate), but the candle simulator filled generously
(~70% fill rate for the same orders). The strategy selected orders the
simulator would fill but reality wouldn't.

**Implementation note:** The simplest way to guarantee consistency is to
have the simulator call the probability model and do a Bernoulli draw.
Other approaches (tape-based, candle-based) must demonstrate calibration
against the probability model as a validation requirement.

## Inputs

Everything the model receives must be observable at order-placement time.
No settlement outcomes, no future prices, no future volume.

**Proposed trade** (the strategy's decision):
- side: yes or no
- limit_price: cents
- quantity: contracts

**Market state** (observable from API or data):
- bid, ask: current YES-side best bid/ask in cents
- hours_to_settlement: time remaining
- generating_process, topic: market classification
- trailing_volume: recent trading activity (proxy for liquidity)
- open_interest: resting order depth (proxy for queue)

These are the minimum required inputs. The model may accept additional
observable market state (book depth, bid/ask sizes, recent trade activity,
etc.) via the market_state dict. The interface should not be closed to
richer inputs — queue awareness was the decisive missing piece in previous
models, and better queue/liquidity features may emerge.

The model may use derived features (relative_price, spread, etc.) but the
raw inputs are the contract with the caller.

## Outputs

Two conditional probabilities:
- P(fill | pays_off): probability of fill given the outcome favors us
- P(fill | ~pays_off): probability of fill given the outcome is against us

Why conditioned on outcome? Because fills are not independent of outcomes.
A resting order at a favorable price fills MORE when the market moves
against the eventual outcome (adverse selection). This is not a modeling
choice — it's an empirical fact observed in the trade tape (sports markets
show P(fill|lost)/P(fill|won) = 1.2-1.5x).

## Properties

### Temporal honesty

The model must work within the feature framework. It receives data filtered
by ViewFactory (never sees future data), and the framework assigns its
availability_time. This is already enforced by the EstimatorFactory protocol
(Chunk 4-5). No special handling needed — just implement the protocol.

### Degrades gracefully with missing data

Not all markets have all inputs. Open interest coverage is sparse for some
categories. Trailing volume may be zero for inactive markets. The model
should return reasonable estimates when optional inputs are missing,
falling back to coarser predictions rather than refusing to predict.

The framework View's `fill_probability()` returns `FillEstimate | None`.
Returning None is acceptable when the model genuinely has no basis for
a prediction (e.g., a completely unknown category). But "open_interest
is missing" should trigger a fallback, not a None — the model should
integrate out the missing dimension.

The previous FlowModel had a 5-level fallback hierarchy for this. Whether
the solution is fallback, marginalization, or imputation is an architecture
decision, not a requirement.

### Accuracy is measurable

The model must have a self-contained validation that does not depend on the
trading strategy or replay engine:

1. Train/calibrate on data before a temporal split point
2. Predict fill probabilities on held-out data after the split
3. Compare predicted vs actual fill rates

Validation must include:
- **Calibration**: predicted vs actual fill rates, grouped by predicted
  probability (reliability diagram). Checked on relevant slices (category,
  price range, quantity), not only in global aggregate.
- **Discrimination**: the model must rank orders correctly. A proper scoring
  rule (Brier score or log-loss) on holdout, not just calibration.
- **Monotonicity**: P(fill) must decrease with quantity (all else equal)
  and increase with price aggressiveness (closer to ask).

The gap between predicted and actual, measured across temporal splits, is
the model's accuracy metric. This number must be known before the model
is used in production.

### Accuracy against reality

The model's ultimate ground truth is real orders, not simulated ones.
We have a growing set of real maker orders with known fill outcomes (76+
as of Apr 2026). Any fill model must be validated against this set.

Given the small sample, this validation should:
- Report confidence intervals, not point estimates
- Stratify by category/regime where sample permits
- Be treated as a high-signal smoke test, not the primary training signal

A model that predicts its own simulator perfectly but diverges from
real orders by 17pp (as the queue-aware GBT did) is not accurate. As
the real-order dataset grows, this validation becomes increasingly
definitive.

## Domain of Validity

These requirements are for a **Kalshi tail-price maker fill model** — 
predicting fills for resting limit orders at 85-97 cent prices on the
Kalshi exchange. The empirical evidence, adverse selection patterns, and
queue mechanics are specific to this context.

If the trading strategy expands to mid-range prices (50-80 cents), different
exchanges, or taker orders, the model will need re-evaluation. The
requirements themselves (two interfaces, consistency, temporal honesty) are
general, but the "What We Know" section below is regime-specific.

## What We Know (and don't)

Empirical findings from trade tape analysis that we believe are sound
(measured with outcome conditioning and temporal awareness):

**Sound:**
- Adverse selection is real and category-dependent (sports 1.2-1.5x, econ ~1.1x)
- Adverse selection worsens with order size
- NO side fills more than YES at tail prices (asymmetric taker flow — Kalshi-specific, driven by YES-biased mobile UI)
- Fill rates degrade gracefully with quantity (Q=20 retains ~90% of Q=1 rate)
- Price sensitivity is minimal across the 85-97 cent tail range
- Trailing volume is predictive of next-hour flow
- In-game sports orders (<3h to settlement) have severe adverse selection

**Suspect (measured with tools we now distrust):**
- Absolute fill rate levels (the +17pp simulator bias means all levels are off)
- Queue-aware alpha=0.3 coefficient (calibrated from 76 orders — small sample,
  single calibration, no temporal split)
- GBT feature importances (trained with temporal leakage)
- FlowModel CDF shapes (calibrated without queue awareness)
- Any replay P&L number from before the framework was built

## What This Doc Does Not Decide

- Model architecture (GBT, binned estimator, parametric, neural, etc.)
- Feature engineering (which derived features to use)
- Training procedure (how to generate training data)
- Simulator mechanism (Bernoulli, tape-based, candle-based)
- Whether to build one model or separate models for different categories

These are design decisions for a spec that implements these requirements.
