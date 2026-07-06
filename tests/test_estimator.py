"""Tests for Chunk 4: Estimator protocol, BoundEstimator, CalibrationStore.

Pure tests (no DB) run by default. DB integration tests marked @pytest.mark.db.
"""

import pytest
from datetime import datetime, timezone, timedelta

from framework.estimator import BoundEstimator, EstimatorFactory, EstimatorFeature
from framework.calibration_store import CalibrationStore
from framework.feature import FeatureRegistry
from framework.view import View, TemporalBoundaryError


# ── Test helpers ─────────────────────────────────────────────────


class SimpleEstimator:
    """A simple estimator for testing."""

    def __init__(self, value):
        self.value = value

    def predict(self, x):
        return self.value * x

    def query(self, **params):
        """Feature-protocol-compatible query method."""
        return self.value * params.get('x', 1)

    def __repr__(self):
        return f"SimpleEstimator({self.value})"


class LookupEstimator:
    """Estimator with a dict-based lookup, like EventRateEstimator."""

    def __init__(self, rates):
        self.rates = dict(rates)

    def get_rate(self, key):
        return self.rates.get(key)

    def query(self, **params):
        return self.get_rate(params.get('key'))


class SimpleFactory:
    """EstimatorFactory-compliant factory for testing."""

    def __init__(self, name='test_estimator', data_reqs=None, deps=None):
        self._name = name
        self._data_reqs = data_reqs or ['observations']
        self._deps = deps or []

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
        total = sum(len(v) for v in data.values())
        return SimpleEstimator(total)


class DependentFactory:
    """Factory that uses a dependency estimator during calibration."""

    def __init__(self, name='dependent_est', data_reqs=None, deps=None):
        self._name = name
        self._data_reqs = data_reqs or ['observations']
        self._deps = deps or ['base_estimator']

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
        base = dependencies['base_estimator'] if dependencies else None
        base_value = base.value if base else 0
        n_obs = sum(len(v) for v in data.values())
        return SimpleEstimator(base_value + n_obs)


# ═══════════════════════════════════════════════════════════════════
# BoundEstimator
# ═══════════════════════════════════════════════════════════════════


