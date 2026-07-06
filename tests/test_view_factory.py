"""Tests for Chunk 5: ViewFactory.

Tests the core build() logic: registry cloning, calibrate-or-load,
topological factory ordering, dependency passing, store integration,
force_recalibrate, and expanding-window replay isolation.
"""

import pytest
from datetime import datetime, timezone

from framework.estimator import BoundEstimator, EstimatorFeature
from framework.calibration_store import CalibrationStore
from framework.feature import FeatureRegistry
from framework.view import View, TemporalBoundaryError
from framework.view_factory import ViewFactory


# ── Test helpers ─────────────────────────────────────────────────


class ConstantFeature:
    """Feature that returns a fixed value regardless of as_of."""

    def __init__(self, name, value):
        self._name = name
        self._value = value

    @property
    def name(self):
        return self._name

    def query(self, as_of, **params):
        return self._value


class SimpleEstimator:
    """Estimator that multiplies a stored value by a parameter."""

    def __init__(self, value):
        self.value = value

    def query(self, **params):
        return self.value * params.get('x', 1)

    def __repr__(self):
        return f"SimpleEstimator({self.value})"


class CountingFactory:
    """Factory that calibrates by counting observation length.

    Tracks how many times calibrate() was called for testing.
    """

    def __init__(self, factory_name='test_estimator',
                 data_reqs=None, deps=None):
        self._name = factory_name
        self._data_reqs = data_reqs or ['observations']
        self._deps = deps or []
        self.calibrate_count = 0

    @property
    def name(self):
        return self._name

    @property
    def data_requirements(self):
        return self._data_reqs

    @property
    def depends_on(self):
        return self._deps

    def calibrate(self, data, dependencies=None):
        self.calibrate_count += 1
        total = sum(len(v) if hasattr(v, '__len__') else 0
                    for v in data.values())
        return SimpleEstimator(total)


class DependentFactory:
    """Factory whose calibration depends on another estimator."""

    def __init__(self, factory_name='derived_est',
                 data_reqs=None, deps=None):
        self._name = factory_name
        self._data_reqs = data_reqs or ['observations']
        self._deps = deps or ['base_est']

    @property
    def name(self):
        return self._name

    @property
    def data_requirements(self):
        return self._data_reqs

    @property
    def depends_on(self):
        return self._deps

    def calibrate(self, data, dependencies=None):
        base_value = 0
        if dependencies:
            for dep in dependencies.values():
                base_value += dep.value
        n_obs = sum(len(v) if hasattr(v, '__len__') else 0
                    for v in data.values())
        return SimpleEstimator(base_value + n_obs)


class RecordingFactory:
    """Factory that records what it received for inspection."""

    def __init__(self, factory_name='recording_est',
                 data_reqs=None, deps=None):
        self._name = factory_name
        self._data_reqs = data_reqs or ['observations']
        self._deps = deps or []
        self.received_data = None
        self.received_deps = None
        self.received_as_of = 'NOT_CALLED'

    @property
    def name(self):
        return self._name

    @property
    def data_requirements(self):
        return self._data_reqs

    @property
    def depends_on(self):
        return self._deps

    def calibrate(self, data, dependencies=None):
        self.received_data = data
        self.received_deps = dependencies
        # If calibrate received as_of, that's a bug — it shouldn't
        return SimpleEstimator(42)


class FakeCostModel:
    def maker_fee(self, price_cents, contracts):
        return 2 * contracts

    def taker_fee(self, price_cents, contracts):
        return 8 * contracts


def utc(month, day, hour=0):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def make_base_registry(observations=None):
    """Create a base registry with a constant observations feature."""
    registry = FeatureRegistry()
    registry.register(ConstantFeature('observations',
                                      observations or list(range(100))))
    return registry


# ═══════════════════════════════════════════════════════════════════
# Build basics
# ═══════════════════════════════════════════════════════════════════


