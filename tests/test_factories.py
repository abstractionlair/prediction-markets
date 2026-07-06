"""Tests for Chunk 6: EstimatorFactory wrappers for existing estimators.

Tests the bridge between the framework's EstimatorFactory protocol and
the existing EventRateEstimator, FillRateEstimator, and classification lookup.

Test categories:
- Protocol compliance: factories satisfy EstimatorFactory
- Calibration: factories produce working estimators from synthetic data
- Query functions: parameter translation from View interface to estimator API
- ViewFactory integration: build() wires factories into a working View
- Equivalence: framework View produces same results as hand-constructed MarketView
"""

import pytest
from datetime import datetime, timezone, timedelta

from framework.estimator import BoundEstimator, EstimatorFactory, EstimatorFeature
from framework.factories import (
    EventRateFactory, ClassificationFactory, FillRateFactory, FillEstimate,
)
from framework.feature import FeatureRegistry
from framework.view import View
from framework.view_factory import ViewFactory


# ── Test helpers ─────────────────────────────────────────────────


class Obs:
    """Lightweight observation for testing EventRateEstimator."""
    __slots__ = ('ticker', 'series', 'settled_at', 'yes_bid', 'yes_ask',
                 'yes_mid', 'trade_price', 'result_yes', 'hours_to_settlement',
                 'generating_process', 'topic')


def make_observations(n_tickers=60, price=0.92, gp='continuous_underlyer',
                      topic='financial', settled_at=None):
    """Generate synthetic observations for EventRateEstimator.

    Creates observations in a single (gp, topic, price_bucket) cell with
    enough unique tickers to exceed MIN_TOTAL_MARKETS (50).
    """
    if settled_at is None:
        settled_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    obs = []
    for i in range(n_tickers):
        for h_mult in range(1, 4):
            o = Obs()
            o.ticker = f'TEST-{i:03d}'
            o.series = 'TEST'
            o.settled_at = settled_at
            o.yes_bid = price - 0.02
            o.yes_ask = price + 0.02
            o.yes_mid = price
            o.trade_price = price
            o.result_yes = (i % 5 != 0)  # ~80% win rate
            o.hours_to_settlement = h_mult * 10.0
            o.generating_process = gp
            o.topic = topic
            obs.append(o)
    return obs


CLASSIFICATIONS = {'TEST': ('continuous_underlyer', 'financial')}


class FilteredListFeature:
    """Test helper: serves observations filtered by settled_at < as_of."""

    def __init__(self, name, items):
        self._name = name
        self._items = items

    @property
    def name(self):
        return self._name

    def query(self, as_of, **params):
        return [item for item in self._items if item.settled_at < as_of]


class ConstantFeature:
    """Test helper: serves a constant value regardless of as_of."""

    def __init__(self, name, value):
        self._name = name
        self._value = value

    @property
    def name(self):
        return self._name

    def query(self, as_of, **params):
        return self._value


class MockFillRateEstimator:
    """Mock FillRateEstimator for testing query_fn parameter translation."""

    def __init__(self, rates=None):
        self.rates = rates or {}
        self.last_call = None

    def get_fill_rates(self, gp, topic, hours_to_settlement,
                       relative_price, side):
        self.last_call = {
            'gp': gp, 'topic': topic, 'hours': hours_to_settlement,
            'relative_price': relative_price, 'side': side,
        }
        key = (gp, topic, side)
        if key in self.rates:
            return self.rates[key]
        return None


def make_fill_data(n_tickers=20, gp='continuous_underlyer', topic='financial'):
    """Generate synthetic fill data for FillRateEstimator testing.

    Creates ticker_data with enough candles and tickers to exceed
    min_per_cell (30) in the fill rate calibration.
    """
    from fill_model import CandleData

    settled_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    ticker_data = {}

    for i in range(n_tickers):
        # Alternate yes/no results for variety
        result = 'yes' if i % 2 == 0 else 'no'
        candles = []
        for hours_before in [48, 36, 24, 12, 6]:
            period_end = settled_at - timedelta(hours=hours_before)
            candles.append({
                'period_end': period_end,
                'bid_cents': 90,
                'ask_cents': 96,
                'fill_candle': CandleData(
                    yes_bid_high=95,   # YES bids up to 95 touched
                    yes_ask_low=91,    # YES asks down to 91 touched
                    volume=10,
                    price_high=95,
                    price_low=91,
                ),
            })
        ticker_data[f'FILL-{i:03d}'] = {
            'gp': gp, 'topic': topic,
            'settled_at': settled_at, 'result': result,
            'candles': candles,
        }
    return ticker_data


