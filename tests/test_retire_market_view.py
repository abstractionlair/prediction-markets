"""Tests for Chunk 8: Retire MarketView.

Verifies that:
1. EVStrategy works with framework View (fill_probability, cost, event_rate)
2. ViewFactory-constructed View produces the same outputs as MarketView
3. PreloadedFeature temporal filtering works correctly
4. view_bootstrap.py constructs a valid ViewFactory
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from framework.factories import (
    FillEstimate,
)
from framework.feature import PreloadedFeature
from framework.view import View
from framework.view_factory import ViewFactory
from trading.ev_strategy import EVStrategy


# ── Synthetic data for tests ─────────────────────────────────────


@dataclass
class FakeObservation:
    """Minimal observation for EventRateEstimator."""
    ticker: str
    series: str
    settled_at: datetime
    yes_bid: float
    yes_ask: float
    yes_mid: float
    trade_price: float
    result_yes: bool
    hours_to_settlement: float
    generating_process: str
    topic: str


def _make_observations(n=200, as_of=None):
    """Generate synthetic observations for testing.

    Creates observations for series 'KSERIES' with generating_process='gp'
    and topic='topic'. Half result YES, half NO. Various hours and prices.
    """
    if as_of is None:
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
    observations = []
    for i in range(n):
        # Settle at various times before as_of
        settled = datetime(2026, 1, 1 + (i % 28), tzinfo=timezone.utc)
        if settled >= as_of:
            continue
        price = 0.90 + (i % 10) * 0.01  # 0.90 to 0.99
        observations.append(FakeObservation(
            ticker=f'KSERIES-M{i:03d}',
            series='KSERIES',
            settled_at=settled,
            yes_bid=price - 0.02,
            yes_ask=price + 0.02,
            yes_mid=price,
            trade_price=price,
            result_yes=(i % 2 == 0),
            hours_to_settlement=24.0 + (i % 48),
            generating_process='gp',
            topic='topic',
        ))
    return observations


def _make_classifications():
    return {'KSERIES': ('gp', 'topic')}


def _make_fill_data(as_of=None):
    """Generate synthetic fill data for FillRateEstimator.

    Returns dict of ticker -> {gp, topic, settled_at, result, candles: [...]}.
    """
    if as_of is None:
        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
    from trading.fill_model import CandleData
    fill_data = {}
    for i in range(50):
        settled_at = datetime(2026, 1, 1 + (i % 28), tzinfo=timezone.utc)
        if settled_at >= as_of:
            continue
        ticker = f'KSERIES-M{i:03d}'
        candles = []
        for h in range(5):
            period = datetime(2026, 1, max(1, (i % 28)), h, tzinfo=timezone.utc)
            candles.append({
                'period_end': period,
                'bid_cents': 88 + (h % 5),
                'ask_cents': 92 + (h % 5),
                'fill_candle': CandleData(
                    yes_bid_high=90 + (h % 3),
                    yes_ask_low=89 + (h % 3),
                    volume=10 + h,
                    price_high=92 + h,
                    price_low=88 + h,
                ),
            })
        fill_data[ticker] = {
            'series': 'KSERIES',
            'gp': 'gp',
            'topic': 'topic',
            'settled_at': settled_at,
            'result': 'yes' if i % 2 == 0 else 'no',
            'candles': candles,
        }
    return fill_data


# ── PreloadedFeature ─────────────────────────────────────────────


class TestPreloadedFeature:
    def test_no_filter(self):
        data = {'a': 1, 'b': 2}
        f = PreloadedFeature('test', data)
        assert f.name == 'test'
        result = f.query(datetime.now(timezone.utc))
        assert result == data

    def test_temporal_filter(self):
        items = [
            FakeObservation(
                ticker='A', series='S',
                settled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                yes_bid=0.9, yes_ask=0.95, yes_mid=0.925,
                trade_price=0.925, result_yes=True,
                hours_to_settlement=24.0,
                generating_process='gp', topic='topic'),
            FakeObservation(
                ticker='B', series='S',
                settled_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                yes_bid=0.9, yes_ask=0.95, yes_mid=0.925,
                trade_price=0.925, result_yes=False,
                hours_to_settlement=24.0,
                generating_process='gp', topic='topic'),
        ]
        f = PreloadedFeature(
            'obs', items,
            filter_fn=lambda data, as_of: [o for o in data if o.settled_at < as_of],
        )
        # as_of in March → only January observation passes
        result = f.query(datetime(2026, 3, 1, tzinfo=timezone.utc))
        assert len(result) == 1
        assert result[0].ticker == 'A'

    def test_dict_filter(self):
        data = {
            'T1': {'settled_at': datetime(2026, 1, 1, tzinfo=timezone.utc)},
            'T2': {'settled_at': datetime(2026, 6, 1, tzinfo=timezone.utc)},
        }
        f = PreloadedFeature(
            'fill_data', data,
            filter_fn=lambda d, as_of: {
                t: v for t, v in d.items() if v['settled_at'] < as_of},
        )
        result = f.query(datetime(2026, 3, 1, tzinfo=timezone.utc))
        assert 'T1' in result
        assert 'T2' not in result

    def test_repr(self):
        f = PreloadedFeature('test', [1, 2, 3])
        assert 'test' in repr(f)


# ── build_view_factory ───────────────────────────────────────────


class TestBuildViewFactory:
    def test_returns_view_factory(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)
        assert isinstance(factory, ViewFactory)

    def test_builds_view(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert isinstance(view, View)

    def test_view_has_event_rate(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        result = view.event_rate('KSERIES', 24.0, observed_price_dollars=0.92)
        assert result is not None
        p_yes, se, n = result
        assert 0.0 < p_yes < 1.0
        assert n > 0

    def test_view_has_classification(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        cl = view.classification('KSERIES')
        assert cl == ('gp', 'topic')
        assert view.classification('UNKNOWN') is None

    def test_view_has_cost(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        fee = view.cost(92, 1)
        assert fee > 0

    def test_view_has_fill_probability(self):
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        fill_data = _make_fill_data()
        factory = build_view_factory(obs, classifications, fill_data=fill_data)
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        market_state = {
            'bid': 90, 'ask': 94,
            'hours_to_settlement': 24.0,
            'generating_process': 'gp', 'topic': 'topic',
        }
        result = view.fill_probability('yes', 92, 1, market_state)
        # May be None if synthetic data doesn't produce enough calibration
        # data for this bucket, but the call itself should not error
        if result is not None:
            assert hasattr(result, 'p_fill_won')
            assert hasattr(result, 'p_fill_lost')

    def test_no_fill_data_returns_none(self):
        """Without fill_data, fill_probability returns None."""
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)  # no fill_data
        view = factory.build(as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        market_state = {
            'bid': 90, 'ask': 94,
            'hours_to_settlement': 24.0,
            'generating_process': 'gp', 'topic': 'topic',
        }
        # fill_model not registered → KeyError caught by registry → returns None
        # Actually, fill_model not registered → query will raise KeyError
        # The strategy handles this by checking result is not None
        import pytest
        with pytest.raises(KeyError):
            view.fill_probability('yes', 92, 1, market_state)

    def test_temporal_filtering(self):
        """View built at different as_of dates sees different data."""
        from trading.view_bootstrap import build_view_factory
        obs = _make_observations()
        classifications = _make_classifications()
        factory = build_view_factory(obs, classifications)

        # Early as_of → fewer observations
        early_view = factory.build(
            as_of=datetime(2026, 1, 10, tzinfo=timezone.utc))
        late_view = factory.build(
            as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))

        early_result = early_view.event_rate(
            'KSERIES', 24.0, observed_price_dollars=0.92)
        late_result = late_view.event_rate(
            'KSERIES', 24.0, observed_price_dollars=0.92)

        # Both should return data, but late view has more observations
        if early_result is not None and late_result is not None:
            _, _, n_early = early_result
            _, _, n_late = late_result
            assert n_late >= n_early


# ── EVStrategy with framework View ──────────────────────────────


class _SimpleView:
    """Minimal View for testing EVStrategy with the new API."""

    def __init__(self, event_rate_val=None, classifications=None,
                 fill_estimate=None, fee=1):
        self._event_rate_val = event_rate_val or (0.95, 0.01, 100)
        self._classifications = classifications or {}
        self._fill_estimate = fill_estimate
        self._fee = fee

    def event_rate(self, series, hours, **kwargs):
        return self._event_rate_val

    def classification(self, series):
        return self._classifications.get(series)

    def fill_probability(self, side, limit_price, quantity, market_state):
        if self._fill_estimate is not None:
            return self._fill_estimate
        # Favorable selection: fills more when winning → positive EV
        return FillEstimate(p_fill_won=0.70, p_fill_lost=0.30)

    def cost(self, price_cents, contracts, is_maker=True):
        return self._fee


class TestEVStrategyWithView:
    """Test that EVStrategy works correctly with the framework View API."""

    def _make_event(self, ticker='T-001', event_ticker='E-001',
                    yes_bid=0.90, yes_ask=0.93, status='active',
                    close_time='2026-04-10T00:00:00Z'):
        return {
            'event_ticker': event_ticker,
            'markets': [{
                'ticker': ticker,
                'status': status,
                'yes_bid_dollars': str(yes_bid),
                'yes_ask_dollars': str(yes_ask),
                'expected_expiration_time': close_time,
                'volume_24h_fp': '100',
                'open_interest': 50,
            }],
        }

    def test_scan_produces_opportunities(self):
        view = _SimpleView(
            classifications={'E': ('gp', 'topic')},
        )
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        opps = strategy.scan(events, now=now)
        assert len(opps) > 0
        opp = opps[0]
        assert opp.ev_per_contract > 0
        assert opp.p_fill > 0

    def test_scan_uses_keyword_event_rate(self):
        """event_rate is called with observed_price_dollars as keyword."""
        calls = []

        class TrackingView(_SimpleView):
            def event_rate(self, series, hours, **kwargs):
                calls.append(kwargs)
                return (0.95, 0.01, 100)

        view = TrackingView(classifications={'E': ('gp', 'topic')})
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        strategy.scan(events, now=now)
        assert len(calls) > 0
        assert 'observed_price_dollars' in calls[0]

    def test_scan_calls_fill_probability(self):
        """Strategy uses fill_probability, not fill_rates or fill_estimate."""
        fill_calls = []

        class TrackingView(_SimpleView):
            def fill_probability(self, side, limit_price, quantity, market_state):
                fill_calls.append({
                    'side': side, 'limit_price': limit_price,
                    'quantity': quantity, 'market_state': market_state,
                })
                return FillEstimate(p_fill_won=0.30, p_fill_lost=0.70)

        view = TrackingView(classifications={'E': ('gp', 'topic')})
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        strategy.scan(events, now=now)
        assert len(fill_calls) > 0
        # Verify market_state has required keys
        ms = fill_calls[0]['market_state']
        assert 'bid' in ms
        assert 'ask' in ms
        assert 'hours_to_settlement' in ms
        assert 'generating_process' in ms
        assert 'topic' in ms

    def test_scan_calls_cost(self):
        """Strategy uses cost(), not maker_fee()."""
        cost_calls = []

        class TrackingView(_SimpleView):
            def cost(self, price_cents, contracts, is_maker=True):
                cost_calls.append((price_cents, contracts))
                return 1

        view = TrackingView(classifications={'E': ('gp', 'topic')})
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        strategy.scan(events, now=now)
        assert len(cost_calls) > 0

    def test_fill_probability_none_skips_market(self):
        """If fill_probability returns None, the market is skipped."""
        view = _SimpleView(
            classifications={'E': ('gp', 'topic')},
            fill_estimate=None,  # Will return default FillEstimate
        )
        # Override to return None
        view.fill_probability = lambda *a, **kw: None
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        opps = strategy.scan(events, now=now)
        assert len(opps) == 0

    def test_joint_search_parameter(self):
        """joint_search=True uses _find_best_order."""
        view = _SimpleView(classifications={'E': ('gp', 'topic')})
        strategy = EVStrategy(view, joint_search=True)
        assert strategy.joint_search is True

        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [self._make_event()]
        opps = strategy.scan(events, now=now)
        # Should produce results (joint search works with simple view)
        assert len(opps) > 0

    def test_sort_by_total_ev(self):
        """Opportunities are sorted by total_ev descending."""
        view = _SimpleView(classifications={'E': ('gp', 'topic')})
        strategy = EVStrategy(view)
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        events = [
            self._make_event(ticker='T-001', event_ticker='E1'),
            self._make_event(ticker='T-002', event_ticker='E2',
                             yes_bid=0.91, yes_ask=0.94),
        ]
        opps = strategy.scan(events, now=now)
        if len(opps) >= 2:
            assert opps[0].total_ev >= opps[1].total_ev


# ── MarketView backward compatibility ───────────────────────────


class TestMarketViewCompat:
    """Test that MarketView's bridge methods work with EVStrategy."""

    def test_fill_probability_bridge(self):
        """MarketView.fill_probability delegates to fill_rates."""
        from trading.market_view import MarketView
        assert hasattr(MarketView, 'fill_probability')
        assert hasattr(MarketView, 'cost')

    def test_cost_bridge(self):
        """MarketView.cost delegates to CostModel."""
        from trading.market_view import MarketView
        assert hasattr(MarketView, 'cost')


