# Feature Framework: Specification

**Draft 3** | 2026-04-11

Reviewed by: claude-opus, claude-sonnet, gemini-3.1-pro, gemini-3-flash, gpt-5.4
Review: `reviews/feature-framework-spec/review-2026-04-11.md`

Parent: `infrastructure-vision.md` (Pillars 2-4)
Informed by: survey of Feast, ArcticDB, QuantConnect/LEAN, Metaflow, Lopez de Prado

## Purpose

A framework where data sources, calibrated models, and trading strategies plug
in through standard interfaces — and where common errors (temporal leakage,
survivorship bias, cost omission, training-serving skew) are structurally
impossible, not merely discouraged.

"Structurally impossible" means: the consumer API does not expose an operation
that would produce the error. It is not "here are the rules, please follow
them." It is "the incorrect operation is not available."

## Scope

This spec covers:
- The Feature abstraction (unified access to raw data and derived/computed values)
- Materialization strategies (stored, cached, computed) and why consumers don't see them
- The View (capability boundary between infrastructure and strategy/model code)
- Calibration lifecycle (calibrate, store, load, validate)
- Strategy and estimator protocols
- The Runner (replay, live, validation modes)
- Structural invariants and what each layer enforces

Out of scope: data ingestion (covered by `data-system-spec.md`), specific
estimator implementations, specific strategy implementations.


## 1. Concepts

### 1.1 Availability Time

Every piece of information has an **availability time**: the earliest moment at
which a real-time decision-maker could have reliably known it.

This is distinct from:
- **Event time**: when the thing happened (trade executed, candle period ended)
- **Ingestion time**: when our collector stored it
- **Recording time**: when the database row was written

For most market data, availability ~ event time. But the distinction matters:
- A settlement result is available at settlement time, not when we downloaded it
- A calibration trained on data through March 31 is available on March 31,
  regardless of when the training job ran
- A corrected data point is available at the time of correction, not at the
  time of the original event (bitemporal: the old value was what we knew then)

Each data source declares how to compute availability time from its fields.
This is a per-source function, not a global rule.

**Boundary convention:** Raw data queries use strict `<` (data at exactly
`as_of` may not yet be reliably available). Derived artifacts use `<=`
(an artifact produced *for* `as_of` is valid *at* `as_of`, because it was
trained only on data with availability `< as_of`). Both conventions are
correct together: the strict `<` on data ensures the artifact never saw
data at the boundary instant, so loading it at that instant is safe.

**For derived/computed values**, availability time is not declared — it is
assigned by the framework as the `as_of` under which the value was constructed.
This is the *upper bound* of what the computation could have seen, not the
actual maximum timestamp of its inputs. If the framework queries data with
`as_of = March 31` and the latest actual observation is from March 28, the
derived value's availability time is March 31 — because the framework *asked*
for everything before March 31 and the computation could have used anything
up to that point. This is conservative: it prevents the artifact from being
used in a March 30 context (where additional data from March 28-30 might
exist that this artifact never saw). The alternative — tracking actual
`max(input timestamps)` — would be tighter but adds tracking complexity
and permits artifacts to be used in contexts they weren't produced for.

No component declares its own availability time for derived values; the
framework assigns it.

### 1.2 Feature

A **feature** is a named value with temporal semantics. It answers the question:
"What is the value of X, given that we can only use information available
before time T?"

Raw market data is a feature. A calibrated P(YES) estimate is a feature.
A model's fill probability prediction is a feature. The consumer does not
know or care whether the value was:
- Read from a database table (stored)
- Retrieved from an in-memory cache (cached)
- Computed on demand from other features (computed)

The unification is not at the query level (different features have different
parameters), but at the **temporal enforcement** level: everything has an
availability time, everything is filtered by `as_of`, and the materialization
strategy is invisible to consumers.

### 1.3 View

The **view** is a frozen-in-time projection of all features. It is the single
handle that strategies and dependent models receive. It mediates all access
to data and model outputs. Consumers cannot construct a view — they receive
one from the environment (runner, live trader).

The view is a **capability boundary**: if you don't have a view, you can't
access data or models. If you do have a view, everything you access through
it is guaranteed temporally consistent.

### 1.4 Estimator

An **estimator** transforms data into predictions. It has two lifecycles:
- **Calibrate**: receive pre-filtered data, produce a calibrated instance.
  The estimator does not know what `as_of` was used — it just receives data.
- **Load**: retrieve a previously stored calibration artifact, validated
  against a requested `as_of` by the framework.

Once calibrated or loaded, an estimator serves predictions. It operates on
the data it was given; it cannot reach for more. Critically, the estimator
does not declare or track its own temporal boundary. The **framework** tracks
the availability time externally: it knows what `as_of` was used during
calibration, and it stores this metadata alongside the artifact. This
prevents an estimator from lying about (or miscalculating) its own boundary.

### 1.5 Calibration Artifact

A **calibration artifact** is a stored estimator with framework-assigned metadata:
- What estimator type produced it
- The `availability_time` (= the `as_of` that was in effect when the framework
  calibrated it, which in turn = max availability time of all input data)
- Configuration used (hyperparameters, data source versions)
- Validation metrics at training time (optional but recommended)

The `availability_time` is assigned by the framework, not by the estimator.
The estimator does not know its own boundary. This is an instance of the
general principle: derived values do not declare their availability time;
the framework computes it from the inputs.