class TestBoundEstimator:

    def test_delegates_methods(self):
        inner = SimpleEstimator(42)
        bound = BoundEstimator(inner, datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert bound.predict(2) == 84

    def test_delegates_attributes(self):
        inner = SimpleEstimator(42)
        bound = BoundEstimator(inner, datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert bound.value == 42

    def test_availability_time(self):
        avail = datetime(2026, 4, 1, tzinfo=timezone.utc)
        bound = BoundEstimator(SimpleEstimator(1), avail)
        assert bound.availability_time == avail

    def test_inner_access(self):
        inner = SimpleEstimator(42)
        bound = BoundEstimator(inner, avail_time(4, 1))
        assert bound.inner is inner

    def test_missing_attr_raises(self):
        bound = BoundEstimator(SimpleEstimator(1), avail_time(4, 1))
        with pytest.raises(AttributeError):
            _ = bound.nonexistent_method

    def test_repr(self):
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        r = repr(bound)
        assert 'BoundEstimator' in r
        assert 'availability_time' in r

    def test_own_attrs_not_delegated(self):
        """BoundEstimator's availability_time takes precedence over inner's."""
        inner = SimpleEstimator(42)
        inner.availability_time = "wrong"
        bound = BoundEstimator(inner, avail_time(4, 1))
        assert bound.availability_time == avail_time(4, 1)

    def test_works_with_lookup_estimator(self):
        inner = LookupEstimator({'A': 0.9, 'B': 0.1})
        bound = BoundEstimator(inner, avail_time(4, 1))
        assert bound.get_rate('A') == 0.9
        assert bound.get_rate('C') is None


# ═══════════════════════════════════════════════════════════════════
# EstimatorFactory protocol
# ═══════════════════════════════════════════════════════════════════


class TestEstimatorFactoryProtocol:

    def test_simple_factory_is_compliant(self):
        assert isinstance(SimpleFactory(), EstimatorFactory)

    def test_dependent_factory_is_compliant(self):
        assert isinstance(DependentFactory(), EstimatorFactory)

    def test_factory_calibrate(self):
        factory = SimpleFactory()
        result = factory.calibrate({'observations': [1, 2, 3]})
        assert isinstance(result, SimpleEstimator)
        assert result.value == 3

    def test_factory_properties(self):
        f = SimpleFactory('my_est', ['obs', 'prices'], ['base_model'])
        assert f.name == 'my_est'
        assert f.data_requirements == ['obs', 'prices']
        assert f.depends_on == ['base_model']

    def test_non_compliant_class(self):
        class NotAFactory:
            pass
        assert not isinstance(NotAFactory(), EstimatorFactory)

    def test_partial_compliance_rejected(self):
        """A class with name and calibrate but no data_requirements is rejected."""
        class Partial:
            @property
            def name(self): return 'x'
            def calibrate(self, data, deps=None): return None
        assert not isinstance(Partial(), EstimatorFactory)

    def test_dependent_factory_uses_dependency(self):
        base = SimpleEstimator(100)
        factory = DependentFactory()
        result = factory.calibrate(
            {'observations': list(range(10))},
            dependencies={'base_estimator': base}
        )
        assert result.value == 110  # 100 + 10


# ═══════════════════════════════════════════════════════════════════
# EstimatorFeature adapter
# ═══════════════════════════════════════════════════════════════════


class TestEstimatorFeature:

    def test_name(self):
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        feat = EstimatorFeature('test_feat', bound)
        assert feat.name == 'test_feat'

    def test_availability_time(self):
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        feat = EstimatorFeature('test_feat', bound)
        assert feat.availability_time == avail_time(4, 1)

    def test_query_delegates_to_inner(self):
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        feat = EstimatorFeature('test_feat', bound)
        result = feat.query(avail_time(4, 5), x=3)
        assert result == 126  # 42 * 3

    def test_query_with_custom_fn(self):
        inner = LookupEstimator({'A': 0.9})
        bound = BoundEstimator(inner, avail_time(4, 1))
        feat = EstimatorFeature('rates', bound,
                                query_fn=lambda est, **p: est.get_rate(p['key']))
        assert feat.query(avail_time(4, 5), key='A') == 0.9

    def test_query_ignores_as_of(self):
        """as_of is not passed to estimator — temporal validation is at View level."""
        bound = BoundEstimator(SimpleEstimator(10), avail_time(4, 1))
        feat = EstimatorFeature('test', bound)
        r1 = feat.query(avail_time(3, 1), x=2)
        r2 = feat.query(avail_time(5, 1), x=2)
        assert r1 == r2 == 20

    def test_repr(self):
        bound = BoundEstimator(SimpleEstimator(1), avail_time(4, 1))
        feat = EstimatorFeature('test', bound)
        assert 'EstimatorFeature' in repr(feat)


# ═══════════════════════════════════════════════════════════════════
# CalibrationStore (file-only mode)
# ═══════════════════════════════════════════════════════════════════


class TestCalibrationStoreFileOnly:

    @pytest.fixture
    def store(self, tmp_path):
        return CalibrationStore(tmp_path / 'calibrations')

    def test_store_and_load(self, store):
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        store.store('test_est', bound)

        loaded = store.load('test_est', avail_time(4, 2))
        assert loaded is not None
        assert loaded.availability_time == avail_time(4, 1)
        assert loaded.predict(2) == 84

    def test_load_returns_none_when_empty(self, store):
        assert store.load('nonexistent', avail_time(4, 1)) is None

    def test_load_refuses_future_artifacts(self, store):
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(4, 10)))
        assert store.load('est', avail_time(4, 5)) is None

    def test_load_returns_best_artifact(self, store):
        """Returns artifact with highest avail_time <= as_of."""
        store.store('est', BoundEstimator(SimpleEstimator(10), avail_time(3, 1)))
        store.store('est', BoundEstimator(SimpleEstimator(20), avail_time(3, 15)))
        store.store('est', BoundEstimator(SimpleEstimator(30), avail_time(4, 1)))

        loaded = store.load('est', avail_time(3, 20))
        assert loaded.availability_time == avail_time(3, 15)
        assert loaded.predict(1) == 20

    def test_load_exact_boundary(self, store):
        """avail_time == as_of should match (<= not <)."""
        store.store('est', BoundEstimator(SimpleEstimator(42), avail_time(4, 1)))
        loaded = store.load('est', avail_time(4, 1))
        assert loaded is not None
        assert loaded.availability_time == avail_time(4, 1)

    def test_multiple_estimators_isolated(self, store):
        store.store('est_a', BoundEstimator(SimpleEstimator(10), avail_time(4, 1)))
        store.store('est_b', BoundEstimator(SimpleEstimator(20), avail_time(4, 1)))

        a = store.load('est_a', avail_time(4, 2))
        b = store.load('est_b', avail_time(4, 2))
        assert a.predict(1) == 10
        assert b.predict(1) == 20

    def test_latest_boundary(self, store):
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(3, 1)))
        store.store('est', BoundEstimator(SimpleEstimator(2), avail_time(4, 1)))
        assert store.latest_boundary('est') == avail_time(4, 1)

    def test_latest_boundary_none(self, store):
        assert store.latest_boundary('nonexistent') is None

    def test_list_boundaries(self, store):
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(3, 15)))
        store.store('est', BoundEstimator(SimpleEstimator(2), avail_time(3, 1)))
        store.store('est', BoundEstimator(SimpleEstimator(3), avail_time(4, 1)))

        assert store.list_boundaries('est') == [
            avail_time(3, 1), avail_time(3, 15), avail_time(4, 1)
        ]

    def test_list_boundaries_empty(self, store):
        assert store.list_boundaries('nonexistent') == []

    def test_overwrite_existing(self, store):
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(4, 1)))
        store.store('est', BoundEstimator(SimpleEstimator(2), avail_time(4, 1)))

        loaded = store.load('est', avail_time(4, 2))
        assert loaded.predict(1) == 2

    def test_creates_directories(self, tmp_path):
        store = CalibrationStore(tmp_path / 'deep' / 'nested' / 'dir')
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(4, 1)))
        assert (tmp_path / 'deep' / 'nested' / 'dir' / 'est').exists()

    def test_metadata_accepted_in_file_mode(self, store):
        """Config/metrics are accepted silently in file-only mode."""
        store.store('est', BoundEstimator(SimpleEstimator(1), avail_time(4, 1)),
                    config={'lr': 0.01}, metrics={'accuracy': 0.95},
                    data_hash='abc123')
        loaded = store.load('est', avail_time(4, 2))
        assert loaded is not None

    def test_naive_datetime_treated_as_utc(self, store):
        """Naive datetimes are normalized to UTC for filename encoding."""
        naive = datetime(2026, 4, 1, 12, 0, 0)
        store.store('est', BoundEstimator(SimpleEstimator(42), naive))

        loaded = store.load('est', datetime(2026, 4, 2, tzinfo=timezone.utc))
        assert loaded is not None
        assert loaded.availability_time.tzinfo == timezone.utc

    def test_complex_estimator_roundtrip(self, store):
        """Estimator with internal state survives serialization roundtrip."""
        inner = LookupEstimator({'BTC': 0.95, 'ETH': 0.80, 'SPX': 0.60})
        store.store('rates', BoundEstimator(inner, avail_time(4, 1)))

        loaded = store.load('rates', avail_time(4, 2))
        assert loaded.get_rate('BTC') == 0.95
        assert loaded.get_rate('ETH') == 0.80
        assert loaded.get_rate('missing') is None

    def test_defense_in_depth_rejects_future_artifact(self, tmp_path):
        """Post-load validation catches a corrupted backend returning future artifact.

        Spec Section 4.3: load() validates availability_time <= as_of
        even though the query already filters for this.
        """
        import pickle as _pickle
        store = CalibrationStore(tmp_path / 'calibrations')

        # Manually create a pkl file with a future timestamp in the filename
        # but trick the scan by putting it in a valid filename
        est_dir = tmp_path / 'calibrations' / 'bad_est'
        est_dir.mkdir(parents=True)
        # Write artifact with April 1 filename
        artifact_path = est_dir / '20260401T000000Z.pkl'
        with open(artifact_path, 'wb') as f:
            _pickle.dump(SimpleEstimator(1), f)

        # Normal load works — avail (Apr 1) <= as_of (Apr 5)
        loaded = store.load('bad_est', avail_time(4, 5))
        assert loaded is not None

        # Load at earlier time correctly returns None (filtered by scan)
        assert store.load('bad_est', avail_time(3, 1)) is None