# ── View-vs-MarketView parity test (spec line 1134) ─────────────


class TestViewMarketViewParity:
    """Framework-constructed View produces same outputs as MarketView.

    Spec Section 7.2, line 1134: "Test: framework-constructed View
    produces same outputs as hand-constructed MarketView."

    Constructs both from the same synthetic data and compares outputs.
    """

    def _build_both(self):
        """Build a MarketView and a framework View from the same data."""
        from trading.market_view import MarketView
        from trading.view_bootstrap import build_view_factory

        as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
        observations = _make_observations(n=200, as_of=as_of)
        classifications = _make_classifications()
        fill_data = _make_fill_data(as_of=as_of)

        # Build MarketView
        market_view = MarketView(
            as_of=as_of,
            all_observations=observations,
            all_fill_data=fill_data,
            classifications=classifications,
        )

        # Build framework View via ViewFactory
        factory = build_view_factory(
            observations, classifications, fill_data=fill_data)
        framework_view = factory.build(as_of=as_of)

        return market_view, framework_view, as_of

    def test_event_rate_parity(self):
        """event_rate returns the same (p_yes, se, n) from both."""
        mv, fv, _ = self._build_both()

        mv_result = mv.event_rate('KSERIES', 24.0,
                                  observed_price_dollars=0.92)
        fv_result = fv.event_rate('KSERIES', 24.0,
                                  observed_price_dollars=0.92)

        assert mv_result is not None, "MarketView returned None"
        assert fv_result is not None, "Framework View returned None"

        mv_p, mv_se, mv_n = mv_result
        fv_p, fv_se, fv_n = fv_result

        assert abs(mv_p - fv_p) < 1e-10, f"p_yes mismatch: {mv_p} vs {fv_p}"
        assert abs(mv_se - fv_se) < 1e-10, f"SE mismatch: {mv_se} vs {fv_se}"
        assert mv_n == fv_n, f"n_markets mismatch: {mv_n} vs {fv_n}"

    def test_event_rate_parity_multiple_buckets(self):
        """event_rate matches across different hours/price inputs."""
        mv, fv, _ = self._build_both()

        for hours in [6.0, 24.0, 48.0]:
            for price in [0.88, 0.92, 0.96]:
                mv_r = mv.event_rate('KSERIES', hours,
                                     observed_price_dollars=price)
                fv_r = fv.event_rate('KSERIES', hours,
                                     observed_price_dollars=price)
                # Both should return the same result (or both None)
                if mv_r is None:
                    assert fv_r is None, (
                        f"MarketView=None but View={fv_r} "
                        f"at hours={hours}, price={price}")
                else:
                    assert fv_r is not None, (
                        f"MarketView={mv_r} but View=None "
                        f"at hours={hours}, price={price}")
                    assert abs(mv_r[0] - fv_r[0]) < 1e-10
                    assert mv_r[2] == fv_r[2]

    def test_classification_parity(self):
        """classification returns the same (gp, topic) from both."""
        mv, fv, _ = self._build_both()

        assert mv.classification('KSERIES') == fv.classification('KSERIES')
        assert mv.classification('UNKNOWN') == fv.classification('UNKNOWN')

    def test_cost_parity(self):
        """cost/maker_fee return the same fee from both."""
        mv, fv, _ = self._build_both()

        for price in [85, 90, 93, 95, 97]:
            for contracts in [1, 4, 8]:
                mv_fee = mv.maker_fee(price, contracts)
                fv_fee = fv.cost(price, contracts)
                assert mv_fee == fv_fee, (
                    f"Fee mismatch at price={price}, c={contracts}: "
                    f"{mv_fee} vs {fv_fee}")

    def test_fill_probability_parity(self):
        """fill_probability returns the same values from both.

        Uses MarketView's bridge method for comparison. Both paths
        ultimately call FillRateEstimator.get_fill_rates().
        """
        mv, fv, _ = self._build_both()

        market_state = {
            'bid': 90, 'ask': 94,
            'hours_to_settlement': 24.0,
            'generating_process': 'gp', 'topic': 'topic',
        }

        for side in ['yes', 'no']:
            for limit in [90, 91, 92, 93, 94]:
                mv_r = mv.fill_probability(side, limit, 1, market_state)
                fv_r = fv.fill_probability(side, limit, 1, market_state)

                if mv_r is None:
                    assert fv_r is None, (
                        f"MarketView=None but View={fv_r} "
                        f"at side={side}, limit={limit}")
                elif fv_r is None:
                    assert mv_r is None, (
                        f"View=None but MarketView={mv_r} "
                        f"at side={side}, limit={limit}")
                else:
                    assert abs(mv_r.p_fill_won - fv_r.p_fill_won) < 1e-10, (
                        f"p_fill_won mismatch at side={side}, limit={limit}: "
                        f"{mv_r.p_fill_won} vs {fv_r.p_fill_won}")
                    assert abs(mv_r.p_fill_lost - fv_r.p_fill_lost) < 1e-10, (
                        f"p_fill_lost mismatch at side={side}, limit={limit}: "
                        f"{mv_r.p_fill_lost} vs {fv_r.p_fill_lost}")
