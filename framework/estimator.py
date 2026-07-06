"""Estimator protocol, BoundEstimator wrapper, and EstimatorFeature adapter.

Estimators are calibrated components that serve predictions. They have no
temporal awareness — the framework wraps them in BoundEstimator with a
framework-assigned availability_time.

EstimatorFactory declares data requirements and produces calibrated estimators.
The calibrate() method receives pre-filtered data only, never as_of. This is
the same structural enforcement as CachedFeature/ComputedFeature.

EstimatorFeature adapts a BoundEstimator to the Feature protocol so it can
be registered in a FeatureRegistry and queried through a View.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EstimatorFactory(Protocol):
    """Creates calibrated estimator instances from data.

    Declares what data it needs and what other estimators it depends on.
    The framework uses these declarations to:
    - Load the right data sources, filtered by as_of
    - Resolve dependencies in the right order
    - Assign availability_time to the result

    CRITICAL: calibrate() does NOT receive as_of. It receives pre-filtered
    data only. The framework assigns availability_time externally.
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
                  dependencies: dict | None = None) -> Any:
        """Produce a calibrated estimator from pre-filtered data.

        data: {feature_name: query_result} for each feature in data_requirements.
              Already filtered by the framework.
        dependencies: {name: calibrated_estimator} for each name in depends_on.
              Already calibrated by the framework with the same temporal boundary.

        Returns a plain estimator. The framework wraps it in a BoundEstimator
        with the appropriate availability_time.
        """
        ...


class BoundEstimator:
    """Framework wrapper that pairs an estimator with its availability time.

    Created by the framework when calibrating or loading an estimator.
    The inner estimator doesn't know about the boundary.

    Delegates all attribute access to the inner estimator via __getattr__.
    This means consumers call estimator methods directly on the wrapper.
    """

    def __init__(self, inner, availability_time: datetime):
        self._inner = inner
        self.availability_time = availability_time

    @property
    def inner(self):
        """Access the wrapped estimator (for framework use only)."""
        return self._inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return (f"BoundEstimator({self._inner!r}, "
                f"availability_time={self.availability_time})")


class EstimatorFeature:
    """Adapter: makes a BoundEstimator work as a Feature in the registry.

    The Feature protocol requires name and query(as_of, **params).
    EstimatorFeature wraps a BoundEstimator and delegates query() to
    the inner estimator's query method (or a custom query_fn).

    The as_of parameter in query() is intentionally ignored — the estimator
    is already calibrated for a specific temporal boundary. The View validates
    availability_time <= as_of at construction time, so by the time query()
    is called, the temporal boundary has already been enforced.
    """

    def __init__(self, name: str, bound_estimator: BoundEstimator,
                 query_fn=None):
        """
        Args:
            name: Feature name for registry lookup.
            bound_estimator: The wrapped estimator with availability_time.
            query_fn: Optional fn(estimator, **params) -> value.
                      If None, calls inner.query(**params).
        """
        self._name = name
        self._bound = bound_estimator
        self._query_fn = query_fn

    @property
    def name(self) -> str:
        return self._name

    @property
    def availability_time(self) -> datetime:
        return self._bound.availability_time

    def query(self, as_of: datetime, **params) -> Any:
        """Query the estimator. as_of is validated at View construction,
        not here — the estimator is already calibrated for the right boundary.
        """
        if self._query_fn is not None:
            return self._query_fn(self._bound.inner, **params)
        return self._bound.inner.query(**params)

    def __repr__(self) -> str:
        return (f"EstimatorFeature({self._name!r}, "
                f"avail={self._bound.availability_time})")