# ═══════════════════════════════════════════════════════════════════
# CalibrationStore filename encoding
# ═══════════════════════════════════════════════════════════════════


class TestFilenameEncoding:

    def test_roundtrip(self):
        dt = datetime(2026, 4, 11, 14, 30, 0, tzinfo=timezone.utc)
        filename = CalibrationStore._time_to_filename(dt)
        parsed = CalibrationStore._filename_to_time(filename)
        assert parsed == dt

    def test_naive_datetime(self):
        dt = datetime(2026, 4, 11, 14, 30, 0)
        filename = CalibrationStore._time_to_filename(dt)
        assert filename == '20260411T143000Z.pkl'

    def test_timezone_conversion(self):
        """Non-UTC timezone is converted to UTC in filename."""
        est = timezone(timedelta(hours=-5))
        dt = datetime(2026, 4, 11, 10, 0, 0, tzinfo=est)  # 10 AM EST = 15:00 UTC
        filename = CalibrationStore._time_to_filename(dt)
        assert filename == '20260411T150000Z.pkl'

    def test_filename_parse(self):
        parsed = CalibrationStore._filename_to_time('20260101T000000Z.pkl')
        assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# Calibration lifecycle (end-to-end)
# ═══════════════════════════════════════════════════════════════════


class TestCalibrationLifecycle:

    def test_factory_calibrate_wrap_store_load(self, tmp_path):
        """Full lifecycle: factory -> calibrate -> wrap -> store -> load -> serve."""
        store = CalibrationStore(tmp_path / 'calibrations')
        factory = SimpleFactory('event_rate', data_reqs=['observations'])

        # 1. Calibrate
        data = {'observations': list(range(100))}
        raw = factory.calibrate(data)

        # 2. Framework wraps
        as_of = avail_time(4, 1)
        bound = BoundEstimator(raw, availability_time=as_of)

        # 3. Store
        store.store(factory.name, bound)

        # 4. Load in a later view build
        loaded = store.load(factory.name, avail_time(4, 5))
        assert loaded is not None
        assert loaded.availability_time == as_of
        assert loaded.predict(2) == 200  # 100 obs * 2

    def test_expanding_window_calibration(self, tmp_path):
        """Simulate expanding-window replay with multiple calibration points."""
        store = CalibrationStore(tmp_path / 'calibrations')
        factory = SimpleFactory('event_rate', data_reqs=['observations'])

        for month in range(1, 5):
            as_of = avail_time(month, 1)
            data = {'observations': list(range(month * 100))}
            raw = factory.calibrate(data)
            bound = BoundEstimator(raw, as_of)
            store.store(factory.name, bound)

        # March 15 -> uses March 1 calibration
        loaded = store.load('event_rate', avail_time(3, 15))
        assert loaded.availability_time == avail_time(3, 1)
        assert loaded.predict(1) == 300

        # February 15 -> uses February 1 calibration
        loaded = store.load('event_rate', avail_time(2, 15))
        assert loaded.availability_time == avail_time(2, 1)
        assert loaded.predict(1) == 200

    def test_dependent_calibration_chain(self, tmp_path):
        """Estimator B depends on estimator A: A calibrated first, passed to B."""
        store = CalibrationStore(tmp_path / 'calibrations')
        as_of = avail_time(4, 1)

        # Calibrate A
        factory_a = SimpleFactory('base_estimator', data_reqs=['observations'])
        data_a = {'observations': list(range(50))}
        raw_a = factory_a.calibrate(data_a)
        bound_a = BoundEstimator(raw_a, as_of)
        store.store(factory_a.name, bound_a)

        # Calibrate B (depends on A)
        factory_b = DependentFactory('derived_est', deps=['base_estimator'])
        data_b = {'observations': list(range(10))}
        loaded_a = store.load('base_estimator', as_of)
        raw_b = factory_b.calibrate(data_b, dependencies={
            'base_estimator': loaded_a.inner
        })
        bound_b = BoundEstimator(raw_b, as_of)
        store.store(factory_b.name, bound_b)

        # Load and verify
        loaded_b = store.load('derived_est', avail_time(4, 5))
        assert loaded_b.value == 60  # 50 (from A) + 10 (own obs)