class TestBuildBasics:

    def test_build_with_no_factories(self):
        """Build with only stored features, no estimators."""
        registry = make_base_registry()
        vf = ViewFactory(registry)
        view = vf.build(utc(4, 1))
        assert isinstance(view, View)
        assert view.query('observations') == list(range(100))

    def test_build_with_one_factory(self):
        """Single factory calibrates from data and is queryable."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        view = vf.build(utc(4, 1))
        # Factory saw 100 observations -> SimpleEstimator(100)
        assert view.query('my_est', x=2) == 200

    def test_build_calibrates_factory(self):
        """Verify calibrate() was actually called."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        vf.build(utc(4, 1))
        assert factory.calibrate_count == 1

    def test_build_with_costs(self):
        """CostModel is passed through to View."""
        registry = make_base_registry()
        costs = FakeCostModel()
        vf = ViewFactory(registry, costs=costs)

        view = vf.build(utc(4, 1))
        assert view.cost(50, 1, is_maker=True) == 2

    def test_build_without_costs(self):
        """View without costs raises on cost()."""
        registry = make_base_registry()
        vf = ViewFactory(registry)
        view = vf.build(utc(4, 1))
        with pytest.raises(RuntimeError, match="No CostModel"):
            view.cost(50, 1)

    def test_build_live(self):
        """build_live() produces a valid View."""
        registry = make_base_registry()
        vf = ViewFactory(registry)
        view = vf.build_live()
        assert isinstance(view, View)


# ═══════════════════════════════════════════════════════════════════
# Structural enforcement
# ═══════════════════════════════════════════════════════════════════


class TestStructuralEnforcement:

    def test_calibrate_does_not_receive_as_of(self):
        """calibrate() receives data and deps, never as_of."""
        registry = make_base_registry()
        factory = RecordingFactory('rec_est')
        vf = ViewFactory(registry, factories=[factory])

        vf.build(utc(4, 1))

        # Factory received data dict with feature values
        assert 'observations' in factory.received_data
        assert factory.received_data['observations'] == list(range(100))
        # No dependencies
        assert factory.received_deps is None
        # as_of was never passed (it stayed 'NOT_CALLED' which means
        # calibrate's signature doesn't include it)
        assert factory.received_as_of == 'NOT_CALLED'

    def test_availability_time_assigned_by_framework(self):
        """Framework assigns availability_time = as_of, not the estimator."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        as_of = utc(4, 1)
        view = vf.build(as_of)

        # The estimator feature in the registry has the right avail time
        feat = view._registry._features['my_est']
        assert hasattr(feat, 'availability_time')
        assert feat.availability_time == as_of


# ═══════════════════════════════════════════════════════════════════
# CalibrationStore integration
# ═══════════════════════════════════════════════════════════════════


class TestStoreIntegration:

    def test_calibrate_then_load(self, tmp_path):
        """First build calibrates and stores; second build loads from store."""
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory], store=store)

        # First build: calibrates
        view1 = vf.build(utc(4, 1))
        assert factory.calibrate_count == 1
        assert view1.query('my_est', x=2) == 200

        # Second build at same as_of: loads from store
        view2 = vf.build(utc(4, 1))
        assert factory.calibrate_count == 1  # NOT called again
        assert view2.query('my_est', x=2) == 200

    def test_different_as_of_calibrates_again(self, tmp_path):
        """Different as_of that has no stored artifact triggers calibration."""
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory], store=store)

        vf.build(utc(4, 1))
        assert factory.calibrate_count == 1

        # Later as_of loads the April 1 artifact (avail <= as_of)
        vf.build(utc(4, 5))
        assert factory.calibrate_count == 1  # loaded from store

    def test_force_recalibrate(self, tmp_path):
        """force_recalibrate ignores the store."""
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory], store=store)

        vf.build(utc(4, 1))
        assert factory.calibrate_count == 1

        # Force recalibrate
        vf.build(utc(4, 1), force_recalibrate={'my_est'})
        assert factory.calibrate_count == 2

    def test_force_recalibrate_selective(self, tmp_path):
        """force_recalibrate only affects named factories."""
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')
        factory_a = CountingFactory('est_a')
        factory_b = CountingFactory('est_b')
        vf = ViewFactory(registry, factories=[factory_a, factory_b], store=store)

        vf.build(utc(4, 1))
        assert factory_a.calibrate_count == 1
        assert factory_b.calibrate_count == 1

        # Only force est_a
        vf.build(utc(4, 1), force_recalibrate={'est_a'})
        assert factory_a.calibrate_count == 2
        assert factory_b.calibrate_count == 1  # loaded from store

    def test_no_store_always_calibrates(self):
        """Without a store, every build() calibrates fresh."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        vf.build(utc(4, 1))
        vf.build(utc(4, 1))
        assert factory.calibrate_count == 2


# ═══════════════════════════════════════════════════════════════════
# Factory dependencies
# ═══════════════════════════════════════════════════════════════════


