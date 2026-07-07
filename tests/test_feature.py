"""Tests for the feature framework.

Chunk 1: Feature protocol + StoredFeature
Chunk 2: FeatureRegistry + CachedFeature + ComputedFeature
Chunk 3: View (capability boundary)

Pure logic tests run without external dependencies.
DB tests require a live database connection and are marked @pytest.mark.db.
"""

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from framework.feature import (
    CachedFeature,
    ComputedFeature,
    Feature,
    FeatureRegistry,
    StoredFeature,
)
from framework.observations import Observation, ObservationsFeature
from framework.view import TemporalBoundaryError, View


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestFeatureProtocol:
    """Feature protocol is runtime-checkable and StoredFeature satisfies it."""

    def test_stored_feature_is_feature(self):
        sf = StoredFeature("test", "some_table", "created_at", lambda: None)
        assert isinstance(sf, Feature)

    def test_observations_feature_is_feature(self):
        of = ObservationsFeature(lambda: None)
        assert isinstance(of, Feature)

    def test_arbitrary_object_not_feature(self):
        assert not isinstance("hello", Feature)
        assert not isinstance(42, Feature)

    def test_custom_class_with_protocol(self):
        """A custom class implementing name + query satisfies Feature."""
        class MyFeature:
            @property
            def name(self):
                return "custom"
            def query(self, as_of, **params):
                return None

        assert isinstance(MyFeature(), Feature)

    def test_incomplete_class_not_feature(self):
        """Missing query() means not a Feature."""
        class Incomplete:
            @property
            def name(self):
                return "broken"

        assert not isinstance(Incomplete(), Feature)


# ---------------------------------------------------------------------------
# StoredFeature construction and properties
# ---------------------------------------------------------------------------