A calibration artifact with availability_time T can be loaded by any view
with `as_of >= T`. It cannot be loaded by a view with `as_of < T`.

### 1.6 Strategy

A **strategy** is a pure function: `(market_state, positions, view) -> actions`.
No persistent state across calls, no database access, no side channels.
The same implementation runs in replay and production — the environment
(what view it receives) changes, not the strategy.

Actions include both **entries** (new positions to open) and **exits**
(existing positions to close). The strategy receives current positions as
input so it can make exit decisions using the same calibrated estimates it
uses for entry decisions. This unifies entry scanning and exit evaluation
into a single strategy output.

Positions are execution state provided by the Runner, not market data from
the View. In replay, positions are the simulated portfolio. In production,
positions are the live portfolio.


## 2. Layer 1: Feature Layer

### 2.1 Feature Protocol

```python
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Feature(Protocol):
    """A named value with temporal semantics.

    The Feature protocol is the internal framework interface.
    Consumers (strategies, compute functions) never call query() directly —
    they go through the View, which calls query() on their behalf.

    query() takes as_of because it is framework code. User-provided
    functions (compute_fn, calibrate) do NOT take as_of — see Section 2.5.
    """

    @property
    def name(self) -> str: ...

    def query(self, as_of: datetime, **params) -> Any:
        """Return the feature value, using only information available before as_of.

        Parameters are feature-specific:
        - Raw data features: entity identifiers (ticker, series)
        - Model features: model query parameters (series, hours, price)

        Returns None if no value is available for the given parameters and as_of.

        This method is called by framework infrastructure (View, Registry),
        never by user code (strategies, compute functions, calibrators).
        """
        ...
```

### 2.2 Materialization Strategies

Three strategies. The consumer sees only `Feature.query()`.

#### 2.2.1 StoredFeature

Backed by a database table. The availability time is a column in the table.

```python
class StoredFeature:
    """Feature backed by a database table.

    Used for: raw ingested data, persisted calibration artifacts,
    materialized derived tables.

    The query method translates to a SQL query with a temporal filter:
        WHERE {availability_column} < {as_of}
    """

    def __init__(self, name: str, table: str, availability_column: str,
                 conn_factory):
        self.name = name
        self.table = table
        self.availability_column = availability_column
        self._conn_factory = conn_factory

    def query(self, as_of, **params):
        # Subclasses or configuration define the specific SQL
        # and how params map to WHERE clauses.
        ...
```

Examples of stored features:
- `kalshi_trades`: raw trade data. `availability_column = 'created_time'`.
- `kalshi_settled_markets`: settlement outcomes. `availability_column = 'settled_at'`.
- `event_rate_calibration`: a persisted EventRateEstimator artifact.
  `availability_column = 'availability_time'`.

#### 2.2.2 CachedFeature

Computed once per `as_of` bucket, then cached in memory. The cache is scoped
to the process; not persisted across restarts (use StoredFeature for that).

```python
class CachedFeature:
    """Feature computed once per temporal bucket, then cached.

    Used for: moderately expensive computations (estimator calibration
    that takes seconds, aggregations over large tables).

    The cache key is (feature_name, as_of_bucket). The bucket granularity
    is configurable (daily, hourly).

    CRITICAL: the compute_fn does NOT receive as_of. It receives a dict
    of pre-resolved dependency values, already filtered by the framework.
    This is what makes temporal discipline structural rather than
    conventional — the compute function literally cannot access data
    beyond the boundary because it never sees the boundary or any
    handle that could be used to bypass it.
    """

    def __init__(self, name: str, compute_fn, dependencies: list[str],
                 registry: 'FeatureRegistry',
                 bucket_granularity: str = 'daily'):
        self.name = name
        self._compute_fn = compute_fn      # fn(resolved_deps) -> queryable
        self.dependencies = dependencies
        self._registry = registry          # for resolving dependencies
        self._bucket_granularity = bucket_granularity
        self._cache = {}  # bucket -> computed result

    def query(self, as_of, **params):
        bucket = self._to_bucket(as_of)
        if bucket not in self._cache:
            # Always resolve at bucket floor, not raw as_of.
            # This ensures deterministic results regardless of query order.
            # Without this, query(22:00) then query(10:00) in the same
            # daily bucket would serve 10:00 data from 22:00's perspective.
            canonical_time = self._bucket_floor(as_of)
            resolved = {dep: self._registry.query(dep, canonical_time)
                        for dep in self.dependencies}
            # compute_fn receives resolved data ONLY — not as_of,
            # not a database connection, not a registry handle.
            self._cache[bucket] = self._compute_fn(resolved)
        return self._cache[bucket].query(**params)
```

Note: `query(self, as_of, **params)` still takes `as_of` — that's the Feature
protocol, implemented by infrastructure code. The user-provided `compute_fn`
does not. The as_of flows through the framework's dependency resolution, not
through user code.

**Return type contract:** CachedFeature's `compute_fn` must return a
**queryable object** — something with a `.query(**params)` method (typically
a calibrated estimator). The cache stores the whole object; individual queries
dispatch to it. This is different from ComputedFeature, whose `compute_fn`
returns a **plain value** directly. The distinction reflects their use cases:
CachedFeature caches an estimator that serves many queries; ComputedFeature
computes a single derived value per call.

