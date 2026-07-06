"""Feature protocol, materialization strategies, and registry.

The Feature protocol is the internal framework interface. Consumers (strategies,
compute functions) never call query() directly -- they go through the View,
which calls query() on their behalf.

query() takes as_of because it is framework code. User-provided functions
(compute_fn, calibrate) do NOT take as_of -- see spec Section 2.3.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Feature(Protocol):
    """A named value with temporal semantics.

    Every feature answers: "What is the value of X, given that we can only
    use information available before time T?"

    The consumer does not know or care whether the value was read from a
    database (stored), retrieved from cache (cached), or computed on demand
    (computed). The unification is at the temporal enforcement level.
    """

    @property
    def name(self) -> str: ...

    def query(self, as_of: datetime, **params) -> Any:
        """Return the feature value using only information available before as_of.

        Parameters are feature-specific (entity identifiers, model query params).
        Returns None if no value is available for the given parameters and as_of.
        """
        ...


class PreloadedFeature:
    """Feature backed by in-memory data.

    Used for expanding-window replay (and live trading initialization)
    where data is preloaded from the database once and filtered by
    as_of on each query.

    The filter_fn receives (data, as_of) and returns filtered data.
    If filter_fn is None, the data is returned as-is (no temporal filtering).
    """

    def __init__(self, name: str, data: Any, filter_fn=None):
        """
        Args:
            name: Unique feature name for registry lookup.
            data: The preloaded data (list, dict, etc.).
            filter_fn: Optional fn(data, as_of) -> filtered_data.
                       If None, query() returns data unchanged.
        """
        self._name = name
        self._data = data
        self._filter_fn = filter_fn

    @property
    def name(self) -> str:
        return self._name

    def query(self, as_of: datetime, **params) -> Any:
        if self._filter_fn is not None:
            return self._filter_fn(self._data, as_of)
        return self._data

    def __repr__(self) -> str:
        return f"PreloadedFeature({self._name!r})"


class StoredFeature:
    """Feature backed by a database table.

    The availability time is derived from a column in the table.
    Temporal filter: availability_column < as_of (strict <, per spec Section 1.1).

    Subclasses override query() to provide specific SQL for complex joins.
    The base implementation handles single-table queries with optional
    param-based filtering.
    """

    def __init__(self, name: str, table: str, availability_column: str,
                 conn_factory):
        """
        Args:
            name: Unique feature name for registry lookup.
            table: Fully qualified table name (e.g., 'prediction_markets.kalshi_trades').
            availability_column: Column representing when data became available.
            conn_factory: Callable returning a database connection.
        """
        self._name = name
        self._table = table
        self._availability_column = availability_column
        self._conn_factory = conn_factory

    @property
    def name(self) -> str:
        return self._name

    @property
    def table(self) -> str:
        return self._table

    @property
    def availability_column(self) -> str:
        return self._availability_column

    def query(self, as_of: datetime, **params) -> Any:
        """Query with temporal filter: availability_column < as_of.

        Default implementation: SELECT * FROM table WHERE availability_column < as_of.
        Override for joins, custom columns, or param-based filtering.
        """
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT * FROM {self._table} "
                f"WHERE {self._availability_column} < %s",
                (as_of,)
            )
            return cur.fetchall()
        finally:
            cur.close()

    def __repr__(self) -> str:
        return f"StoredFeature({self._name!r}, table={self._table!r})"


# ---------------------------------------------------------------------------
# CachedFeature
# ---------------------------------------------------------------------------


class CachedFeature:
    """Feature computed once per temporal bucket, then cached.

    Used for: moderately expensive computations (estimator calibration
    that takes seconds, aggregations over large tables).

    CRITICAL: the compute_fn does NOT receive as_of. It receives a dict
    of pre-resolved dependency values, already filtered by the framework.
    This is what makes temporal discipline structural rather than
    conventional.

    Return type contract: compute_fn must return a queryable object
    (something with a .query(**params) method). The cache stores the
    whole object; individual queries dispatch to it.
    """

    def __init__(self, name: str, compute_fn, dependencies: list[str],
                 registry: FeatureRegistry,
                 bucket_granularity: str = 'daily'):
        """
        Args:
            name: Unique feature name.
            compute_fn: fn(resolved_deps: dict) -> queryable object.
                        Must NOT receive as_of.
            dependencies: Names of features this depends on.
            registry: Registry for resolving dependencies.
            bucket_granularity: 'daily' or 'hourly'.
        """
        self._name = name
        self._compute_fn = compute_fn
        self.dependencies = list(dependencies)
        self._registry = registry
        self._bucket_granularity = bucket_granularity
        self._cache: dict[date | tuple[date, int], Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _normalize_utc(as_of: datetime) -> datetime:
        """Normalize to UTC. Naive datetimes are assumed UTC."""
        if as_of.tzinfo is None:
            return as_of.replace(tzinfo=timezone.utc)
        return as_of.astimezone(timezone.utc)

    def _to_bucket(self, as_of: datetime) -> date | tuple[date, int]:
        utc = self._normalize_utc(as_of)
        if self._bucket_granularity == 'hourly':
            return (utc.date(), utc.hour)
        return utc.date()

    def _bucket_floor(self, as_of: datetime) -> datetime:
        """Return the start of the bucket containing as_of, in UTC.

        Always resolve dependencies at bucket floor, not raw as_of.
        This ensures deterministic results regardless of query order.
        Without this, query(22:00) then query(10:00) in the same
        daily bucket would serve 10:00 data from 22:00's perspective.

        All bucketing is done in UTC to ensure the same absolute instant
        always maps to the same bucket regardless of the input timezone.
        """
        utc = self._normalize_utc(as_of)
        if self._bucket_granularity == 'hourly':
            return utc.replace(minute=0, second=0, microsecond=0)
        return utc.replace(hour=0, minute=0, second=0, microsecond=0)

    def query(self, as_of: datetime, **params) -> Any:
        bucket = self._to_bucket(as_of)
        if bucket not in self._cache:
            canonical_time = self._bucket_floor(as_of)
            resolved = {dep: self._registry.query(dep, canonical_time)
                        for dep in self.dependencies}
            self._cache[bucket] = self._compute_fn(resolved)
        return self._cache[bucket].query(**params)

    def clear_cache(self):
        """Clear the in-memory cache."""
        self._cache.clear()

    def __repr__(self) -> str:
        return f"CachedFeature({self._name!r}, deps={self.dependencies})"


# ---------------------------------------------------------------------------
# ComputedFeature
# ---------------------------------------------------------------------------


class ComputedFeature:
    """Feature computed on demand from other features. No caching.

    Used for: cheap derivations, features that combine multiple inputs
    (e.g., spread = ask - bid), features that need the freshest inputs.

    Like CachedFeature, the compute_fn does NOT receive as_of. It
    receives pre-resolved dependency values only.

    Return type contract: compute_fn returns a plain value directly
    (unlike CachedFeature which returns a queryable object).
    """

    def __init__(self, name: str, dependencies: list[str], compute_fn,
                 registry: FeatureRegistry):
        """
        Args:
            name: Unique feature name.
            compute_fn: fn(resolved_deps: dict, **params) -> value.
                        Must NOT receive as_of.
            dependencies: Names of features this depends on.
            registry: Registry for resolving dependencies.
        """
        self._name = name
        self.dependencies = list(dependencies)
        self._compute_fn = compute_fn
        self._registry = registry

    @property
    def name(self) -> str:
        return self._name

    def query(self, as_of: datetime, **params) -> Any:
        # Dependencies are resolved WITHOUT the caller's **params.
        # This is an intentional deviation from spec Section 2.2.3.
        # Rationale: params are feature-specific (e.g., key="ticker").
        # Forwarding them to unrelated dependencies would cause errors
        # or silent wrong results. The compute_fn receives params and
        # is responsible for interpreting them.
        resolved = {dep: self._registry.query(dep, as_of)
                    for dep in self.dependencies}
        return self._compute_fn(resolved, **params)

    def __repr__(self) -> str:
        return f"ComputedFeature({self._name!r}, deps={self.dependencies})"


# ---------------------------------------------------------------------------
# FeatureRegistry
# ---------------------------------------------------------------------------


class FeatureRegistry:
    """Central catalog of all features.

    Responsibilities:
    - Register features by name
    - Resolve dependency graphs (topological sort)
    - Validate: no circular dependencies, all dependencies exist
    - Provide query routing: given a feature name and as_of, return the value
    - Clone for per-view isolation (expanding-window replay safety)
    """

    def __init__(self):
        self._features: dict[str, Feature] = {}

    def register(self, feature: Feature):
        """Register a feature. Raises if name already taken."""
        if feature.name in self._features:
            raise ValueError(f"Feature '{feature.name}' already registered")
        self._features[feature.name] = feature

    def register_bound(self, name: str, bound_estimator: BoundEstimator,
                       query_fn=None):
        """Register a BoundEstimator as a feature.

        Wraps the BoundEstimator in an EstimatorFeature adapter so it
        conforms to the Feature protocol (name + query(as_of, **params)).

        Args:
            name: Feature name for registry lookup.
            bound_estimator: BoundEstimator with availability_time.
            query_fn: Optional fn(estimator, **params) -> value.
                      If None, calls estimator.query(**params).
        """
        from framework.estimator import EstimatorFeature
        if name in self._features:
            raise ValueError(f"Feature '{name}' already registered")
        feature = EstimatorFeature(name, bound_estimator, query_fn)
        self._features[name] = feature

    def query(self, name: str, as_of: datetime, **params) -> Any:
        """Query a feature by name. Temporal filtering is automatic."""
        if name not in self._features:
            raise KeyError(f"Feature '{name}' not registered")
        return self._features[name].query(as_of, **params)

    def validate(self):
        """Check that all dependencies exist and there are no cycles.

        Raises ValueError with a descriptive message on failure.
        """
        # Check all dependencies exist
        for name, feature in self._features.items():
            for dep in self._get_dependencies(feature):
                if dep not in self._features:
                    raise ValueError(
                        f"Feature '{name}' depends on '{dep}' "
                        f"which is not registered"
                    )

        # Check no cycles via DFS
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._features}
        path: list[str] = []

        def visit(name: str):
            color[name] = GRAY
            path.append(name)
            for dep in self._get_dependencies(self._features[name]):
                if color[dep] == GRAY:
                    cycle_start = path.index(dep)
                    cycle = path[cycle_start:] + [dep]
                    raise ValueError(
                        f"Circular dependency: {' -> '.join(cycle)}"
                    )
                if color[dep] == WHITE:
                    visit(dep)
            path.pop()
            color[name] = BLACK

        for name in self._features:
            if color[name] == WHITE:
                visit(name)

    def dependency_order(self) -> list[str]:
        """Topological sort of features by dependency.

        Returns feature names in an order such that every feature appears
        after all of its dependencies. Raises ValueError if cycles exist.
        """
        self.validate()

        # Kahn's algorithm
        in_degree: dict[str, int] = {n: 0 for n in self._features}
        for name, feature in self._features.items():
            for dep in self._get_dependencies(feature):
                in_degree[name] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        queue.sort()  # deterministic ordering among features with same in-degree
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            # Find features that depend on this node
            for name, feature in self._features.items():
                if node in self._get_dependencies(feature):
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        # Insert sorted for determinism
                        idx = 0
                        while idx < len(queue) and queue[idx] < name:
                            idx += 1
                        queue.insert(idx, name)

        return result

    def clone(self) -> FeatureRegistry:
        """Create a copy for per-view isolation.

        Features without mutable state (StoredFeature, EstimatorFeature)
        are shared. Features with a registry reference (CachedFeature,
        ComputedFeature) get new instances pointing at the clone, so that
        features added to the clone are visible to dependency resolution.
        CachedFeature caches are NOT copied — each clone starts fresh.

        This is used by ViewFactory.build() to create per-view registries
        for expanding-window replay.
        """
        new = FeatureRegistry()
        for name, feature in self._features.items():
            if isinstance(feature, CachedFeature):
                cloned = CachedFeature(
                    feature._name, feature._compute_fn,
                    feature.dependencies, new,
                    feature._bucket_granularity,
                )
                new._features[name] = cloned
            elif isinstance(feature, ComputedFeature):
                cloned = ComputedFeature(
                    feature._name, feature.dependencies,
                    feature._compute_fn, new,
                )
                new._features[name] = cloned
            else:
                new._features[name] = feature
        return new

    def _get_dependencies(self, feature: Feature) -> list[str]:
        """Extract dependency list from a feature, if it has one."""
        deps = getattr(feature, 'dependencies', None)
        return list(deps) if deps else []

    def items(self):
        """Iterate over (name, feature) pairs."""
        return self._features.items()

    def __contains__(self, name: str) -> bool:
        return name in self._features

    def __len__(self) -> int:
        return len(self._features)

    def __repr__(self) -> str:
        return f"FeatureRegistry({len(self._features)} features)"