class TestStoredFeature:

    def test_name_property(self):
        sf = StoredFeature("trades", "pm.kalshi_trades", "created_time", lambda: None)
        assert sf.name == "trades"

    def test_table_property(self):
        sf = StoredFeature("trades", "pm.kalshi_trades", "created_time", lambda: None)
        assert sf.table == "pm.kalshi_trades"

    def test_availability_column_property(self):
        sf = StoredFeature("trades", "pm.kalshi_trades", "created_time", lambda: None)
        assert sf.availability_column == "created_time"

    def test_repr(self):
        sf = StoredFeature("trades", "pm.kalshi_trades", "created_time", lambda: None)
        assert "trades" in repr(sf)
        assert "pm.kalshi_trades" in repr(sf)

    def test_default_query_uses_strict_less_than(self):
        """Default query() builds SQL with strict < (not <=)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        sf = StoredFeature("test", "my_table", "avail_time", lambda: mock_conn)
        as_of = datetime(2026, 3, 15, tzinfo=timezone.utc)
        sf.query(as_of)

        # Verify the SQL uses strict <
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        assert "< %s" in sql
        assert "<= %s" not in sql
        assert call_args[0][1] == (as_of,)

    def test_default_query_returns_fetchall(self):
        """Default query() returns cursor.fetchall() result."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        expected = [(1, "a"), (2, "b")]
        mock_cursor.fetchall.return_value = expected

        sf = StoredFeature("test", "my_table", "ts", lambda: mock_conn)
        result = sf.query(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == expected

    def test_default_query_closes_cursor(self):
        """Cursor is closed even on success."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        sf = StoredFeature("test", "t", "c", lambda: mock_conn)
        sf.query(datetime(2026, 1, 1, tzinfo=timezone.utc))
        mock_cursor.close.assert_called_once()

    def test_default_query_closes_cursor_on_error(self):
        """Cursor is closed even when execute raises."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = Exception("db error")

        sf = StoredFeature("test", "t", "c", lambda: mock_conn)
        with pytest.raises(Exception, match="db error"):
            sf.query(datetime(2026, 1, 1, tzinfo=timezone.utc))
        mock_cursor.close.assert_called_once()


# ---------------------------------------------------------------------------
# Observation dataclass
# ---------------------------------------------------------------------------


class TestObservation:

    def test_construction(self):
        obs = Observation(
            ticker="KXBTC-26MAR27-T60000",
            series="KXBTC",
            settled_at=datetime(2026, 3, 27, tzinfo=timezone.utc),
            yes_bid=0.88,
            yes_ask=0.92,
            yes_mid=0.90,
            trade_price=0.91,
            result_yes=True,
            hours_to_settlement=24.0,
            generating_process="continuous_underlyer",
            topic="financial",
        )
        assert obs.ticker == "KXBTC-26MAR27-T60000"
        assert obs.result_yes is True
        assert obs.yes_mid == 0.90

    def test_slots(self):
        """Observation uses __slots__ for memory efficiency."""
        assert hasattr(Observation, '__slots__')


# ---------------------------------------------------------------------------
# ObservationsFeature construction
# ---------------------------------------------------------------------------


class TestObservationsFeature:

    def test_name(self):
        of = ObservationsFeature(lambda: None)
        assert of.name == "observations"

    def test_availability_column(self):
        of = ObservationsFeature(lambda: None)
        assert of.availability_column == "settled_at"

    def test_repr(self):
        of = ObservationsFeature(lambda: None)
        assert "ObservationsFeature" in repr(of)


# ---------------------------------------------------------------------------
# DB integration tests
# ---------------------------------------------------------------------------


def get_test_conn():
    """Get a database connection for testing."""
    import psycopg2
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    cur.close()
    return conn


@pytest.mark.db
class TestObservationsFeatureDB:
    """Integration tests requiring a live database."""

    @pytest.fixture(autouse=True)
    def setup_conn(self):
        self._conn = get_test_conn()
        yield
        try:
            self._conn.rollback()
        except Exception:
            pass
        self._conn.close()

    def conn_factory(self):
        return self._conn

    def test_query_returns_observations(self):
        """query() returns a list of Observation objects."""
        feature = ObservationsFeature(self.conn_factory)
        # Use a recent date to get some data
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
        result = feature.query(as_of)

        assert isinstance(result, list)
        assert len(result) > 0, "Expected observations in database"
        assert isinstance(result[0], Observation)

    def test_temporal_boundary_strict_less_than(self):
        """Data at exactly as_of is excluded (strict <)."""
        feature = ObservationsFeature(self.conn_factory)

        # Find a specific settled_at time
        cur = self._conn.cursor()
        cur.execute("""
            SELECT settled_at
            FROM prediction_markets.kalshi_settled_markets
            WHERE result IN ('yes', 'no') AND settled_at IS NOT NULL
            ORDER BY settled_at
            LIMIT 1 OFFSET 100
        """)
        row = cur.fetchone()
        cur.close()
        assert row is not None, "Need settled markets in DB"
        boundary_time = row[0]

        result = feature.query(boundary_time)
        for obs in result:
            assert obs.settled_at < boundary_time, (
                f"Observation settled_at={obs.settled_at} >= as_of={boundary_time}"
            )

    def test_earlier_cutoff_returns_fewer(self):
        """An earlier as_of returns a subset of a later as_of's results."""
        feature = ObservationsFeature(self.conn_factory)

        early = datetime(2025, 10, 1, tzinfo=timezone.utc)
        late = datetime(2026, 4, 1, tzinfo=timezone.utc)

        early_result = feature.query(early)
        late_result = feature.query(late)

        assert len(early_result) < len(late_result), (
            f"Expected fewer observations at {early.date()} ({len(early_result)}) "
            f"than at {late.date()} ({len(late_result)})"
        )

    def test_very_early_cutoff_returns_empty(self):
        """as_of before any data returns empty list."""
        feature = ObservationsFeature(self.conn_factory)
        result = feature.query(datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert result == []

    def test_observations_have_valid_fields(self):
        """Spot-check that returned observations have reasonable values."""
        feature = ObservationsFeature(self.conn_factory)
        result = feature.query(datetime(2026, 2, 1, tzinfo=timezone.utc))
        if not result:
            pytest.skip("No observations before 2026-02-01")

        for obs in result[:100]:  # check first 100
            assert 0 < obs.yes_bid <= 1.0
            assert 0 < obs.yes_ask <= 1.0
            assert obs.yes_bid <= obs.yes_ask
            assert 0 < obs.yes_mid < 1.0
            assert obs.hours_to_settlement >= 0
            assert obs.generating_process
            assert obs.topic
            assert isinstance(obs.result_yes, bool)

    def test_compatible_with_event_rate_estimator(self):
        """Observations can be passed to EventRateEstimator.calibrate()."""
        from trading.event_rate import EventRateEstimator

        feature = ObservationsFeature(self.conn_factory)
        observations = feature.query(datetime(2026, 2, 1, tzinfo=timezone.utc))
        if not observations:
            pytest.skip("No observations before 2026-02-01")

        estimator = EventRateEstimator()
        # EventRateEstimator needs classifications set
        series_map = {}
        for obs in observations:
            if obs.series not in series_map:
                series_map[obs.series] = (obs.generating_process, obs.topic)
        estimator.set_classifications(series_map)

        # This should not raise
        estimator.calibrate(observations)

        # Verify it actually produced rate cells
        total_cells = sum(len(v) for v in estimator.rates.values())
        assert total_cells > 0, "EventRateEstimator should produce rate cells"


# ===========================================================================
# Chunk 2: FeatureRegistry + CachedFeature + ComputedFeature
# ===========================================================================


# ---------------------------------------------------------------------------
# Test helpers: lightweight features for pure tests
# ---------------------------------------------------------------------------


class ConstantFeature:
    """A feature that returns a constant value. For testing."""

    def __init__(self, name: str, value: Any):
        self._name = name
        self._value = value

    @property
    def name(self) -> str:
        return self._name

    def query(self, as_of: datetime, **params) -> Any:
        return self._value


class RecordingFeature:
    """A feature that records what as_of values it was queried with."""

    def __init__(self, name: str, value: Any):
        self._name = name
        self._value = value
        self.queries: list[datetime] = []

    @property
    def name(self) -> str:
        return self._name

    def query(self, as_of: datetime, **params) -> Any:
        self.queries.append(as_of)
        return self._value


# ---------------------------------------------------------------------------
# FeatureRegistry
# ---------------------------------------------------------------------------


class TestFeatureRegistry:

    def test_register_and_query(self):
        reg = FeatureRegistry()
        feat = ConstantFeature("alpha", 42)
        reg.register(feat)
        assert reg.query("alpha", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 42

    def test_contains(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("alpha", 1))
        assert "alpha" in reg
        assert "beta" not in reg

    def test_len(self):
        reg = FeatureRegistry()
        assert len(reg) == 0
        reg.register(ConstantFeature("a", 1))
        reg.register(ConstantFeature("b", 2))
        assert len(reg) == 2

    def test_duplicate_name_raises(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("alpha", 1))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(ConstantFeature("alpha", 2))

    def test_query_unknown_raises(self):
        reg = FeatureRegistry()
        with pytest.raises(KeyError, match="not registered"):
            reg.query("nope", datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_repr(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        assert "1 features" in repr(reg)


class TestRegistryValidation:

    def test_valid_no_deps(self):
        """Registry with only stored features (no deps) validates fine."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        reg.register(ConstantFeature("b", 2))
        reg.validate()  # should not raise

    def test_missing_dependency_raises(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("base", [1, 2, 3]))
        computed = ComputedFeature("derived", ["base", "missing"],
                                   lambda deps, **p: None, reg)
        reg.register(computed)
        with pytest.raises(ValueError, match="missing.*not registered"):
            reg.validate()

    def test_circular_dependency_raises(self):
        reg = FeatureRegistry()
        # A depends on B, B depends on A
        a = ComputedFeature("a", ["b"], lambda d, **p: None, reg)
        b = ComputedFeature("b", ["a"], lambda d, **p: None, reg)
        reg.register(a)
        reg.register(b)
        with pytest.raises(ValueError, match="Circular dependency"):
            reg.validate()

    def test_self_dependency_raises(self):
        reg = FeatureRegistry()
        a = ComputedFeature("a", ["a"], lambda d, **p: None, reg)
        reg.register(a)
        with pytest.raises(ValueError, match="Circular dependency"):
            reg.validate()

    def test_longer_cycle_raises(self):
        reg = FeatureRegistry()
        a = ComputedFeature("a", ["c"], lambda d, **p: None, reg)
        b = ComputedFeature("b", ["a"], lambda d, **p: None, reg)
        c = ComputedFeature("c", ["b"], lambda d, **p: None, reg)
        reg.register(a)
        reg.register(b)
        reg.register(c)
        with pytest.raises(ValueError, match="Circular dependency"):
            reg.validate()


class TestDependencyOrder:

    def test_no_deps_returns_all(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        reg.register(ConstantFeature("b", 2))
        order = reg.dependency_order()
        assert set(order) == {"a", "b"}

    def test_deps_come_first(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("base", 1))
        computed = ComputedFeature("derived", ["base"],
                                   lambda deps, **p: deps["base"] * 2, reg)
        reg.register(computed)
        order = reg.dependency_order()
        assert order.index("base") < order.index("derived")

    def test_diamond_dependency(self):
        """D depends on B and C, both depend on A."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        b = ComputedFeature("b", ["a"], lambda d, **p: None, reg)
        c = ComputedFeature("c", ["a"], lambda d, **p: None, reg)
        d = ComputedFeature("d", ["b", "c"], lambda d, **p: None, reg)
        reg.register(b)
        reg.register(c)
        reg.register(d)
        order = reg.dependency_order()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_deterministic(self):
        """Same input produces same output across multiple calls."""
        reg = FeatureRegistry()
        for name in ["z", "y", "x", "w"]:
            reg.register(ConstantFeature(name, 1))
        order1 = reg.dependency_order()
        order2 = reg.dependency_order()
        assert order1 == order2


class TestRegistryClone:

    def test_clone_has_same_features(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        reg.register(ConstantFeature("b", 2))
        clone = reg.clone()
        assert "a" in clone
        assert "b" in clone
        assert len(clone) == 2

    def test_clone_is_independent(self):
        """Adding to clone does not affect original."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        clone = reg.clone()
        clone.register(ConstantFeature("b", 2))
        assert "b" in clone
        assert "b" not in reg

    def test_clone_original_independent(self):
        """Adding to original does not affect clone."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        clone = reg.clone()
        reg.register(ConstantFeature("c", 3))
        assert "c" in reg
        assert "c" not in clone

    def test_clone_shares_stored_features(self):
        """StoredFeatures (no mutable state) are shared, not copied."""
        reg = FeatureRegistry()
        feat = ConstantFeature("a", 1)
        reg.register(feat)
        clone = reg.clone()
        assert clone.query("a", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 1

    def test_clone_computed_resolves_in_clone(self):
        """ComputedFeature in clone resolves deps added to the clone."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("base", 10))
        reg.register(ComputedFeature(
            "derived", ["base", "extra"],
            lambda deps, **p: deps["base"] + deps["extra"],
            reg,
        ))

        clone = reg.clone()
        # "extra" only exists in clone — derived must see it
        clone.register(ConstantFeature("extra", 5))

        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert clone.query("derived", t) == 15

    def test_clone_cached_gets_fresh_cache(self):
        """CachedFeature in clone has an independent empty cache."""
        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"v": call_count})

        reg = FeatureRegistry()
        reg.register(CachedFeature("cached", counting_fn, [], reg))

        t = datetime(2026, 3, 15, tzinfo=timezone.utc)

        # Query in original — populates original's cache
        reg.query("cached", t, key="v")
        assert call_count == 1

        # Clone gets fresh cache — must recompute
        clone = reg.clone()
        clone.query("cached", t, key="v")
        assert call_count == 2

    def test_clone_cached_resolves_in_clone(self):
        """CachedFeature in clone resolves deps through the clone's registry."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("src", [1, 2, 3]))
        reg.register(CachedFeature(
            "agg",
            lambda deps: _QueryableResult({"sum": sum(deps["src"])}),
            ["src"], reg,
        ))

        clone = reg.clone()
        # Override "src" in clone with different data
        clone._features["src"] = ConstantFeature("src", [10, 20])

        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Clone's CachedFeature should see the clone's "src", not the original's
        assert clone.query("agg", t, key="sum") == 30


# ---------------------------------------------------------------------------
# ComputedFeature
# ---------------------------------------------------------------------------


class TestComputedFeature:

    def test_protocol_compliance(self):
        reg = FeatureRegistry()
        cf = ComputedFeature("x", [], lambda d, **p: 0, reg)
        assert isinstance(cf, Feature)

    def test_derives_from_two_stored_features(self):
        """Core Chunk 2 test: computed feature resolves from stored features."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("bid", 90))
        reg.register(ConstantFeature("ask", 95))
        spread = ComputedFeature(
            "spread", ["bid", "ask"],
            lambda deps, **p: deps["ask"] - deps["bid"],
            reg,
        )
        reg.register(spread)

        result = reg.query("spread", datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result == 5

    def test_chained_computation(self):
        """Computed feature depending on another computed feature."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("price", 10))
        doubled = ComputedFeature(
            "doubled", ["price"],
            lambda deps, **p: deps["price"] * 2,
            reg,
        )
        reg.register(doubled)
        quadrupled = ComputedFeature(
            "quadrupled", ["doubled"],
            lambda deps, **p: deps["doubled"] * 2,
            reg,
        )
        reg.register(quadrupled)

        assert reg.query("quadrupled", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 40

    def test_passes_params_to_compute_fn(self):
        """Extra params reach the compute_fn."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("data", {"a": 1, "b": 2}))
        lookup = ComputedFeature(
            "lookup", ["data"],
            lambda deps, **p: deps["data"].get(p.get("key")),
            reg,
        )
        reg.register(lookup)

        assert reg.query("lookup", datetime(2026, 1, 1, tzinfo=timezone.utc),
                         key="a") == 1
        assert reg.query("lookup", datetime(2026, 1, 1, tzinfo=timezone.utc),
                         key="b") == 2

    def test_compute_fn_never_sees_as_of(self):
        """Structural enforcement: compute_fn args don't include as_of."""
        received_args = {}

        def capturing_fn(deps, **params):
            received_args['deps'] = deps
            received_args['params'] = params
            return 42

        reg = FeatureRegistry()
        reg.register(ConstantFeature("src", "data"))
        cf = ComputedFeature("test", ["src"], capturing_fn, reg)
        reg.register(cf)
        reg.query("test", datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc))

        assert "as_of" not in received_args['params']
        assert set(received_args['deps'].keys()) == {"src"}

    def test_no_dependencies(self):
        """Computed feature with no deps still works (constant producer)."""
        reg = FeatureRegistry()
        cf = ComputedFeature("const", [], lambda deps, **p: 99, reg)
        reg.register(cf)
        assert reg.query("const", datetime(2026, 1, 1, tzinfo=timezone.utc)) == 99

    def test_params_not_forwarded_to_dependencies(self):
        """Intentional: caller's params are NOT forwarded to dependency resolution.

        Spec Section 2.2.3 shows params forwarded, but the implementation
        intentionally omits this. Params are feature-specific; forwarding
        them to unrelated deps would cause errors or silent wrong results.
        The compute_fn receives params and handles the mapping.
        """
        dep_received_params = {}

        class ParamRecordingFeature:
            @property
            def name(self):
                return "dep"
            def query(self, as_of, **params):
                dep_received_params.update(params)
                return 42

        reg = FeatureRegistry()
        reg.register(ParamRecordingFeature())
        cf = ComputedFeature(
            "derived", ["dep"],
            lambda deps, **p: deps["dep"] + p.get("bonus", 0),
            reg,
        )
        reg.register(cf)

        result = reg.query("derived", datetime(2026, 1, 1, tzinfo=timezone.utc),
                           bonus=10)
        assert result == 52
        assert dep_received_params == {}, (
            "Dependency should NOT receive caller's params"
        )


# ---------------------------------------------------------------------------
# CachedFeature
# ---------------------------------------------------------------------------


class _QueryableResult:
    """Test helper: an object with a .query() method."""

    def __init__(self, data: dict):
        self._data = data

    def query(self, **params):
        key = params.get("key")
        return self._data.get(key)


class TestCachedFeature:

    def test_protocol_compliance(self):
        reg = FeatureRegistry()
        cf = CachedFeature("x", lambda d: _QueryableResult({}), [], reg)
        assert isinstance(cf, Feature)

    def test_caches_per_bucket(self):
        """compute_fn called once per daily bucket, not per query."""
        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"key": call_count})

        reg = FeatureRegistry()
        cf = CachedFeature("cached", counting_fn, [], reg, bucket_granularity='daily')
        reg.register(cf)

        t1 = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 15, 22, 0, tzinfo=timezone.utc)

        r1 = cf.query(t1, key="key")
        r2 = cf.query(t2, key="key")

        assert call_count == 1, "Should compute only once for same day"
        assert r1 == r2

    def test_different_days_recompute(self):
        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"v": call_count})

        reg = FeatureRegistry()
        cf = CachedFeature("cached", counting_fn, [], reg)
        reg.register(cf)

        cf.query(datetime(2026, 3, 15, tzinfo=timezone.utc), v="v")
        cf.query(datetime(2026, 3, 16, tzinfo=timezone.utc), v="v")

        assert call_count == 2

    def test_hourly_granularity(self):
        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"v": call_count})

        reg = FeatureRegistry()
        cf = CachedFeature("cached", counting_fn, [], reg,
                           bucket_granularity='hourly')
        reg.register(cf)

        cf.query(datetime(2026, 3, 15, 10, 15, tzinfo=timezone.utc), v="v")
        cf.query(datetime(2026, 3, 15, 10, 45, tzinfo=timezone.utc), v="v")
        cf.query(datetime(2026, 3, 15, 11, 0, tzinfo=timezone.utc), v="v")

        assert call_count == 2, "Same hour = 1 call, next hour = 2nd call"

    def test_bucket_floor_determinism(self):
        """Queries at different times in same bucket resolve deps at floor."""
        reg = FeatureRegistry()
        recorder = RecordingFeature("source", [1, 2, 3])
        reg.register(recorder)

        cf = CachedFeature(
            "cached",
            lambda deps: _QueryableResult({"val": len(deps["source"])}),
            ["source"],
            reg,
        )
        reg.register(cf)

        # Query at 22:00, then 10:00 — both should resolve at midnight
        cf.query(datetime(2026, 3, 15, 22, 0, tzinfo=timezone.utc), val="val")

        assert len(recorder.queries) == 1
        assert recorder.queries[0].hour == 0  # bucket floor = midnight
        assert recorder.queries[0].minute == 0

    def test_resolves_dependencies(self):
        """CachedFeature resolves deps through the registry."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("input", [10, 20, 30]))

        def sum_fn(deps):
            total = sum(deps["input"])
            return _QueryableResult({"total": total})

        cf = CachedFeature("agg", sum_fn, ["input"], reg)
        reg.register(cf)

        result = cf.query(datetime(2026, 1, 1, tzinfo=timezone.utc), key="total")
        assert result == 60

    def test_compute_fn_never_sees_as_of(self):
        """Structural enforcement: compute_fn only receives resolved deps."""
        received = {}

        def capturing_fn(deps):
            received['deps'] = deps
            return _QueryableResult({})

        reg = FeatureRegistry()
        reg.register(ConstantFeature("src", "data"))
        cf = CachedFeature("test", capturing_fn, ["src"], reg)
        reg.register(cf)
        cf.query(datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc))

        assert set(received['deps'].keys()) == {"src"}
        # No as_of, no registry, no connection in what compute_fn received

    def test_clear_cache(self):
        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"v": call_count})

        reg = FeatureRegistry()
        cf = CachedFeature("cached", counting_fn, [], reg)
        reg.register(cf)

        t = datetime(2026, 3, 15, tzinfo=timezone.utc)
        cf.query(t, v="v")
        assert call_count == 1
        cf.clear_cache()
        cf.query(t, v="v")
        assert call_count == 2

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetimes are assumed UTC for bucketing."""
        reg = FeatureRegistry()
        recorder = RecordingFeature("src", "data")
        reg.register(recorder)

        cf = CachedFeature(
            "cached",
            lambda deps: _QueryableResult({"v": 1}),
            ["src"], reg,
        )
        reg.register(cf)

        naive_t = datetime(2026, 3, 15, 14, 30)  # no tzinfo
        cf.query(naive_t, v="v")

        # Should resolve at midnight UTC
        assert recorder.queries[0].hour == 0
        assert recorder.queries[0].tzinfo == timezone.utc

    def test_different_timezones_same_instant_same_bucket(self):
        """Same absolute instant in different timezones hits the same bucket."""
        from datetime import timedelta

        call_count = 0

        def counting_fn(deps):
            nonlocal call_count
            call_count += 1
            return _QueryableResult({"v": call_count})

        reg = FeatureRegistry()
        cf = CachedFeature("cached", counting_fn, [], reg)
        reg.register(cf)

        # 2026-03-15 10:00 UTC and 2026-03-15 05:00 EST (same instant)
        est = timezone(timedelta(hours=-5))
        utc_time = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        est_time = datetime(2026, 3, 15, 5, 0, tzinfo=est)

        cf.query(utc_time, v="v")
        cf.query(est_time, v="v")

        assert call_count == 1, "Same instant should hit same daily bucket"


