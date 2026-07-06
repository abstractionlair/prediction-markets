# Infrastructure Vision

**Draft 3** | 2026-04-04

## Purpose

A trading system where data sources, models, and strategies can be added, modified, and validated through repeatable processes — not bespoke wiring. The infrastructure enforces correctness properties structurally, so that individual components can't silently violate them.

## Properties the Infrastructure Enforces

These are invariants the system maintains regardless of which specific data sources, models, or strategies are plugged in.

### Temporal Discipline

Every calibrated component shares a transitive temporal boundary defined by an `as_of` date (the point in time up to which data is visible). A strategy evaluated at time T uses only estimators calibrated on data from before T. An estimator calibrated at time T uses only observations from before T. This is enforced by the data structures — downstream consumers physically cannot access data they shouldn't have because upstream filters it before they ever see it.

The infrastructure must enforce temporal boundaries even for models that are expensive to retrain. A model with no declared or enforced training boundary is temporal leakage by construction — there must be no path by which such a model reaches production.

### Cost Realism

Every evaluation of a strategy includes realistic transaction costs. An opportunity that looks profitable before fees must still look profitable after fees. This applies equally in backtesting and in live evaluation — the cost model is not optional in either path. This is the property that revealed the original FLB strategy's edge was largely illusory.

### Validation Before Deployment

No model or strategy reaches production without passing through a validation gate. For models: temporal split validation (train on pre-cutoff, test on post-cutoff) with a recorded gap metric (predicted vs. observed accuracy). For strategies: out-of-sample replay producing a TrackRecord (a structured record of trades, returns, and calibration metrics) with known performance characteristics. The infrastructure makes it easy to validate and hard to skip.

### Declared Dependencies

What data a model needs, what models a strategy uses, what temporal constraints apply — these relationships are declared and inspectable, not implicit in import chains and SQL queries. When something changes upstream, the impact is traceable.

## Four Pillars

### 1. Data

Data sources feed into the system through collectors and bulk downloads. Each source has declared metadata: what it contains, its coverage window, how fresh it is, who owns it, and what depends on it.

**Principles** (from `data-ingestion-vision.md`): Historical API data is the source of truth. Live collectors fill the gap to the present. The splice (the join between historical backfill and live-collected data) is transparent to consumers. Adding a new data type follows an established pattern. Store the most granular resolution available.

Adding a new data source follows a standardized registration pattern and requires no changes to downstream code until a consumer explicitly opts in.

### 2. Models and Estimators

Models and estimators transform data into predictions — event probabilities, fill rates, implied distributions. Each has a defined interface: given data up to time T, produce estimates for time T onward.

The infrastructure provides a standard lifecycle: train (or retrain) on temporal-boundary-respecting data, validate against a holdout period, deploy through MarketView (the temporal gateway that provisions estimators and serves their outputs to strategies), monitor for degradation. This lifecycle applies whether the model is a simple binned estimator that retrains in seconds or a gradient-boosted tree that takes minutes.

Base interfaces define what it means to be an estimator in this system. Concrete implementations vary; the contract is uniform.

### 3. Strategies

Strategies consume estimates from models (via MarketView) and produce trading opportunities. They are pure functions of their inputs — no direct data access, no side effects.

A base interface defines the strategy contract: receive market state and a view, return scored opportunities. The same strategy implementation runs in backtesting and in production. The runner (replay engine or live trader) provides the environment; the strategy provides the logic.

This separation means strategies are independently testable: give them synthetic market data and a mock view, verify they produce correct output. No API, no database, no network.

### 4. Testing and Evaluation

Testing is not subordinate to any of the other pillars — it's how we know whether they work.

**Model validation:** Temporal split testing is the standard. Train on data before a cutoff, predict after, measure the gap. This is a mechanical process the infrastructure supports for any model that implements the estimator interface.

**Strategy backtesting:** Replay over historical data using expanding-window recalibration, producing a TrackRecord. The replay engine is infrastructure, not a one-off script — it works with any strategy that implements the strategy interface. The same replay validates fill simulation accuracy, cost model accuracy, and calibration quality, not just headline returns.

**Component testing:** Pure functions have unit tests. Estimators have calibration tests (predicted vs. observed rates). Strategies have integration tests with synthetic data.

**Ongoing monitoring:** After deployment, track record analysis detects alpha decay, calibration drift, and fill model degradation. The same TrackRecord format used in backtesting is used in production monitoring — evaluation is continuous, not a one-time gate.

The base interfaces for models and strategies exist partly *for* testing. A standard model interface means temporal split validation can be written once and applied to every model. A standard strategy interface means the replay engine doesn't need per-strategy wiring.

## The Dependency Graph

```
data sources → raw tables ─┐
                            ├→ derived tables (materialized, declared dependencies)
                            │
                            └→ MarketView(as_of)
                                 ├→ Estimator A  (implements base interface)
                                 ├→ Estimator B  (implements base interface)
                                 │
                                 └→ Strategy(view) → opportunities
                                      │
                                      ├→ replay engine → TrackRecord (backtest)
                                      └→ live trader   → TrackRecord (production)
```

- **Data layer** declares what's available. Consumers discover, not hardcode.
- **MarketView** provisions estimators from declared data requirements and enforces temporal boundaries.
- **Estimators** implement a base interface. MarketView can host any conforming estimator.
- **Strategies** implement a base interface. The replay engine and live trader can run any conforming strategy.
- **TrackRecord** is the common output of both backtesting and live trading — the single format for answering "does this work?"

## What This Is Not

- **Not a runtime data access framework.** Data sources are too heterogeneous to force through one query abstraction. The infrastructure provides metadata and conventions, not a universal data API. (MarketView is the temporal boundary between estimators and strategies — not a general-purpose data layer.)

- **Not a reimplementation of what works.** MarketView's temporal boundary, EVStrategy's scan interface, the replay engine's expanding-window approach, TrackRecord — these are good foundations. The work is formalizing interfaces, filling gaps, and extending the patterns to cover components that currently bypass them.

## Relationship to Other Docs

- **`data-system-spec.md`** — detailed specification for the data pillar.
- **`feature-framework-spec.md`** — specification for the model/strategy framework (Pillars 2-4).
- **`fill-model-v2-spec.md`** and **`fill-model-requirements.md`** — fill prediction model design.