#### 2.2.3 ComputedFeature

Derived from other features on every call. No caching.

```python
class ComputedFeature:
    """Feature computed on demand from other features.

    Used for: cheap derivations, features that combine multiple
    inputs (e.g., spread = ask - bid), features that need the
    freshest possible inputs.

    Like CachedFeature, the compute_fn does NOT receive as_of.
    It receives pre-resolved dependency values only.
    """

    def __init__(self, name: str, dependencies: list[str], compute_fn,
                 registry: 'FeatureRegistry'):
        self.name = name
        self.dependencies = dependencies
        self._compute_fn = compute_fn      # fn(resolved_deps, **params) -> value
        self._registry = registry

    def query(self, as_of, **params):
        # Framework resolves dependencies with as_of filtering
        resolved = {dep: self._registry.query(dep, as_of, **params)
                    for dep in self.dependencies}
        # compute_fn gets resolved values and query params — not as_of
        return self._compute_fn(resolved, **params)
```

### 2.3 The Boundary Between Framework and User Code

The three materialization strategies have a critical structural property:

**Framework code** (StoredFeature.query, CachedFeature.query, ComputedFeature.query,
FeatureRegistry.query, View.__init__) handles `as_of`. It performs temporal
filtering, dependency resolution, and boundary validation. This code is
infrastructure — written once, tested heavily, not modified by users adding
new features or estimators.

**User code** (compute_fn, calibrate functions, strategy.scan) **never sees
`as_of`**. User code receives:
- Pre-filtered data (for calibration functions)
- Pre-resolved dependency values (for compute functions)
- A View handle (for strategies) whose temporal boundary is baked in and opaque

This is the structural enforcement. It's not "the compute function should only
use data before as_of." It's "the compute function receives data, and that
data has already been filtered — there is no as_of to misuse and no handle
to reach around the boundary."

The one exception: StoredFeature subclasses are framework code that knows
about `as_of` and database connections. These are infrastructure, not user
extensions. Adding a new raw data source means writing a StoredFeature
(infrastructure), not a compute function (user code).

### 2.4 Feature Registry

The registry holds all registered features and resolves dependencies.

```python
class FeatureRegistry:
    """Central catalog of all features.

    Responsibilities:
    - Register features by name
    - Resolve dependency graphs (topological sort)
    - Validate: no circular dependencies, all dependencies exist
    - Provide query routing: given a feature name and as_of, return the value
    """

    def __init__(self):
        self._features: dict[str, Feature] = {}

    def register(self, feature: Feature):
        """Register a feature. Raises if name already taken."""
        if feature.name in self._features:
            raise ValueError(f"Feature '{feature.name}' already registered")
        self._features[feature.name] = feature

    def query(self, name: str, as_of: datetime, **params) -> Any:
        """Query a feature by name. Temporal filtering is automatic."""
        feature = self._features[name]
        return feature.query(as_of, **params)

    def validate(self):
        """Check that all dependencies exist and there are no cycles."""
        ...

    def dependency_order(self) -> list[str]:
        """Topological sort of features by dependency."""
        ...
```

### 2.5 Universe Features (Survivorship Bias Prevention)

A **universe feature** is a stored feature that defines the set of entities
that existed at a given time. It includes entities that no longer exist
(settled markets, delisted assets).

```python
# Example: the market universe feature
# Includes ALL markets — active, settled, superseded.
# Temporal filtering is by the market's existence window,
# not by its current status.

class MarketUniverseFeature(StoredFeature):
    """All markets that existed at as_of.

    A market "existed" at time T if:
    - It was created before T (open_time <= T or created_time <= T)
    - It had not yet settled at T (settled_at IS NULL OR settled_at > T)
      OR it had settled but we need it for calibration

    This feature ALWAYS includes settled/dead markets in its historical
    range. There is no "active only" variant exposed to consumers.
    Survivorship bias is prevented by construction.
    """

    def query(self, as_of, **params):
        # Returns markets that were tradeable at as_of
        # AND markets that had settled before as_of (for calibration)
        # The 'include_settled' flag in params controls which subset,
        # but both subsets are available — neither is hidden.
        ...
```

The structural invariant: **any feature that represents a universe of entities
includes dead/settled entities by definition.** A consumer who wants "active
markets at time T" gets the correct set for time T (including markets that
have since settled). A consumer who wants "training data" gets observations
from all markets that settled before T. Neither query suffers survivorship
bias because the underlying data includes the full history.


## 3. Layer 2: View

### 3.1 View Construction

Views are constructed by the environment (ViewFactory), never by consumers.