# ---------------------------------------------------------------------------
# Integration: registry + all feature types together
# ---------------------------------------------------------------------------


class TestRegistryIntegration:

    def test_mixed_feature_types(self):
        """Registry with stored, computed, and cached features validates."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("raw_data", [1, 2, 3]))
        reg.register(ComputedFeature(
            "derived", ["raw_data"],
            lambda deps, **p: sum(deps["raw_data"]),
            reg,
        ))
        reg.register(CachedFeature(
            "expensive", lambda deps: _QueryableResult({"v": 99}),
            ["raw_data"], reg,
        ))
        reg.validate()
        order = reg.dependency_order()
        assert order[0] == "raw_data"

    def test_end_to_end_computation(self):
        """Full pipeline: stored -> computed -> computed (all plain values)."""
        reg = FeatureRegistry()

        # Layer 0: raw data
        reg.register(ConstantFeature("prices", [90, 92, 88, 95]))

        # Layer 1: computed aggregation (returns plain value)
        def compute_mean(deps, **params):
            prices = deps["prices"]
            return sum(prices) / len(prices)

        reg.register(ComputedFeature("mean_price", ["prices"], compute_mean, reg))

        # Layer 2: computed derivation
        def is_high(deps, **params):
            threshold = params.get("threshold", 90)
            mean = deps["mean_price"]
            return mean > threshold

        reg.register(ComputedFeature("is_high", ["mean_price"], is_high, reg))

        # Validate dependency graph
        reg.validate()
        order = reg.dependency_order()
        assert order.index("prices") < order.index("mean_price")
        assert order.index("mean_price") < order.index("is_high")

        # Query through the chain
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert reg.query("mean_price", t) == 91.25
        assert reg.query("is_high", t, threshold=90) is True
        assert reg.query("is_high", t, threshold=92) is False

    def test_cached_feature_queried_with_params(self):
        """CachedFeature queried directly through registry passes params."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("raw", [1, 2, 3]))

        def make_lookup(deps):
            data = deps["raw"]
            return _QueryableResult({
                "sum": sum(data),
                "count": len(data),
            })

        reg.register(CachedFeature("lookup", make_lookup, ["raw"], reg))
        reg.validate()

        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert reg.query("lookup", t, key="sum") == 6
        assert reg.query("lookup", t, key="count") == 3