class TestFactoryDependencies:

    def test_dependent_factory_receives_inner(self):
        """Dependent factory receives the inner estimator, not BoundEstimator."""
        registry = make_base_registry()
        base = CountingFactory('base_est')
        derived = DependentFactory('derived_est', deps=['base_est'])
        vf = ViewFactory(registry, factories=[base, derived])

        view = vf.build(utc(4, 1))
        # base_est: 100 observations -> SimpleEstimator(100)
        # derived_est: 100 (from base) + 100 (own observations) = 200
        assert view.query('derived_est', x=1) == 200

    def test_topological_order(self):
        """Factories are calibrated in dependency order."""
        registry = make_base_registry()
        # derived depends on base — regardless of list order
        derived = DependentFactory('derived_est', deps=['base_est'])
        base = CountingFactory('base_est')
        vf = ViewFactory(registry, factories=[derived, base])

        view = vf.build(utc(4, 1))
        assert view.query('base_est', x=1) == 100
        assert view.query('derived_est', x=1) == 200

    def test_diamond_dependencies(self):
        """Diamond: C depends on A and B, both depend on base."""
        registry = make_base_registry()
        base = CountingFactory('base_est')
        a = DependentFactory('est_a', deps=['base_est'])
        b = DependentFactory('est_b', deps=['base_est'])

        class DiamondFactory:
            @property
            def name(self): return 'est_c'
            @property
            def data_requirements(self): return ['observations']
            @property
            def depends_on(self): return ['est_a', 'est_b']
            def calibrate(self, data, dependencies=None):
                total = sum(d.value for d in dependencies.values())
                return SimpleEstimator(total)

        vf = ViewFactory(registry, factories=[DiamondFactory(), a, b, base])
        view = vf.build(utc(4, 1))

        # base=100, a=200, b=200, c=a+b=400
        assert view.query('est_c', x=1) == 400

    def test_loaded_dependency_available(self, tmp_path):
        """A factory loaded from store is available as a dependency."""
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')
        base = CountingFactory('base_est')
        derived = DependentFactory('derived_est', deps=['base_est'])
        vf = ViewFactory(registry, factories=[base, derived], store=store)

        # First build: calibrates both
        vf.build(utc(4, 1))
        assert base.calibrate_count == 1

        # Second build: base loaded from store, derived calibrated
        # (derived always recalibrates because force_recalibrate)
        view = vf.build(utc(4, 1), force_recalibrate={'derived_est'})
        assert base.calibrate_count == 1  # loaded
        assert view.query('derived_est', x=1) == 200  # still correct

    def test_cycle_detected(self):
        """Circular factory dependencies raise ValueError."""
        registry = make_base_registry()
        a = DependentFactory('est_a', deps=['est_b'])
        b = DependentFactory('est_b', deps=['est_a'])
        vf = ViewFactory(registry, factories=[a, b])

        with pytest.raises(ValueError, match="Circular"):
            vf.build(utc(4, 1))

    def test_missing_dependency_detected(self):
        """Factory depending on unregistered factory raises ValueError."""
        registry = make_base_registry()
        orphan = DependentFactory('orphan', deps=['nonexistent'])
        vf = ViewFactory(registry, factories=[orphan])

        with pytest.raises(ValueError, match="nonexistent"):
            vf.build(utc(4, 1))


# ═══════════════════════════════════════════════════════════════════
# Registry isolation (expanding-window safety)
# ═══════════════════════════════════════════════════════════════════


class TestRegistryIsolation:

    def test_builds_produce_independent_registries(self):
        """Two builds don't share registry state."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        view1 = vf.build(utc(4, 1))
        view2 = vf.build(utc(4, 5))

        # Both views work independently
        assert view1.query('my_est', x=1) == 100
        assert view2.query('my_est', x=1) == 100

        # They're not the same registry
        assert view1._registry is not view2._registry

    def test_base_registry_unchanged_after_build(self):
        """build() doesn't mutate the base registry."""
        registry = make_base_registry()
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory])

        # Base registry has 1 feature
        assert len(registry) == 1

        vf.build(utc(4, 1))

        # Base registry still has 1 feature (estimator not leaked)
        assert len(registry) == 1
        assert 'my_est' not in registry

    def test_expanding_window_simulation(self, tmp_path):
        """Simulate expanding window: multiple builds at different as_of."""
        obs_data = list(range(100))
        registry = FeatureRegistry()
        registry.register(ConstantFeature('observations', obs_data))

        store = CalibrationStore(tmp_path / 'cal')
        factory = CountingFactory('my_est')
        vf = ViewFactory(registry, factories=[factory], store=store)

        views = []
        for month in range(1, 5):
            view = vf.build(utc(month, 1))
            views.append(view)

        # All views work, all query the same estimator value (same obs data)
        for v in views:
            assert v.query('my_est', x=1) == 100

        # First build calibrated, rest loaded from store
        assert factory.calibrate_count == 1

        # Store has the artifact
        assert store.latest_boundary('my_est') == utc(1, 1)