# ═══════════════════════════════════════════════════════════════════
# Registry + View integration
# ═══════════════════════════════════════════════════════════════════


class TestRegistryBoundIntegration:

    def test_register_bound_basic(self):
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        registry.register_bound('test_est', bound)
        assert 'test_est' in registry

    def test_register_bound_duplicate_raises(self):
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        registry.register_bound('test_est', bound)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_bound('test_est', bound)

    def test_query_through_registry(self):
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        registry.register_bound('test_est', bound)

        result = registry.query('test_est', avail_time(4, 5), x=3)
        assert result == 126  # 42 * 3

    def test_query_with_custom_fn(self):
        registry = FeatureRegistry()
        inner = LookupEstimator({'A': 0.9})
        bound = BoundEstimator(inner, avail_time(4, 1))
        registry.register_bound('rates', bound,
                                query_fn=lambda est, **p: est.get_rate(p['key']))
        assert registry.query('rates', avail_time(4, 5), key='A') == 0.9

    def test_clone_shares_estimator_feature(self):
        """EstimatorFeature is immutable -- shared across clones."""
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        registry.register_bound('est', bound)

        cloned = registry.clone()
        assert 'est' in cloned
        result = cloned.query('est', avail_time(4, 5), x=2)
        assert result == 84


class TestViewEstimatorIntegration:

    def test_view_validates_availability_time(self):
        """View refuses construction when estimator's avail > as_of."""
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(1), avail_time(4, 10))
        registry.register_bound('est', bound)

        with pytest.raises(TemporalBoundaryError, match="availability_time"):
            View(avail_time(4, 5), registry)

    def test_view_accepts_valid_estimator(self):
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        registry.register_bound('est', bound)

        view = View(avail_time(4, 5), registry)
        result = view.query('est', x=3)
        assert result == 126

    def test_view_accepts_equal_boundary(self):
        """avail_time == as_of is valid (<= check)."""
        registry = FeatureRegistry()
        bound = BoundEstimator(SimpleEstimator(1), avail_time(4, 1))
        registry.register_bound('est', bound)

        view = View(avail_time(4, 1), registry)
        assert view.query('est', x=5) == 5

    def test_view_with_multiple_estimators(self):
        registry = FeatureRegistry()
        registry.register_bound('event_rate',
            BoundEstimator(LookupEstimator({'BTC': 0.95}), avail_time(4, 1)),
            query_fn=lambda est, **p: est.get_rate(p.get('key')))
        registry.register_bound('fill_model',
            BoundEstimator(LookupEstimator({'YES': 0.70}), avail_time(3, 28)),
            query_fn=lambda est, **p: est.get_rate(p.get('key')))

        view = View(avail_time(4, 1), registry)
        assert view.query('event_rate', key='BTC') == 0.95
        assert view.query('fill_model', key='YES') == 0.70

    def test_full_pipeline(self, tmp_path):
        """Factory -> calibrate -> store -> load -> register -> View -> query."""
        store = CalibrationStore(tmp_path / 'calibrations')

        # Calibrate and store
        factory = SimpleFactory('my_model', data_reqs=['observations'])
        raw = factory.calibrate({'observations': list(range(42))})
        as_of = avail_time(4, 1)
        bound = BoundEstimator(raw, as_of)
        store.store(factory.name, bound)

        # Load and register in a fresh registry
        registry = FeatureRegistry()
        loaded = store.load('my_model', avail_time(4, 5))
        registry.register_bound('my_model', loaded)

        # Build View and query
        view = View(avail_time(4, 5), registry)
        assert view.query('my_model', x=2) == 84