# ===========================================================================
# Chunk 3: View (capability boundary)
# ===========================================================================


# ---------------------------------------------------------------------------
# Test helpers for View tests
# ---------------------------------------------------------------------------


class FakeEstimatorFeature:
    """Feature with an availability_time, simulating a BoundEstimator."""

    def __init__(self, name: str, availability_time: datetime, value: Any = None):
        self._name = name
        self.availability_time = availability_time
        self._value = value

    @property
    def name(self) -> str:
        return self._name

    def query(self, as_of: datetime, **params) -> Any:
        return self._value


class FakeCostModel:
    """Minimal CostModel for testing View.cost() delegation."""

    def maker_fee(self, price_cents: int, contracts: int = 1) -> int:
        return 2 * contracts

    def taker_fee(self, price_cents: int, contracts: int = 1) -> int:
        return 8 * contracts


# ---------------------------------------------------------------------------
# View construction and validation
# ---------------------------------------------------------------------------


class TestViewConstruction:

    def test_basic_construction(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("data", [1, 2, 3]))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert view is not None

    def test_repr(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("a", 1))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert "2026-03-15" in repr(view)
        assert "1 features" in repr(view)

    def test_stats(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("raw", 1))
        reg.register(FakeEstimatorFeature(
            "model", datetime(2026, 3, 1, tzinfo=timezone.utc), value=42))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        stats = view.stats
        assert "2 features" in stats
        assert "1 temporally bounded" in stats