# ── Protocol compliance ──────────────────────────────────────────


class TestProtocolCompliance:
    """All factories satisfy EstimatorFactory protocol."""

    def test_event_rate_factory(self):
        f = EventRateFactory()
        assert isinstance(f, EstimatorFactory)
        assert f.name == 'event_rate'
        assert 'observations' in f.data_requirements
        assert 'classifications' in f.data_requirements
        assert f.depends_on == []

    def test_classification_factory(self):
        f = ClassificationFactory()
        assert isinstance(f, EstimatorFactory)
        assert f.name == 'classification'
        assert f.data_requirements == ['classifications']
        assert f.depends_on == []

    def test_fill_rate_factory(self):
        f = FillRateFactory()
        assert isinstance(f, EstimatorFactory)
        assert f.name == 'fill_model'
        assert f.data_requirements == ['fill_data']
        assert f.depends_on == []


# ── EventRateFactory ─────────────────────────────────────────────


class TestEventRateFactory:
    """EventRateFactory calibration and query function."""

    def test_calibrate_produces_estimator(self):
        obs = make_observations()
        data = {'observations': obs, 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory(price_method='mid')
        estimator = factory.calibrate(data)
        assert hasattr(estimator, 'get_event_rate')
        assert hasattr(estimator, 'rates')
        assert 'mid' in estimator.rates

    def test_calibrate_all_methods(self):
        obs = make_observations()
        data = {'observations': obs, 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory()  # default: all methods
        estimator = factory.calibrate(data)
        for method in ('bid', 'mid', 'ask'):
            assert method in estimator.rates, f"{method} missing from rates"

    def test_calibrate_no_observations(self):
        data = {'observations': [], 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory()
        estimator = factory.calibrate(data)
        assert estimator.rates == {} or all(
            len(v) == 0 for v in estimator.rates.values())

    def test_query_fn_basic(self):
        """query_fn passes series and hours correctly."""
        obs = make_observations()
        data = {'observations': obs, 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory(price_method='mid')
        estimator = factory.calibrate(data)

        result = EventRateFactory.query_fn(
            estimator, series='TEST', hours=15.0,
            observed_price_dollars=0.92)
        assert result is not None
        p_yes, se, n_markets = result
        assert 0 < p_yes < 1
        assert se >= 0
        assert n_markets >= 50

    def test_query_fn_bid_ask(self):
        """query_fn passes bid/ask price kwargs."""
        obs = make_observations()
        data = {'observations': obs, 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory()
        estimator = factory.calibrate(data)

        result = EventRateFactory.query_fn(
            estimator, series='TEST', hours=15.0,
            bid_dollars=0.90, ask_dollars=0.94)
        assert result is not None

    def test_query_fn_unknown_series(self):
        """query_fn returns None for unknown series."""
        obs = make_observations()
        data = {'observations': obs, 'classifications': CLASSIFICATIONS}
        factory = EventRateFactory(price_method='mid')
        estimator = factory.calibrate(data)

        result = EventRateFactory.query_fn(
            estimator, series='UNKNOWN', hours=15.0,
            observed_price_dollars=0.92)
        assert result is None

    def test_calibrate_does_not_receive_as_of(self):
        """Structural enforcement: calibrate() signature has no as_of."""
        import inspect
        sig = inspect.signature(EventRateFactory.calibrate)
        params = list(sig.parameters.keys())
        assert 'as_of' not in params


# ── ClassificationFactory ────────────────────────────────────────


class TestClassificationFactory:

    def test_calibrate_returns_dict(self):
        data = {'classifications': CLASSIFICATIONS}
        factory = ClassificationFactory()
        result = factory.calibrate(data)
        assert isinstance(result, dict)
        assert result == CLASSIFICATIONS

    def test_calibrate_returns_copy(self):
        """Returned dict is independent of input."""
        data = {'classifications': CLASSIFICATIONS}
        factory = ClassificationFactory()
        result = factory.calibrate(data)
        result['NEW'] = ('x', 'y')
        assert 'NEW' not in CLASSIFICATIONS

    def test_query_fn_known_series(self):
        result = ClassificationFactory.query_fn(CLASSIFICATIONS, series='TEST')
        assert result == ('continuous_underlyer', 'financial')

    def test_query_fn_unknown_series(self):
        result = ClassificationFactory.query_fn(CLASSIFICATIONS, series='UNKNOWN')
        assert result is None


# ── FillRateFactory ──────────────────────────────────────────────


class TestFillRateFactory:
    """FillRateFactory calibration and query_fn parameter translation."""

    def test_calibrate_produces_estimator(self):
        """calibrate() produces a working FillRateEstimator."""
        fill_data = make_fill_data()
        data = {'fill_data': fill_data}
        factory = FillRateFactory()
        estimator = factory.calibrate(data)
        assert hasattr(estimator, 'get_fill_rates')
        assert hasattr(estimator, 'rates')
        assert len(estimator.rates) > 0

    def test_calibrate_and_query(self):
        """Calibrated estimator serves fill rates for the synthetic data."""
        fill_data = make_fill_data()
        data = {'fill_data': fill_data}
        factory = FillRateFactory()
        estimator = factory.calibrate(data)

        # Query via query_fn — should return a FillEstimate
        result = FillRateFactory.query_fn(
            estimator, side='yes', limit_price=93, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='continuous_underlyer', topic='financial')
        assert result is not None
        assert isinstance(result, FillEstimate)
        assert 0 <= result.p_fill_won <= 1
        assert 0 <= result.p_fill_lost <= 1

    def test_query_fn_yes_side_relative_price(self):
        """YES side: relative_price = (limit - bid) / spread."""
        mock = MockFillRateEstimator(
            rates={('cu', 'fin', 'yes'): (0.6, 0.4)})

        result = FillRateFactory.query_fn(
            mock, side='yes', limit_price=92, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')

        assert mock.last_call['side'] == 'yes'
        assert mock.last_call['relative_price'] == pytest.approx(
            (92 - 90) / (96 - 90))  # 2/6 ≈ 0.333
        assert isinstance(result, FillEstimate)
        assert result.p_fill_won == 0.6
        assert result.p_fill_lost == 0.4

    def test_query_fn_no_side_inverts_bid_ask(self):
        """NO side: uses 100-ask as bid, 100-bid as ask."""
        mock = MockFillRateEstimator(
            rates={('cu', 'fin', 'no'): (0.5, 0.3)})

        result = FillRateFactory.query_fn(
            mock, side='no', limit_price=8, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')

        # NO side: s_bid = 100 - 96 = 4, s_ask = 100 - 90 = 10
        # relative_price = (8 - 4) / (10 - 4) = 4/6 ≈ 0.667
        assert mock.last_call['side'] == 'no'
        assert mock.last_call['relative_price'] == pytest.approx(4 / 6)
        assert result.p_fill_won == 0.5

    def test_query_fn_zero_spread(self):
        """Zero spread returns None."""
        mock = MockFillRateEstimator()
        result = FillRateFactory.query_fn(
            mock, side='yes', limit_price=90, quantity=1,
            bid=90, ask=90, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')
        assert result is None

    def test_query_fn_no_data(self):
        """Returns None when estimator has no data for the cell."""
        mock = MockFillRateEstimator()  # empty rates
        result = FillRateFactory.query_fn(
            mock, side='yes', limit_price=92, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')
        assert result is None

    def test_query_fn_at_bid(self):
        """limit_price == bid → relative_price = 0."""
        mock = MockFillRateEstimator(
            rates={('cu', 'fin', 'yes'): (0.7, 0.5)})
        FillRateFactory.query_fn(
            mock, side='yes', limit_price=90, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')
        assert mock.last_call['relative_price'] == pytest.approx(0.0)

    def test_query_fn_at_ask(self):
        """limit_price == ask → relative_price = 1."""
        mock = MockFillRateEstimator(
            rates={('cu', 'fin', 'yes'): (0.7, 0.5)})
        FillRateFactory.query_fn(
            mock, side='yes', limit_price=96, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')
        assert mock.last_call['relative_price'] == pytest.approx(1.0)

    def test_query_fn_midpoint(self):
        """limit_price at midpoint → relative_price = 0.5."""
        mock = MockFillRateEstimator(
            rates={('cu', 'fin', 'yes'): (0.7, 0.5)})
        FillRateFactory.query_fn(
            mock, side='yes', limit_price=93, quantity=1,
            bid=90, ask=96, hours_to_settlement=24.0,
            generating_process='cu', topic='fin')
        assert mock.last_call['relative_price'] == pytest.approx(0.5)


class TestFillEstimate:

    def test_attributes(self):
        fe = FillEstimate(p_fill_won=0.6, p_fill_lost=0.3)
        assert fe.p_fill_won == 0.6
        assert fe.p_fill_lost == 0.3

    def test_frozen(self):
        fe = FillEstimate(p_fill_won=0.6, p_fill_lost=0.3)
        with pytest.raises(AttributeError):
            fe.p_fill_won = 0.9

    def test_equality(self):
        a = FillEstimate(0.6, 0.3)
        b = FillEstimate(0.6, 0.3)
        assert a == b


# ── ViewFactory integration ──────────────────────────────────────


class TestViewFactoryIntegration:
    """ViewFactory.build() correctly wires factories with query_fn."""

    @pytest.fixture
    def obs_and_registry(self):
        """Pre-built observations and registry for integration tests."""
        obs = make_observations()
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', obs))
        registry.register(ConstantFeature('classifications', CLASSIFICATIONS))
        return obs, as_of, registry

    def test_build_with_event_rate(self, obs_and_registry):
        obs, as_of, registry = obs_and_registry
        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
        )
        view = vf.build(as_of)
        result = view.event_rate('TEST', 15.0,
                                 observed_price_dollars=0.92)
        assert result is not None
        p_yes, se, n_markets = result
        assert 0 < p_yes < 1
        assert n_markets >= 50

    def test_build_with_classification(self, obs_and_registry):
        _, as_of, registry = obs_and_registry
        vf = ViewFactory(
            registry=registry,
            factories=[ClassificationFactory()],
        )
        view = vf.build(as_of)
        assert view.classification('TEST') == (
            'continuous_underlyer', 'financial')
        assert view.classification('UNKNOWN') is None

    def test_build_with_both(self, obs_and_registry):
        """View with both event_rate and classification works."""
        obs, as_of, registry = obs_and_registry
        vf = ViewFactory(
            registry=registry,
            factories=[
                EventRateFactory(price_method='mid'),
                ClassificationFactory(),
            ],
        )
        view = vf.build(as_of)

        er = view.event_rate('TEST', 15.0, observed_price_dollars=0.92)
        assert er is not None

        cl = view.classification('TEST')
        assert cl == ('continuous_underlyer', 'financial')

    def test_query_fn_passed_through_on_load(self, obs_and_registry, tmp_path):
        """query_fn works even when estimator is loaded from store."""
        from framework.calibration_store import CalibrationStore
        obs, as_of, registry = obs_and_registry
        store = CalibrationStore(tmp_path)

        # First build: calibrates and stores
        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
            store=store,
        )
        view1 = vf.build(as_of)
        r1 = view1.event_rate('TEST', 15.0, observed_price_dollars=0.92)

        # Second build: loads from store
        registry2 = FeatureRegistry()
        registry2.register(FilteredListFeature('observations', obs))
        registry2.register(ConstantFeature('classifications', CLASSIFICATIONS))
        vf2 = ViewFactory(
            registry=registry2,
            factories=[EventRateFactory(price_method='mid')],
            store=store,
        )
        view2 = vf2.build(as_of)
        r2 = view2.event_rate('TEST', 15.0, observed_price_dollars=0.92)

        assert r1 is not None and r2 is not None
        assert r1[0] == pytest.approx(r2[0])
        assert r1[2] == r2[2]

    def test_expanding_window_independence(self, obs_and_registry):
        """Different as_of → independent views with potentially different data."""
        obs, _, registry = obs_and_registry
        early = datetime(2026, 2, 1, tzinfo=timezone.utc)
        late = datetime(2026, 4, 1, tzinfo=timezone.utc)

        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
        )

        # Early: all observations have settled_at=2026-03-01, which is
        # AFTER early, so FilteredListFeature returns empty list
        view_early = vf.build(early)
        assert view_early.event_rate('TEST', 15.0,
                                     observed_price_dollars=0.92) is None

        # Late: settled_at < late, so all observations included
        view_late = vf.build(late)
        assert view_late.event_rate('TEST', 15.0,
                                    observed_price_dollars=0.92) is not None

    def test_build_with_fill_model(self):
        """End-to-end: FillRateFactory → ViewFactory.build() → View.fill_probability()."""
        fill_data = make_fill_data()
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)

        registry = FeatureRegistry()
        registry.register(ConstantFeature('fill_data', fill_data))

        vf = ViewFactory(
            registry=registry,
            factories=[FillRateFactory()],
        )
        view = vf.build(as_of)

        result = view.fill_probability(
            side='yes', limit_price=93, quantity=1,
            market_state={
                'bid': 90, 'ask': 96,
                'hours_to_settlement': 24.0,
                'generating_process': 'continuous_underlyer',
                'topic': 'financial',
                'trailing_volume': 0,
                'open_interest': 0,
            })
        assert result is not None
        assert isinstance(result, FillEstimate)
        assert 0 <= result.p_fill_won <= 1
        assert 0 <= result.p_fill_lost <= 1

    def test_fill_model_loaded_from_store(self, tmp_path):
        """fill_probability works when FillRateEstimator is loaded from store."""
        from framework.calibration_store import CalibrationStore

        fill_data = make_fill_data()
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
        store = CalibrationStore(tmp_path)

        # First build: calibrates and stores
        registry = FeatureRegistry()
        registry.register(ConstantFeature('fill_data', fill_data))
        vf = ViewFactory(registry=registry,
                         factories=[FillRateFactory()], store=store)
        view1 = vf.build(as_of)
        r1 = view1.fill_probability(
            side='yes', limit_price=93, quantity=1,
            market_state={
                'bid': 90, 'ask': 96,
                'hours_to_settlement': 24.0,
                'generating_process': 'continuous_underlyer',
                'topic': 'financial',
            })

        # Second build: loads from store, query_fn still works
        registry2 = FeatureRegistry()
        registry2.register(ConstantFeature('fill_data', fill_data))
        vf2 = ViewFactory(registry=registry2,
                          factories=[FillRateFactory()], store=store)
        view2 = vf2.build(as_of)
        r2 = view2.fill_probability(
            side='yes', limit_price=93, quantity=1,
            market_state={
                'bid': 90, 'ask': 96,
                'hours_to_settlement': 24.0,
                'generating_process': 'continuous_underlyer',
                'topic': 'financial',
            })

        assert r1 is not None and r2 is not None
        assert r1.p_fill_won == pytest.approx(r2.p_fill_won)
        assert r1.p_fill_lost == pytest.approx(r2.p_fill_lost)

    def test_factory_without_query_fn(self):
        """Factories without query_fn still work (backward compat)."""

        class SimpleFactory:
            @property
            def name(self):
                return 'simple'

            @property
            def data_requirements(self):
                return ['data']

            @property
            def depends_on(self):
                return []

            def calibrate(self, data, dependencies=None):
                class SimpleEst:
                    def query(self, **params):
                        return params.get('x', 42)
                return SimpleEst()

        registry = FeatureRegistry()
        registry.register(ConstantFeature('data', [1, 2, 3]))
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)

        vf = ViewFactory(registry=registry, factories=[SimpleFactory()])
        view = vf.build(as_of)
        assert view.query('simple', x=10) == 10


# ── Equivalence with MarketView ──────────────────────────────────


class TestMarketViewEquivalence:
    """Framework View produces same results as hand-constructed MarketView.

    This is the key test from spec Chunk 6: "framework-constructed View
    produces same outputs as hand-constructed MarketView."

    Only tests event_rate equivalence. Fill model interfaces differ
    between MarketView (fill_rates/fill_estimate) and View (fill_probability),
    so direct method-level equivalence isn't applicable.
    """

    @pytest.fixture
    def synthetic_data(self):
        obs = make_observations(n_tickers=60, price=0.92)
        classifications = dict(CLASSIFICATIONS)
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
        return obs, classifications, as_of

    def test_event_rate_same_result(self, synthetic_data):
        """Same P(YES) from MarketView and framework View."""
        from market_view import MarketView

        obs, classifications, as_of = synthetic_data

        # MarketView path (hand-constructed)
        mv = MarketView(as_of=as_of, all_observations=obs,
                        classifications=classifications)

        # Framework path
        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', obs))
        registry.register(ConstantFeature('classifications', classifications))
        vf = ViewFactory(
            registry=registry,
            factories=[
                EventRateFactory(price_method='mid'),
                ClassificationFactory(),
            ],
        )
        view = vf.build(as_of)

        # Compare event_rate for the test series
        mv_result = mv.event_rate('TEST', 15.0, observed_price_dollars=0.92)
        fw_result = view.event_rate('TEST', 15.0,
                                    observed_price_dollars=0.92)

        assert mv_result is not None, "MarketView returned None"
        assert fw_result is not None, "Framework View returned None"

        # P(YES), SE, n_markets must match
        assert mv_result[0] == pytest.approx(fw_result[0]), \
            f"P(YES) mismatch: MV={mv_result[0]}, FW={fw_result[0]}"
        assert mv_result[1] == pytest.approx(fw_result[1]), \
            f"SE mismatch: MV={mv_result[1]}, FW={fw_result[1]}"
        assert mv_result[2] == fw_result[2], \
            f"n_markets mismatch: MV={mv_result[2]}, FW={fw_result[2]}"

    def test_classification_same_result(self, synthetic_data):
        """Same classification from MarketView and framework View."""
        from market_view import MarketView

        obs, classifications, as_of = synthetic_data

        mv = MarketView(as_of=as_of, all_observations=obs,
                        classifications=classifications)

        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', obs))
        registry.register(ConstantFeature('classifications', classifications))
        vf = ViewFactory(
            registry=registry,
            factories=[
                EventRateFactory(price_method='mid'),
                ClassificationFactory(),
            ],
        )
        view = vf.build(as_of)

        assert mv.classification('TEST') == view.classification('TEST')
        assert mv.classification('UNKNOWN') == view.classification('UNKNOWN')

    def test_unknown_series_both_none(self, synthetic_data):
        """Both return None for unknown series."""
        from market_view import MarketView

        obs, classifications, as_of = synthetic_data

        mv = MarketView(as_of=as_of, all_observations=obs,
                        classifications=classifications)

        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', obs))
        registry.register(ConstantFeature('classifications', classifications))
        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
        )
        view = vf.build(as_of)

        assert mv.event_rate('UNKNOWN', 15.0,
                             observed_price_dollars=0.92) is None
        assert view.event_rate('UNKNOWN', 15.0,
                               observed_price_dollars=0.92) is None

    def test_multiple_query_points(self, synthetic_data):
        """Equivalence holds across different hours and prices."""
        from market_view import MarketView

        obs, classifications, as_of = synthetic_data

        mv = MarketView(as_of=as_of, all_observations=obs,
                        classifications=classifications)

        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', obs))
        registry.register(ConstantFeature('classifications', classifications))
        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
        )
        view = vf.build(as_of)

        test_points = [
            (10.0, 0.92), (20.0, 0.92), (30.0, 0.92),
            (15.0, 0.91), (15.0, 0.93),
        ]
        for hours, price in test_points:
            mv_r = mv.event_rate('TEST', hours,
                                 observed_price_dollars=price)
            fw_r = view.event_rate('TEST', hours,
                                   observed_price_dollars=price)
            if mv_r is None:
                assert fw_r is None, \
                    f"hours={hours}, price={price}: MV=None, FW={fw_r}"
            else:
                assert fw_r is not None, \
                    f"hours={hours}, price={price}: MV={mv_r}, FW=None"
                assert mv_r[0] == pytest.approx(fw_r[0]), \
                    f"hours={hours}, price={price}: P(YES) mismatch"

    def test_temporal_filtering_equivalence(self, synthetic_data):
        """Both filter the same observations for a given as_of."""
        from market_view import MarketView

        # Create observations with two different settled_at dates
        early_obs = make_observations(
            n_tickers=60, price=0.92,
            settled_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        late_obs = make_observations(
            n_tickers=60, price=0.92,
            settled_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        # Different tickers to avoid dedup effects
        for i, o in enumerate(late_obs):
            o.ticker = f'TEST-LATE-{i // 3:03d}'
        all_obs = early_obs + late_obs

        # as_of between the two groups → only early_obs should be used
        as_of = datetime(2026, 3, 15, tzinfo=timezone.utc)
        classifications = dict(CLASSIFICATIONS)

        mv = MarketView(as_of=as_of, all_observations=all_obs,
                        classifications=classifications)

        registry = FeatureRegistry()
        registry.register(FilteredListFeature('observations', all_obs))
        registry.register(ConstantFeature('classifications', classifications))
        vf = ViewFactory(
            registry=registry,
            factories=[EventRateFactory(price_method='mid')],
        )
        view = vf.build(as_of)

        mv_r = mv.event_rate('TEST', 15.0, observed_price_dollars=0.92)
        fw_r = view.event_rate('TEST', 15.0,
                               observed_price_dollars=0.92)

        assert mv_r is not None and fw_r is not None
        assert mv_r[0] == pytest.approx(fw_r[0])
        assert mv_r[2] == fw_r[2]