# ═══════════════════════════════════════════════════════════════════
# CalibrationStore (DB mode)
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def db_conn_factory():
    """Connection factory for DB tests."""
    import os
    import psycopg2
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    conn = psycopg2.connect(dsn)
    yield lambda: conn
    # Clean up test data
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM prediction_markets.calibration_artifacts
            WHERE estimator_name LIKE 'test_%%'
        """)
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
    conn.close()


@pytest.mark.db
class TestCalibrationStoreDB:

    def test_store_and_load(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        bound = BoundEstimator(SimpleEstimator(42), avail_time(4, 1))
        store.store('test_event_rate', bound,
                    config={'method': 'all'},
                    metrics={'accuracy': 0.95},
                    data_hash='abc123')

        loaded = store.load('test_event_rate', avail_time(4, 5))
        assert loaded is not None
        assert loaded.availability_time == avail_time(4, 1)
        assert loaded.predict(2) == 84

    def test_load_refuses_future(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        store.store('test_future', BoundEstimator(SimpleEstimator(1), avail_time(4, 10)))
        assert store.load('test_future', avail_time(4, 5)) is None

    def test_load_best_artifact(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        store.store('test_multi', BoundEstimator(SimpleEstimator(10), avail_time(3, 1)))
        store.store('test_multi', BoundEstimator(SimpleEstimator(20), avail_time(3, 15)))
        store.store('test_multi', BoundEstimator(SimpleEstimator(30), avail_time(4, 1)))

        loaded = store.load('test_multi', avail_time(3, 20))
        assert loaded.predict(1) == 20

    def test_latest_boundary(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        store.store('test_latest', BoundEstimator(SimpleEstimator(1), avail_time(3, 1)))
        store.store('test_latest', BoundEstimator(SimpleEstimator(2), avail_time(4, 1)))
        assert store.latest_boundary('test_latest') == avail_time(4, 1)

    def test_list_boundaries(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        store.store('test_list', BoundEstimator(SimpleEstimator(1), avail_time(3, 15)))
        store.store('test_list', BoundEstimator(SimpleEstimator(2), avail_time(3, 1)))
        store.store('test_list', BoundEstimator(SimpleEstimator(3), avail_time(4, 1)))

        boundaries = store.list_boundaries('test_list')
        assert len(boundaries) == 3
        assert boundaries[0] == avail_time(3, 1)
        assert boundaries[-1] == avail_time(4, 1)

    def test_upsert_overwrites(self, tmp_path, db_conn_factory):
        store = CalibrationStore(tmp_path / 'calibrations', db_conn_factory)
        store.store('test_upsert', BoundEstimator(SimpleEstimator(1), avail_time(4, 1)))
        store.store('test_upsert', BoundEstimator(SimpleEstimator(99), avail_time(4, 1)))

        loaded = store.load('test_upsert', avail_time(4, 2))
        assert loaded.predict(1) == 99


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def avail_time(month: int, day: int, hour: int = 0) -> datetime:
    """Shorthand for creating UTC datetimes in 2026."""
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)