class TestViewValidation:

    def test_valid_estimator_accepted(self):
        """Estimator with availability_time <= as_of passes validation."""
        reg = FeatureRegistry()
        reg.register(FakeEstimatorFeature(
            "model", datetime(2026, 3, 1, tzinfo=timezone.utc)))
        # as_of is after availability_time — should be fine
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert view is not None

    def test_estimator_at_exact_boundary_accepted(self):
        """Estimator with availability_time == as_of passes (<=)."""
        t = datetime(2026, 3, 15, tzinfo=timezone.utc)
        reg = FeatureRegistry()
        reg.register(FakeEstimatorFeature("model", t))
        view = View(t, reg)
        assert view is not None

    def test_future_estimator_rejected(self):
        """Estimator with availability_time > as_of raises TemporalBoundaryError."""
        reg = FeatureRegistry()
        reg.register(FakeEstimatorFeature(
            "future_model", datetime(2026, 4, 1, tzinfo=timezone.utc)))
        with pytest.raises(TemporalBoundaryError, match="future_model"):
            View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)

    def test_multiple_estimators_all_checked(self):
        """All estimators are validated, not just the first."""
        reg = FeatureRegistry()
        reg.register(FakeEstimatorFeature(
            "good", datetime(2026, 3, 1, tzinfo=timezone.utc)))
        reg.register(FakeEstimatorFeature(
            "bad", datetime(2026, 5, 1, tzinfo=timezone.utc)))
        with pytest.raises(TemporalBoundaryError, match="bad"):
            View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)

    def test_features_without_availability_time_ignored(self):
        """StoredFeatures (no availability_time) don't trigger validation."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("raw", [1, 2, 3]))
        reg.register(FakeEstimatorFeature(
            "model", datetime(2026, 3, 1, tzinfo=timezone.utc)))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert view is not None


# ---------------------------------------------------------------------------
# Typed methods delegate to registry
# ---------------------------------------------------------------------------


class TestViewTypedMethods:

    def _make_view(self):
        """Build a view with features matching the typed method names."""
        reg = FeatureRegistry()
        reg.register(ConstantFeature("event_rate", (0.85, 0.02, 150)))
        reg.register(ConstantFeature("fill_model", {"p_fill_won": 0.6}))
        reg.register(ConstantFeature("classification",
                                     ("continuous_underlyer", "financial")))
        return View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg,
                    costs=FakeCostModel())

    def test_event_rate(self):
        view = self._make_view()
        result = view.event_rate("KXBTC", 24.0)
        assert result == (0.85, 0.02, 150)

    def test_fill_probability(self):
        view = self._make_view()
        result = view.fill_probability("yes", 90, 1, {"bid": 89, "ask": 92})
        assert result == {"p_fill_won": 0.6}

    def test_classification(self):
        view = self._make_view()
        result = view.classification("KXBTC")
        assert result == ("continuous_underlyer", "financial")

    def test_missing_feature_raises(self):
        """Typed method for unregistered feature raises KeyError."""
        reg = FeatureRegistry()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        with pytest.raises(KeyError):
            view.event_rate("KXBTC", 24.0)


# ---------------------------------------------------------------------------
# Cost delegation
# ---------------------------------------------------------------------------


class TestViewCost:

    def test_maker_fee(self):
        reg = FeatureRegistry()
        costs = FakeCostModel()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg, costs=costs)
        assert view.cost(90, 3, is_maker=True) == 6  # 2 * 3

    def test_taker_fee(self):
        reg = FeatureRegistry()
        costs = FakeCostModel()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg, costs=costs)
        assert view.cost(90, 3, is_maker=False) == 24  # 8 * 3
        assert view.cost(90, 3, is_maker=False) > view.cost(90, 3, is_maker=True)

    def test_no_cost_model_raises(self):
        reg = FeatureRegistry()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg, costs=None)
        with pytest.raises(RuntimeError, match="No CostModel"):
            view.cost(90, 1)


# ---------------------------------------------------------------------------
# Generic query escape hatch
# ---------------------------------------------------------------------------


class TestViewGenericQuery:

    def test_query_delegates_to_registry(self):
        reg = FeatureRegistry()
        reg.register(ConstantFeature("custom_metric", 42))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert view.query("custom_metric") == 42

    def test_query_passes_params(self):
        reg = FeatureRegistry()
        reg.register(ComputedFeature(
            "lookup", [],
            lambda deps, **p: p.get("key", "default"),
            reg,
        ))
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert view.query("lookup", key="hello") == "hello"


# ---------------------------------------------------------------------------
# as_of privacy (conventional enforcement)
# ---------------------------------------------------------------------------


class TestViewPrivacy:

    def test_as_of_not_in_public_interface(self):
        """as_of is not exposed as a public attribute."""
        reg = FeatureRegistry()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        # _as_of exists (private) but as_of does not (public)
        assert not hasattr(view, 'as_of')
        assert hasattr(view, '_as_of')

    def test_registry_not_in_public_interface(self):
        """Registry is not exposed as a public attribute."""
        reg = FeatureRegistry()
        view = View(datetime(2026, 3, 15, tzinfo=timezone.utc), reg)
        assert not hasattr(view, 'registry')
        assert hasattr(view, '_registry')

    def test_view_queries_use_internal_as_of(self):
        """All queries go through the view's private as_of."""
        reg = FeatureRegistry()
        recorder = RecordingFeature("data", [1, 2, 3])
        reg.register(recorder)

        as_of = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        view = View(as_of, reg)
        view.query("data")

        assert len(recorder.queries) == 1
        assert recorder.queries[0] == as_of