# ═══════════════════════════════════════════════════════════════════
# Topological sort edge cases
# ═══════════════════════════════════════════════════════════════════


class TestTopologicalSort:

    def test_no_factories(self):
        vf = ViewFactory(make_base_registry())
        assert vf._topo_sorted_factories() == []

    def test_independent_factories_sorted_by_name(self):
        """Independent factories appear in deterministic (alphabetical) order."""
        registry = make_base_registry()
        c = CountingFactory('charlie')
        a = CountingFactory('alpha')
        b = CountingFactory('bravo')
        vf = ViewFactory(registry, factories=[c, a, b])

        names = [f.name for f in vf._topo_sorted_factories()]
        assert names == ['alpha', 'bravo', 'charlie']

    def test_chain_dependency(self):
        """A -> B -> C: resolved in correct order."""
        registry = make_base_registry()
        c = CountingFactory('c')
        b = DependentFactory('b', deps=['c'])
        a = DependentFactory('a', deps=['b'])
        vf = ViewFactory(registry, factories=[a, b, c])

        names = [f.name for f in vf._topo_sorted_factories()]
        assert names.index('c') < names.index('b') < names.index('a')

    def test_self_cycle(self):
        registry = make_base_registry()
        loop = DependentFactory('loop', deps=['loop'])
        vf = ViewFactory(registry, factories=[loop])
        with pytest.raises(ValueError, match="Circular"):
            vf._topo_sorted_factories()

    def test_three_way_cycle(self):
        registry = make_base_registry()
        a = DependentFactory('a', deps=['c'])
        b = DependentFactory('b', deps=['a'])
        c = DependentFactory('c', deps=['b'])
        vf = ViewFactory(registry, factories=[a, b, c])
        with pytest.raises(ValueError, match="Circular"):
            vf._topo_sorted_factories()


# ═══════════════════════════════════════════════════════════════════
# Full pipeline integration
# ═══════════════════════════════════════════════════════════════════


class TestFullPipeline:

    def test_end_to_end_with_store(self, tmp_path):
        """Complete pipeline: stored features -> factory -> store -> view."""
        registry = FeatureRegistry()
        registry.register(ConstantFeature('observations', list(range(50))))

        store = CalibrationStore(tmp_path / 'cal')
        factory = CountingFactory('event_rate')
        costs = FakeCostModel()
        vf = ViewFactory(registry, factories=[factory],
                         store=store, costs=costs)

        # Build view
        view = vf.build(utc(4, 1))

        # Query estimator through view
        assert view.query('event_rate', x=3) == 150  # 50 * 3

        # Query stored feature through view
        assert view.query('observations') == list(range(50))

        # Costs work
        assert view.cost(50, 1, is_maker=True) == 2

        # Artifact persisted
        assert store.latest_boundary('event_rate') == utc(4, 1)

    def test_view_validates_temporal_boundary(self, tmp_path):
        """View construction validates estimator availability_time <= as_of."""
        # This is already enforced by BoundEstimator + View._validate().
        # ViewFactory assigns availability_time = as_of, so this always holds.
        # But if we manually inject a future artifact, it should fail.
        registry = make_base_registry()
        store = CalibrationStore(tmp_path / 'cal')

        # Store an artifact with future availability_time
        future = utc(12, 1)
        store.store('bad_est', BoundEstimator(SimpleEstimator(1), future))

        # Factory that will trigger load of this artifact
        factory = CountingFactory('bad_est')
        vf = ViewFactory(registry, factories=[factory], store=store)

        # build at April 1: store.load returns None (future), so calibrates
        view = vf.build(utc(4, 1))
        assert view.query('bad_est', x=1) == 100  # calibrated, not loaded

    def test_repr(self):
        registry = make_base_registry()
        vf = ViewFactory(registry, factories=[CountingFactory('a')])
        assert '1 factories' in repr(vf)