```python
class View:
    """Frozen-in-time projection of all features.

    The single handle strategies and dependent models receive.
    Mediates all access to data AND model outputs.

    Structural invariants enforced at construction:
    1. Every estimator's availability_time <= self.as_of
    2. Every data query is filtered by self.as_of
    3. Consumers cannot access self.as_of (it's not part of the interface)
    4. Consumers cannot access the underlying registry or database

    Invariants 3-4 are conventions enforced by code review, not by the
    type system. Python doesn't have the access control to make these
    truly impossible. But the API makes violating them unnatural — you'd
    have to reach into private attributes.
    """

    def __init__(self, as_of: datetime, registry: FeatureRegistry,
                 costs: CostModel):
        self._as_of = as_of
        self._registry = registry
        self._costs = costs
        self._validate()

    def _validate(self):
        """Verify all features respect the temporal boundary."""
        for name, feature in self._registry._features.items():
            if hasattr(feature, 'availability_time'):
                if feature.availability_time > self._as_of:
                    raise TemporalBoundaryError(
                        f"Feature '{name}' availability_time="
                        f"{feature.availability_time} "
                        f"exceeds view as_of={self._as_of}")

    # ── Strategy-facing interface ──────────────────────────────

    def event_rate(self, series: str, hours: float,
                   **price_kwargs) -> tuple | None:
        """P(YES | series, hours_to_settlement, observed_prices).

        Returns (p_yes, standard_error, n_markets) or None if insufficient data.
        """
        return self._registry.query('event_rate', self._as_of,
                                     series=series, hours=hours, **price_kwargs)

    def fill_probability(self, side: str, limit_price: int, quantity: int,
                         market_state: dict) -> FillEstimate | None:
        """P(fill | side, limit_price, quantity, market_state).

        market_state includes: bid, ask, hours_to_settlement, trailing_volume,
        open_interest, generating_process, topic.

        Returns FillEstimate with .p_fill_won and .p_fill_lost, or None.
        """
        return self._registry.query('fill_model', self._as_of,
                                     side=side, limit_price=limit_price,
                                     quantity=quantity, **market_state)

    def classification(self, series: str) -> tuple[str, str] | None:
        """(generating_process, topic) for a series ticker, or None."""
        return self._registry.query('classification', self._as_of,
                                     series=series)

    def cost(self, price_cents: int, contracts: int,
             is_maker: bool = True) -> int:
        """Transaction cost in cents. Not temporally bounded (fee schedule is static)."""
        if is_maker:
            return self._costs.maker_fee(price_cents, contracts)
        return self._costs.taker_fee(price_cents, contracts)
```

### 3.2 What the View Exposes vs. Hides

**Exposed to consumers (strategies, dependent models):**
- Typed query methods (event_rate, fill_probability, classification, cost)
- No knowledge of which estimator backend is in use
- No knowledge of whether results are stored, cached, or computed
- No access to as_of (the temporal boundary is invisible)

