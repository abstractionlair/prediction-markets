"""ViewFactory: constructs temporally-consistent Views.

This is the "environment" — strategies never see it.
The Runner and live trader use it to produce Views.

ViewFactory combines:
- A base registry (stored features / data sources)
- Estimator factories (calibration logic)
- A calibration store (persistence)
- A cost model (transaction costs)

Given an as_of, it produces a View with all estimators calibrated or
loaded from the store. Each build() creates a FRESH registry snapshot,
which is critical for expanding-window replay where build() is called
per evaluation point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from framework.estimator import BoundEstimator
from framework.view import View

if TYPE_CHECKING:
    from framework.calibration_store import CalibrationStore
    from framework.estimator import EstimatorFactory
    from framework.feature import FeatureRegistry


class ViewFactory:
    """Constructs temporally-consistent Views.

    Strategies never see ViewFactory — they receive Views.
    The Runner and live trader use ViewFactory to produce Views.
    """

    def __init__(self, registry: FeatureRegistry,
                 factories: list[EstimatorFactory] | None = None,
                 store: CalibrationStore | None = None,
                 costs=None):
        """
        Args:
            registry: Base registry with stored features (data sources).
            factories: EstimatorFactory instances to calibrate/load.
            store: CalibrationStore for artifact persistence. If None,
                   all estimators are calibrated fresh on every build().
            costs: CostModel passed through to View for fee calculations.
        """
        self._registry = registry
        self._factories: dict[str, EstimatorFactory] = {
            f.name: f for f in (factories or [])
        }
        self._store = store
        self._costs = costs

    def build(self, as_of: datetime,
              force_recalibrate: set[str] | None = None) -> View:
        """Build a View for the given as_of.

        For each registered estimator factory (in dependency order):
        1. Try loading a valid cached artifact from the store
        2. If no cache hit (or force_recalibrate), calibrate from data
        3. Optionally store the new artifact
        4. Register the estimator as a feature in the view registry

        Returns a View that wraps the populated registry.
        """
        force_recalibrate = force_recalibrate or set()

        # Fresh registry snapshot per build — critical for expanding-window
        # replay. A shared mutable registry would fail on the second call.
        view_registry = self._registry.clone()

        # Track calibrated estimators for dependency resolution
        estimators: dict[str, BoundEstimator] = {}

        for factory in self._topo_sorted_factories():
            # query_fn bridges the View's typed interface to the estimator's
            # specific API. Factories that don't need bridging omit it.
            query_fn = getattr(factory, 'query_fn', None)

            # Step 1: Try loading from store
            if (factory.name not in force_recalibrate
                    and self._store is not None):
                cached = self._store.load(factory.name, as_of)
                if cached is not None:
                    view_registry.register_bound(
                        factory.name, cached, query_fn=query_fn)
                    estimators[factory.name] = cached
                    continue

            # Step 2: Calibrate from data
            data = {req: view_registry.query(req, as_of)
                    for req in factory.data_requirements}
            deps = {dep: estimators[dep].inner
                    for dep in factory.depends_on}

            # calibrate() does NOT receive as_of — structural enforcement
            raw_estimator = factory.calibrate(data, deps if deps else None)

            # Framework assigns availability_time — the estimator didn't
            bound = BoundEstimator(raw_estimator, availability_time=as_of)

            # Step 3: Optionally store
            if self._store is not None:
                self._store.store(factory.name, bound)

            view_registry.register_bound(
                factory.name, bound, query_fn=query_fn)
            estimators[factory.name] = bound

        return View(as_of, view_registry, self._costs)

    def build_live(self) -> View:
        """Build a View for live trading (as_of = now).

        Convenience wrapper. Uses the latest available calibrations.
        """
        return self.build(as_of=datetime.now(timezone.utc))

    def _topo_sorted_factories(self) -> list:
        """Topologically sort factories by depends_on.

        Returns factories in an order where each factory appears after
        all factories it depends on. Raises ValueError on cycles or
        missing dependencies.
        """
        if not self._factories:
            return []

        # Validate all dependencies reference registered factories
        for name, factory in self._factories.items():
            for dep in factory.depends_on:
                if dep not in self._factories:
                    raise ValueError(
                        f"Factory '{name}' depends on '{dep}' "
                        f"which is not a registered factory"
                    )

        # Kahn's algorithm
        remaining = dict(self._factories)
        resolved: set[str] = set()
        result = []

        while remaining:
            ready = [name for name, f in remaining.items()
                     if all(d in resolved for d in f.depends_on)]
            if not ready:
                raise ValueError(
                    f"Circular factory dependencies among: "
                    f"{sorted(remaining.keys())}"
                )
            ready.sort()  # deterministic ordering
            for name in ready:
                result.append(remaining.pop(name))
                resolved.add(name)

        return result

    def __repr__(self) -> str:
        return (f"ViewFactory({len(self._factories)} factories, "
                f"store={'yes' if self._store else 'no'})")