**Hidden from consumers:**
- The as_of timestamp (consumers cannot inspect or change it)
- The feature registry (consumers cannot query arbitrary features)
- The database connection (consumers cannot run SQL)
- Estimator internals (consumers cannot access model parameters)
- Materialization strategy (consumers don't know if a value was stored or computed)

**Accessible to the environment (ViewFactory, Runner):**
- as_of (needed to construct the view)
- registry (needed to populate the view)
- stats/diagnostics (for logging and monitoring)

### 3.3 View Extension

When a new estimator type is added:

1. Register the backing feature in the registry (StoredFeature, CachedFeature,
   or ComputedFeature)
2. Add a typed method to View that delegates to `self._registry.query()`
3. Strategies that want to use the new estimator call the new method

Step 2 is deliberate coupling. Each new capability gets an explicit, documented,
type-checked method. The alternative (a generic `view.query('feature_name', ...)`
method) is more extensible but loses type safety and discoverability.

For experimentation before committing to a permanent View method, use:

```python
    def query(self, feature_name: str, **params) -> Any:
        """Generic feature query. Prefer typed methods for production use.

        Available for experimentation and for features that haven't yet
        earned a permanent View method.
        """
        return self._registry.query(feature_name, self._as_of, **params)
```


## 4. Calibration Lifecycle

### 4.1 Estimator Protocol

```python
class Estimator:
    """A calibrated component that serves predictions.

    Once calibrated, an estimator is immutable — it serves predictions
    from its frozen internal state. It has no temporal awareness.

    Estimators do NOT track their own temporal boundary. The framework
    wraps them in a BoundEstimator that records availability_time
    externally. This prevents an estimator from lying about or
    miscalculating its boundary.
    """
    # No trained_as_of property. Estimator-specific query methods only.
    # e.g., get_event_rate(series, hours, **prices)
    # e.g., estimate(side, limit_price, quantity, market_state)


class BoundEstimator:
    """Framework wrapper that pairs an estimator with its availability time.

    Created by the framework when calibrating or loading an estimator.
    The inner estimator doesn't know about the boundary.
    """

    def __init__(self, inner: Estimator, availability_time: datetime):
        self._inner = inner
        self.availability_time = availability_time

    def __getattr__(self, name):
        return getattr(self._inner, name)
```

### 4.2 Estimator Factory Protocol

```python
@runtime_checkable
class EstimatorFactory(Protocol):
    """Creates calibrated estimator instances from data.

    Declares what data it needs and what other estimators it depends on.
    The framework uses these declarations to:
    - Load the right data sources, filtered by as_of
    - Resolve dependencies in the right order
    - Assign availability_time to the result

    CRITICAL: calibrate() does NOT receive as_of. It receives pre-filtered
    data only. The framework assigns availability_time externally. This is
    the same structural enforcement as ComputedFeature — user code never
    sees the temporal boundary.
    """

    @property
    def name(self) -> str:
        """Unique name for this estimator type."""
        ...

    @property
    def data_requirements(self) -> list[str]:
        """Names of features this estimator needs for calibration."""
        ...

    @property
    def depends_on(self) -> list[str]:
        """Names of other estimators this one depends on.

        Dependencies are calibrated first and passed to calibrate().
        """
        ...

    def calibrate(self, data: dict[str, Any],
                  dependencies: dict[str, Estimator] | None = None) -> Estimator:
        """Produce a calibrated estimator from pre-filtered data.

        data: {feature_name: query_result} for each feature in data_requirements.
              Already filtered by the framework. The calibrate function does
              not know what as_of was used — it just trains on what it receives.
        dependencies: {name: calibrated_estimator} for each name in depends_on.
              Already calibrated by the framework with the same temporal boundary.

        Returns a plain Estimator. The framework wraps it in a BoundEstimator
        with the appropriate availability_time.
        """
        ...
```

### 4.3 Calibration Store

Where calibration artifacts are persisted and retrieved.

```python
class CalibrationStore:
    """Persistent storage for calibration artifacts.

    Backed by filesystem (serialized artifacts) + database metadata
    (for querying by name and temporal boundary).

    Directory layout:
        calibrations/
            {estimator_name}/
                {availability_time_iso}.pkl

    Metadata table: prediction_markets.calibration_artifacts
        estimator_name    TEXT NOT NULL,
        availability_time TIMESTAMPTZ NOT NULL,  -- framework-assigned
        artifact_path     TEXT NOT NULL,
        config            JSONB,          -- hyperparameters, feature versions
        metrics           JSONB,          -- validation metrics at training time
        data_hash         TEXT,           -- hash of input data for reproducibility
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (estimator_name, availability_time)
    """

    def store(self, estimator_name: str, bound_estimator: BoundEstimator,
              config: dict = None, metrics: dict = None,
              data_hash: str = None):
        """Persist a calibrated estimator.

        The availability_time is read from the BoundEstimator wrapper
        (framework-assigned, not estimator-declared).
        Overwrites if an artifact with the same (name, availability_time) exists.
        """
        ...

    def load(self, estimator_name: str, as_of: datetime) -> BoundEstimator | None:
        """Load the best artifact valid for a given as_of.

        Returns the BoundEstimator with the highest availability_time <= as_of,
        or None if no valid artifact exists.

        Validates: availability_time <= as_of (defense in depth).
        """
        ...

    def latest_boundary(self, estimator_name: str) -> datetime | None:
        """The availability_time of the most recent artifact for this estimator."""
        ...

    def list_boundaries(self, estimator_name: str) -> list[datetime]:
        """All available availability_time dates, sorted ascending."""
        ...
```

### 4.4 Calibration Lifecycle

The full lifecycle for an estimator:

```
1. REGISTER
   EstimatorFactory registered with ViewFactory.
   Declares data_requirements and depends_on.

2. CALIBRATE
   ViewFactory calls factory.calibrate(data, dependencies).
   Data is pre-filtered by as_of. Dependencies are pre-calibrated.
   Returns a plain Estimator (no temporal metadata).
   Framework wraps it: BoundEstimator(estimator, availability_time=as_of).

3. STORE (optional)
   CalibrationStore.store(name, bound_estimator, config, metrics).
   Serializes the estimator + framework-assigned availability_time.

4. LOAD (alternative to 2)
   CalibrationStore.load(name, as_of).
   Returns the best stored BoundEstimator with availability_time <= as_of.
   Used when calibration is expensive and a recent artifact exists.

5. SERVE
   BoundEstimator registered as a feature in the FeatureRegistry.
   View delegates queries to it.

6. VALIDATE
   Runner.validate_estimator(name, split_date, metric_fn).
   Calibrates on data before split_date, evaluates on data after.
   Records gap metrics. Optionally gates deployment.
```

The framework decides between steps 2 and 4 automatically:

```python
# In ViewFactory.build():

# Each build() creates a FRESH registry snapshot. The base registry
# holds StoredFeatures (raw data sources). build() clones it and adds
# estimator features for this specific as_of. This is critical for
# expanding-window replay where build() is called per evaluation point —
# a shared mutable registry would fail on the second call.
view_registry = self._registry.clone()

for factory in self._topo_sorted_factories():
    # Try loading from store first (step 4)
    if factory.name not in force_recalibrate and self.store:
        cached = self.store.load(factory.name, as_of)
        if cached is not None:
            view_registry.register_bound(factory.name, cached)
            continue

    # Calibrate from data (step 2)
    data = {req: view_registry.query(req, as_of)
            for req in factory.data_requirements}
    deps = {dep: estimators[dep] for dep in factory.depends_on}

    # calibrate() does NOT receive as_of — only pre-filtered data
    raw_estimator = factory.calibrate(data, deps)

    # Framework assigns availability_time — the estimator didn't
    bound = BoundEstimator(raw_estimator, availability_time=as_of)

    # Optionally store (step 3)
    if self.store:
        self.store.store(factory.name, bound)

    view_registry.register_bound(factory.name, bound)
    estimators[factory.name] = bound

return View(as_of, view_registry, self._costs)
```

The consumer is unaware of whether the estimator was freshly calibrated or
loaded from storage. The view serves the same interface either way.

### 4.5 Transitive Dependencies

If estimator A depends on estimator B:

1. B is calibrated/loaded first (topological sort)
2. B's inner estimator is passed to A's `calibrate()` as a dependency
3. A can use B's predictions as input features during calibration
4. B's `availability_time` must be <= the view's `as_of`
   (enforced by framework at load/calibrate time)

Neither A nor B knows about temporal boundaries. A receives pre-filtered
data and a pre-calibrated B. The framework assigned B's `availability_time`
when B was calibrated; it validates B's boundary before passing B to A.

This means: if you build a chain A -> B -> C, and the view's `as_of` is
March 15, then C was trained on data before March 15, B was trained on data
(including C's outputs) before March 15, and A was trained on data (including
B's outputs) before March 15. Temporal discipline propagates transitively.
No component in the chain is aware of it — the framework handles it.


## 5. Layer 3: Composition

### 5.1 Strategy Protocol

```python
@dataclass
class Position:
    """A currently held position. Provided by the Runner, not the View."""
    ticker: str
    event_ticker: str
    series: str
    side: str              # 'yes' or 'no'
    entry_price: int       # cents
    contracts: int
    entered_at: datetime


@dataclass
class EntryAction:
    """Strategy recommends opening a new position."""
    ticker: str
    event_ticker: str
    series: str
    side: str
    limit_price: int
    contracts: int
    ev_per_contract: float
    # ... other fields as needed by the executor


@dataclass
class ExitAction:
    """Strategy recommends closing an existing position."""
    position: Position
    reason: str            # e.g., 'overpriced', 'alpha_decay', 'stop_loss'
    limit_price: int | None  # None = market order / settle


@runtime_checkable
class Strategy(Protocol):
    """Pure function: (events, positions, view) -> actions.

    Structural constraints:
    - No database access (no psycopg2, no connection objects)
    - No imports of estimator/calibration modules
    - No file I/O
    - No mutation of view, events, or positions
    - The view is the ONLY source of estimates and data
    - Positions are the ONLY source of portfolio state

    These constraints are enforced by:
    - Code review (primary)
    - Import analysis in CI (automated check that strategy modules
      don't import database or calibration modules)
    - Testing with mock views (strategies must work with synthetic data)
    """

    def scan(self, events: list[dict], view: View,
             positions: list[Position] | None = None,
             now: datetime | None = None) -> list[EntryAction | ExitAction]:
        """Evaluate markets and current portfolio. Return recommended actions.

        events: market state (from API or reconstructed for replay).
        view: temporally-bounded handle — the ONLY source of estimates.
        positions: currently held positions (from Runner, not from View).
        now: current simulation/wall-clock time (for days-to-settlement).

        Returns a list of actions (entries and exits), sorted by priority.
        The Runner/executor decides which actions to actually execute
        based on risk limits, capital constraints, etc.
        """
        ...
```

**Why `events` and `now` are outside the View:**

The View mediates access to *historical/calibrated* data and model outputs.
`events` and `now` are *contemporaneous observation* — what the market looks
like right now. These are different concerns:

- **`events`**: In production, comes from the live API. In replay, the
  Runner reconstructs it from historical candles. The View cannot provide
  this because it's current market state, not historical analysis. If the
  Runner's reconstruction is buggy, that's a Runner implementation concern,
  not a framework design failure. The framework prevents *model/calibration*
  leakage structurally; it does not (and should not) try to prevent all
  possible software bugs in all components.

- **`now`**: Wall-clock / simulation time, used for time-to-settlement
  calculations. Distinct from `as_of` (the data boundary). In production
  these diverge: the View might be hours old (last recalibration) while
  `now` is current. Conflating them in the View would muddle its purpose.
  The Runner is responsible for passing `now` correctly in replay (it equals
  the evaluation timestamp, not actual wall-clock time).

### 5.2 ViewFactory

Constructs views. Knows about data sources, estimator factories, and the
calibration store. Given an `as_of`, produces a View with all estimators
calibrated or loaded.

```python
class ViewFactory:
    """Constructs temporally-consistent Views.

    This is the "environment" — strategies never see it.
    The Runner and live trader use it to produce Views.
    """

    def __init__(self, conn_factory, registry: FeatureRegistry,
                 factories: list[EstimatorFactory],
                 store: CalibrationStore | None = None,
                 costs: CostModel | None = None):
        self._conn_factory = conn_factory
        self._registry = registry
        self._factories = {f.name: f for f in factories}
        self._store = store
        self._costs = costs or default_costs()

    def build(self, as_of: datetime,
              force_recalibrate: set[str] | None = None) -> View:
        """Build a View for the given as_of.

        For each registered estimator factory:
        1. Try loading a valid cached artifact from the store
        2. If no cache (or force_recalibrate), calibrate from data
        3. Register the estimator as a feature in the registry

        Resolves dependencies topologically.
        Returns a View that wraps the populated registry.
        """
        ...

    def build_live(self) -> View:
        """Build a View for live trading (as_of = now).

        Convenience wrapper. Uses the latest available calibrations.
        """
        return self.build(as_of=datetime.now(timezone.utc))
```

### 5.3 Runner

Runs strategies over historical data or in production. Generic — works
with any Strategy and any set of estimators.

```python
class Runner:
    """Executes strategies in replay or live mode.

    Responsibilities:
    - Construct views at each evaluation point (expanding window)
    - Reconstruct market state from historical data (replay mode)
    - Simulate fills from subsequent price action (replay mode)
    - Record results to TrackRecord
    - Apply independent cost verification

    The Runner is infrastructure. It does not know which strategy or
    which estimators are in use.
    """

    def __init__(self, view_factory: ViewFactory,
                 strategy_cls: type[Strategy],
                 strategy_kwargs: dict = None):
        self.view_factory = view_factory
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs or {}

    def replay(self, start: datetime, end: datetime,
               recalibration_schedule: str = 'daily',
               fill_simulator = None) -> TrackRecord:
        """Expanding-window replay over historical data.

        For each evaluation point:
        1. Build a View with as_of = evaluation_time
        2. Reconstruct market state from historical data
        3. Run strategy.scan(events, view)
        4. Simulate fills from subsequent candle data
        5. Settle resolved positions
        6. Record to TrackRecord (with independent cost application)

        recalibration_schedule: how often to rebuild the view.
            'daily': new view per calendar day
            'weekly': new view per week
            'hourly': new view per hour
        """
        ...

    def validate_estimator(self, estimator_name: str,
                           split_date: datetime,
                           metric_fn) -> ValidationResult:
        """Temporal split validation for any registered estimator.

        1. Build a view with as_of = split_date
        2. Extract the estimator's predictions on post-split data
        3. Compare to actuals
        4. Return gap metrics

        This is a mechanical process that works for any estimator
        implementing the protocol.
        """
        ...
```

### 5.4 Independent Cost Verification

The Runner applies transaction costs independently of the strategy's internal
cost calculations. This prevents a strategy from reporting inflated returns by
omitting or underestimating costs.

```python
# In Runner.replay(), after recording a trade:

# Runner-verified cost (using the View's cost method, not strategy claims)
verified_cost = view.cost(fill_price, contracts)

trade_record = TradeRecord(
    ...
    fee_cents=verified_cost,
    pnl_cents=pnl_gross - verified_cost,  # Runner always uses verified
)
```


## 6. Invariants and Enforcement Levels

Not all invariants are enforced at the same strength. The framework provides
three levels:

- **Structural**: the incorrect operation is not expressible in the API.
  User code physically cannot produce the error because it lacks the
  necessary handles (as_of, database connection, registry).
- **Validated**: the framework checks at construction/load time and raises
  on violation. Requires intentional circumvention (e.g., constructing a
  BoundEstimator manually with a false availability_time).
- **Conventional**: enforced by code review, import analysis, and CI checks.
  Python's lack of true access control means some boundaries can be
  bypassed by accessing private attributes.

### 6.1 Layer 1 (Feature Layer)

| Invariant | Level | Mechanism |
|-----------|-------|-----------|
| **Temporal leakage in data** | Structural | Feature.query() filters by availability_time < as_of. No unfiltered access path exists. |
| **Temporal leakage in calibrations** | Validated | CalibrationStore.load() returns only artifacts with availability_time <= as_of. Framework assigns availability_time — estimators cannot self-declare. |
| **Temporal leakage in computed features** | Structural | compute_fn and calibrate() do NOT receive as_of. They receive pre-filtered data only. No handle exists to bypass the boundary. |
| **Survivorship bias** | Structural | Universe features include settled/dead entities by definition. No "active only" variant. |
| **Training-serving skew** | Structural | One feature definition per value. Materialization strategy (stored/cached/computed) is invisible to consumers. |

### 6.2 Layer 2 (View)

| Invariant | Level | Mechanism |
|-----------|-------|-----------|
| **Calibrated model temporal consistency** | Validated | View validates all estimators at construction. TemporalBoundaryError on violation. |
| **Transitive temporal consistency** | Validated | Estimator dependencies resolved in topological order. Each validated against as_of. availability_time assigned by framework. |
| **Strategy cannot bypass costs** | Conventional | View exposes cost(). Runner verifies costs independently. Strategy *should* use costs in ranking (EVStrategy does); Runner catches discrepancies. |
| **Strategy cannot access DB or estimator internals** | Conventional | as_of is a private attribute. No database handle exposed. Enforced by code review and CI import analysis — not by the type system. |

### 6.3 Layer 3 (Composition)

| Invariant | Level | Mechanism |
|-----------|-------|-----------|
| **Cost verification** | Validated | Runner applies verified costs to TrackRecord regardless of strategy claims. |
| **Reproducibility** | Validated | CalibrationStore records config + data_hash. Artifacts are versioned. |
| **Estimator validation available** | Conventional | validate_estimator() exists for any conforming estimator. Deployment gates can require it. |


## 7. Migration Path

### 7.1 Existing Code Mapping

| Current | Framework Equivalent |
|---------|---------------------|
| `MarketView(as_of, all_observations, ...)` | `View` constructed by `ViewFactory.build(as_of)` |
| `EventRateEstimator` | An `Estimator` created by an `EstimatorFactory`, registered as a `CachedFeature` |
| `FillRateEstimator` | Same |
| `FlowModel` | Same |
| `FillPredictor` (GBT) | An `Estimator` loaded from `CalibrationStore`, validated at load time |
| `EVStrategy` | Implements `Strategy` protocol (needs: positions param, Action return types) |
| `preload_observations()` | A `StoredFeature` with its own `query()` method |
| `replay.py` expanding-window logic | `Runner.replay()` with `recalibration_schedule='daily'` |
| `CostModel` / `KALSHI_COSTS` | Unchanged — passed to `View` and `Runner` |

### 7.2 Incremental Migration Order

The framework is built alongside existing code, not as a replacement.
Existing code continues to work throughout.

**Chunk 1: Feature protocol + StoredFeature**
- Define Feature protocol
- Implement StoredFeature for one data source (observations)
- Test: feature.query(as_of=T) returns only data with availability_time < T

**Chunk 2: FeatureRegistry + dependency resolution**
- Registry holds features, resolves dependencies (topological sort)
- Test: computed feature that depends on two stored features resolves correctly

**Chunk 3: View**
- Construct from registry + as_of
- Typed methods delegate to registry.query()
- Test: View refuses construction when an estimator's availability_time > as_of

**Chunk 4: Estimator protocol + CalibrationStore**
- Estimator protocol, BoundEstimator wrapper with availability_time
- File-backed store with metadata table
- Test: store.load(name, as_of) returns correct artifact; refuses future artifacts

**Chunk 5: ViewFactory**
- Combines registry, estimator factories, store
- Automatic calibrate-or-load logic
- Test: factory.build(as_of) produces a valid View with all estimators

**Chunk 6: Wrap existing estimators**
- EventRateEstimator, FillRateEstimator, FlowModel implement Estimator protocol
- EstimatorFactory wrappers for each
- **FlowModel migration note:** FlowModel.calibrate() currently takes `as_of`
  and does its own temporal filtering on the trade tape internally. This
  filtering must be extracted into the framework's data resolution. The
  EstimatorFactory wrapper loads pre-filtered trades as a data requirement;
  the inner FlowModel receives only the filtered data.
- **FillPredictor migration note:** This is the primary motivating example
  for CalibrationStore — it's a pre-trained GBT model passed in without
  any temporal boundary validation today. Under the framework, it must be
  loaded from CalibrationStore with a framework-assigned availability_time.
- **Fill interface note:** The current EVStrategy uses two fill interfaces
  (`fill_rates` for `_find_best_limit` and `fill_estimate` for
  `_find_best_order`). The new View unifies these into `fill_probability`.
  Strategy call sites need updating when migrating to the framework View.
- Test: framework-constructed View produces same outputs as hand-constructed MarketView

**Chunk 7: Runner**
- Generic replay using ViewFactory instead of bespoke expanding-window logic
- Independent cost verification
- Test: replay produces TrackRecord consistent with existing replay.py

**Chunk 8: Retire MarketView**
- View replaces MarketView in trader.py and replay.py
- MarketView becomes a thin backward-compatibility wrapper (if needed)


## 8. Open Questions

### 8.1 Entity model

Features are queried with different parameters (ticker, series, (process, topic, price, hours)). Is the entity just "whatever the feature needs," or do we formalize entity types?

Recommendation: entities are informal. Each feature defines its own query
parameters. The View's typed methods document the expected parameters. No
entity hierarchy.

### 8.2 Feature granularity for calibrations

An EventRateEstimator produces a lookup table with thousands of cells. Is the
"feature" the whole estimator, or each cell?

Recommendation: the feature is the estimator (a callable). `view.event_rate()`
is syntactic sugar for calling the estimator's lookup method. The estimator
is an artifact that *serves* feature values, not a single value.

### 8.3 Naming

This is broader than "temporal discipline" — it handles survivorship,
training-serving skew, cost verification, reproducibility. Candidates:

- "Feature framework" (follows Feast/Tecton vocabulary)
- "Evaluation framework" (emphasizes the validation aspect)
- "Quant infrastructure layer" (generic)

No strong opinion. Leave unnamed until a natural name emerges from use.

### 8.4 How much validation to gate on

The framework supports `validate_estimator()` for temporal split validation.
Should deployment require it? Options:

- **Advisory**: validation results stored but not required. Humans decide.
- **Gated**: deployment pipeline rejects estimators whose gap exceeds a threshold.
- **Monitored**: deployed estimators have ongoing validation; alerts on degradation.

Recommendation: start advisory. Gate later when we have enough estimators
to justify the overhead.

### 8.5 Batch operations

The current feature protocol is single-entity (`query(as_of, **params)`).
For replay over thousands of evaluation points, per-entity queries may be
too slow. Should the protocol support batch queries?

Recommendation: defer. The CachedFeature pattern (calibrate once per as_of
bucket) handles the common case. If a specific feature needs batch access,
add it as an optimization on that feature, not a protocol change.

### 8.6 Exit action execution

Strategies now return ExitActions alongside EntryActions. The Runner/executor
needs to handle both. For exits:
- In replay: simulate the exit (sell at limit, or at settlement if no fill)
- In production: place a sell order via the API

Should the exit logic be in the Runner (generic) or in a separate Executor
(per-venue)? The Runner handles the simulation; the Executor handles
real order placement. This separation may already be implied by the
strategy/executor split in the productionization plan.

### 8.7 Live trading view lifecycle

In production, the view is built once and used for hours. Should it be
rebuilt on a schedule? Should it detect when new calibration artifacts
are available and rebuild automatically?

Recommendation: rebuild on a configurable schedule (e.g., every 4 hours).
The ViewFactory.build_live() method always uses the latest valid calibrations.
Auto-detection is a future enhancement.
