"""Tests for framework/runner.py — generic strategy replay.

Tests cover:
- Recalibration boundary calculation
- Settlement mechanics (won/lost, partial fills, unfilled)
- Independent cost verification (Runner uses View.cost(), not strategy claims)
- Fill simulator integration (instant, delayed, never)
- Capital tracking and risk limits
- Active ticker deduplication
- End-of-replay settlement
- Edge cases (empty periods, no events)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from framework.runner import (
    Runner,
    InstantFillSimulator,
    _recalibration_boundary,
    _did_win,
)
from trading.track_record import TradeRecord, TrackRecord


# ── Test helpers ─────────────────────────────────────────────

T0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_periods(n, start=T0, interval_hours=1):
    """Generate n hourly evaluation timestamps."""
    return [start + timedelta(hours=i) for i in range(n)]


def make_events(period):
    """Minimal non-empty events list so scan() gets called."""
    return [{'event_ticker': 'EVT-1', 'markets': [{'ticker': 'T1'}]}]


# ── Mock objects ─────────────────────────────────────────────


class SimpleView:
    """View with configurable cost function for testing."""

    def __init__(self, cost_fn=None):
        self._cost_fn = cost_fn or (lambda p, c: 1)

    def cost(self, price_cents, contracts, is_maker=True):
        return self._cost_fn(price_cents, contracts)


class TrackingViewFactory:
    """ViewFactory that records build() calls and returns SimpleViews."""

    def __init__(self, cost_fn=None):
        self.build_calls: list[datetime] = []
        self._cost_fn = cost_fn

    def build(self, as_of, force_recalibrate=None):
        self.build_calls.append(as_of)
        return SimpleView(self._cost_fn)


@dataclass
class MockOpportunity:
    """Minimal opportunity matching EVOpportunity's attribute interface."""
    ticker: str
    event_ticker: str = 'EVT-1'
    side: str = 'yes'
    limit_price: int = 92
    contracts: int = 5
    ev_per_contract: float = 3.0
    p_event: float = 0.95
    p_fill: float = 0.5
    generating_process: str = 'continuous'
    topic: str = 'financial'
    days_to_settle: float = 2.0


class MockStrategy:
    """Strategy returning preconfigured opportunities.

    Created by Runner as: MockStrategy(view, opportunities=[...])
    """
    instances: list = []

    def __init__(self, view, opportunities=None):
        self.view = view
        self._opportunities = opportunities or []
        MockStrategy.instances.append(self)

    def scan(self, events, traded_tickers=None, now=None):
        traded = traded_tickers or set()
        return [o for o in self._opportunities
                if o.ticker not in traded]


class SimpleMarketSource:
    """In-memory market source for testing."""

    def __init__(self, periods_list, events_by_period=None, settlements=None):
        self._periods = sorted(periods_list)
        self._events = events_by_period or {}
        self._settlements = settlements or {}

    def periods(self):
        return list(self._periods)

    def events_at(self, period):
        return self._events.get(period, [])

    def settlement(self, ticker):
        return self._settlements.get(ticker)


class DelayedFillSimulator:
    """Fills orders after a specified number of check_fills calls."""

    def __init__(self, delay_periods=2):
        self._delay = delay_periods
        self._counts: dict[str, int] = {}

    def on_order(self, ticker, order, period):
        self._counts[ticker] = 0

    def check_fills(self, ticker, order, period):
        if ticker not in self._counts:
            return 0
        self._counts[ticker] += 1
        if self._counts[ticker] >= self._delay:
            return order['contracts'] - order.get('contracts_filled', 0)
        return 0


class NeverFillSimulator:
    """Orders never fill."""

    def on_order(self, ticker, order, period):
        pass

    def check_fills(self, ticker, order, period):
        return 0


class MockRiskLimits:
    """Risk limits that block after a threshold."""

    def __init__(self, max_positions=50, block_deployment=False):
        self._max_positions = max_positions
        self._block_deployment = block_deployment

    def check_position_count(self, count):
        return (count < self._max_positions, "")

    def check_deployment(self, equity, escrow, cost):
        return (not self._block_deployment, "")

    def check_event_concentration(self, equity, event_exp, cost):
        return (True, "")


# ── Unit tests: helpers ──────────────────────────────────────


class TestRecalibrationBoundary:

    def test_daily_truncates_to_midnight(self):
        t = datetime(2025, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
        b = _recalibration_boundary(t, 'daily')
        assert b == datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)

    def test_hourly_truncates_to_top_of_hour(self):
        t = datetime(2025, 6, 15, 14, 30, 45, tzinfo=timezone.utc)
        b = _recalibration_boundary(t, 'hourly')
        assert b == datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

    def test_weekly_truncates_to_monday(self):
        # 2025-06-15 is a Sunday (weekday=6)
        t = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        b = _recalibration_boundary(t, 'weekly')
        assert b == datetime(2025, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
        assert b.weekday() == 0  # Monday

    def test_weekly_monday_stays_same_week(self):
        # 2025-06-09 is a Monday
        t = datetime(2025, 6, 9, 8, 0, 0, tzinfo=timezone.utc)
        b = _recalibration_boundary(t, 'weekly')
        assert b == datetime(2025, 6, 9, 0, 0, 0, tzinfo=timezone.utc)

    def test_invalid_schedule_raises(self):
        t = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="Unknown"):
            _recalibration_boundary(t, 'monthly')


class TestDidWin:

    def test_yes_side_yes_result(self):
        assert _did_win('yes', 'yes') is True

    def test_yes_side_no_result(self):
        assert _did_win('yes', 'no') is False

    def test_no_side_no_result(self):
        assert _did_win('no', 'no') is True

    def test_no_side_yes_result(self):
        assert _did_win('no', 'yes') is False


class TestInstantFillSimulator:

    def test_fills_all_remaining(self):
        sim = InstantFillSimulator()
        order = {'contracts': 5, 'contracts_filled': 0}
        assert sim.check_fills('T1', order, T0) == 5

    def test_fills_partial_remaining(self):
        sim = InstantFillSimulator()
        order = {'contracts': 5, 'contracts_filled': 3}
        assert sim.check_fills('T1', order, T0) == 2

    def test_on_order_noop(self):
        sim = InstantFillSimulator()
        sim.on_order('T1', {}, T0)  # should not raise


# ── Integration tests: single trade lifecycle ────────────────


class TestSingleTradeLifecycle:

    def setup_method(self):
        MockStrategy.instances.clear()

    def _run_single_trade(self, side, result, limit_price=92, contracts=5,
                          cost_fn=None):
        """Helper: run replay with one opportunity and one settlement."""
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': result, 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'TSERIES',
            }},
        )

        vf = TrackingViewFactory(cost_fn=cost_fn or (lambda p, c: 2))
        opp = MockOpportunity(
            ticker='T1', side=side,
            limit_price=limit_price, contracts=contracts)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        return runner.replay(source, starting_capital_cents=10000)

    def test_winning_yes_trade(self):
        track = self._run_single_trade('yes', 'yes')
        assert len(track) == 1
        t = track.trades[0]
        assert t.ticker == 'T1'
        assert t.side == 'yes'
        assert t.entry_price == 92
        assert t.contracts == 5
        assert t.exit_price == 100
        assert t.fee_cents == 2
        assert t.pnl_cents == (100 - 92) * 5 - 2  # 38

    def test_losing_yes_trade(self):
        track = self._run_single_trade('yes', 'no')
        t = track.trades[0]
        assert t.exit_price == 0
        assert t.pnl_cents == (0 - 92) * 5 - 2  # -462

    def test_winning_no_trade(self):
        track = self._run_single_trade('no', 'no', limit_price=8, contracts=10)
        t = track.trades[0]
        assert t.side == 'no'
        assert t.exit_price == 100
        assert t.pnl_cents == (100 - 8) * 10 - 2  # 918

    def test_losing_no_trade(self):
        track = self._run_single_trade('no', 'yes', limit_price=8, contracts=10)
        t = track.trades[0]
        assert t.exit_price == 0
        assert t.pnl_cents == (0 - 8) * 10 - 2  # -82


# ── Independent cost verification ────────────────────────────


class TestIndependentCostVerification:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_fee_comes_from_view_cost(self):
        """Runner must use View.cost(), not any strategy-internal estimate."""
        settlement_time = T0 + timedelta(days=1)
        periods = make_periods(48)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        # View charges exactly 7 cents — a distinctive value
        vf = TrackingViewFactory(cost_fn=lambda p, c: 7)
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        assert track.trades[0].fee_cents == 7

    def test_cost_uses_correct_price_and_contracts(self):
        """Verify price and contracts are passed to View.cost()."""
        calls = []

        def tracking_cost(price, contracts):
            calls.append((price, contracts))
            return 3

        settlement_time = T0 + timedelta(days=1)
        periods = make_periods(48)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory(cost_fn=tracking_cost)
        opp = MockOpportunity(ticker='T1', limit_price=95, contracts=3)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        runner.replay(source, starting_capital_cents=10000)

        # View.cost() called with the order's price and contracts
        assert (95, 3) in calls


# ── Recalibration schedule ───────────────────────────────────


class TestRecalibrationSchedule:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_daily_new_view_per_day(self):
        """ViewFactory.build() called once per calendar day."""
        day1 = make_periods(12, start=T0)
        day2 = make_periods(12, start=T0 + timedelta(days=1))
        periods = day1 + day2

        source = SimpleMarketSource(periods_list=periods)
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        runner.replay(source, recalibration_schedule='daily')

        assert len(vf.build_calls) == 2
        assert vf.build_calls[0] == T0.replace(hour=0)
        assert vf.build_calls[1] == (T0 + timedelta(days=1)).replace(hour=0)

    def test_hourly_new_view_per_hour(self):
        periods = [
            datetime(2025, 6, 1, 10, 15, tzinfo=timezone.utc),
            datetime(2025, 6, 1, 10, 45, tzinfo=timezone.utc),
            datetime(2025, 6, 1, 11, 15, tzinfo=timezone.utc),
        ]

        source = SimpleMarketSource(periods_list=periods)
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        runner.replay(source, recalibration_schedule='hourly')

        assert len(vf.build_calls) == 2  # 10:00 and 11:00

    def test_weekly_new_view_per_week(self):
        # 2025-06-02 is Monday, 2025-06-09 is next Monday
        week1 = datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc)
        week2 = datetime(2025, 6, 9, 12, 0, tzinfo=timezone.utc)

        source = SimpleMarketSource(periods_list=[week1, week2])
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        runner.replay(source, recalibration_schedule='weekly')

        assert len(vf.build_calls) == 2

    def test_strategy_gets_new_view_on_recalibration(self):
        """Each recalibration creates a fresh strategy with the new view."""
        day1 = make_periods(4, start=T0)
        day2 = make_periods(4, start=T0 + timedelta(days=1))

        source = SimpleMarketSource(periods_list=day1 + day2)
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        runner.replay(source, recalibration_schedule='daily')

        # Two strategy instances created (one per day)
        assert len(MockStrategy.instances) == 2
        # Each got a different view
        assert MockStrategy.instances[0].view is not MockStrategy.instances[1].view


# ── Capital tracking ─────────────────────────────────────────


class TestCapitalTracking:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_insufficient_capital_skips_order(self):
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory()
        # Order costs 92 * 5 = 460¢, but only 100¢ available
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=100)

        assert len(track) == 0

    def test_multiple_orders_limited_by_capital(self):
        """Can only afford some of the opportunities."""
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: [
                {'event_ticker': 'EVT-1', 'markets': [
                    {'ticker': 'T1'}, {'ticker': 'T2'}, {'ticker': 'T3'}
                ]}
            ]},
            settlements={
                'T1': {'result': 'yes', 'settled_at': settlement_time,
                       'event': 'EVT-1', 'series': 'T'},
                'T2': {'result': 'yes', 'settled_at': settlement_time,
                       'event': 'EVT-1', 'series': 'T'},
                'T3': {'result': 'yes', 'settled_at': settlement_time,
                       'event': 'EVT-1', 'series': 'T'},
            },
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 0)
        # Each costs 50 * 1 = 50¢. Capital = 120¢ → can afford 2, not 3
        opps = [
            MockOpportunity(ticker='T1', limit_price=50, contracts=1),
            MockOpportunity(ticker='T2', limit_price=50, contracts=1),
            MockOpportunity(ticker='T3', limit_price=50, contracts=1),
        ]

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': opps})
        track = runner.replay(source, starting_capital_cents=120)

        assert len(track) == 2  # only 2 could be placed


# ── Fill simulation ──────────────────────────────────────────


class TestFillSimulation:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_delayed_fill_still_settles(self):
        """Order fills after delay, then settles correctly."""
        settlement_time = T0 + timedelta(days=3)
        periods = make_periods(96)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 1)
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(
            source, fill_simulator=DelayedFillSimulator(delay_periods=3))

        assert len(track) == 1
        assert track.trades[0].exit_price == 100

    def test_never_fill_no_trade(self):
        """Unfilled order → no trade recorded, escrow returned."""
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory()
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(
            source, fill_simulator=NeverFillSimulator(),
            starting_capital_cents=10000)

        assert len(track) == 0

    def test_partial_fill_at_settlement(self):
        """Order partially filled when market settles → trade for filled qty."""
        settlement_time = T0 + timedelta(hours=2)  # settles before full fill
        periods = make_periods(5)  # 5 hourly periods

        # Simulator fills 2 contracts per period
        class PartialFillSimulator:
            def on_order(self, ticker, order, period):
                pass

            def check_fills(self, ticker, order, period):
                remaining = order['contracts'] - order.get('contracts_filled', 0)
                return min(2, remaining)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 1)
        opp = MockOpportunity(ticker='T1', limit_price=90, contracts=10)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, fill_simulator=PartialFillSimulator(),
                              starting_capital_cents=10000)

        # Period 0: order placed (pending, 0 filled)
        # Period 1: check_fills → 2 filled. Then settlement check: T0+2h > T0+1h, not yet.
        # Period 2: settlement check first: T0+2h <= T0+2h → settle! 2 contracts filled.
        assert len(track) == 1
        assert track.trades[0].contracts == 2
        assert track.trades[0].exit_price == 100


# ── Active ticker tracking ───────────────────────────────────


class TestActiveTickers:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_same_ticker_not_traded_twice(self):
        settlement_time = T0 + timedelta(days=5)
        periods = make_periods(24)

        # Events in multiple periods
        events = {p: make_events(p) for p in periods[:5]}

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period=events,
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 1)
        opp = MockOpportunity(ticker='T1', limit_price=10, contracts=1)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        assert len(track) == 1


# ── End-of-replay settlement ────────────────────────────────


class TestEndOfReplay:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_filled_positions_settled_at_end(self):
        """Positions that haven't settled during the loop get settled at end."""
        settlement_time = T0 + timedelta(days=30)
        periods = make_periods(24)  # only 1 day of periods

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 1)
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        assert len(track) == 1
        assert track.trades[0].exit_price == 100

    def test_no_settlement_data_ignored(self):
        """Positions without settlement data are NOT settled at end."""
        periods = make_periods(24)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={},  # no settlement data at all
        )

        vf = TrackingViewFactory()
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        assert len(track) == 0


# ── Risk limits ──────────────────────────────────────────────


class TestRiskLimits:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_deployment_limit_blocks_order(self):
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory()
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)
        limits = MockRiskLimits(block_deployment=True)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, risk_limits=limits,
                              starting_capital_cents=10000)

        assert len(track) == 0

    def test_position_count_limit_blocks_period(self):
        settlement_time = T0 + timedelta(days=5)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'T',
            }},
        )

        vf = TrackingViewFactory()
        opp = MockOpportunity(ticker='T1', limit_price=92, contracts=5)
        # max_positions=0 blocks everything
        limits = MockRiskLimits(max_positions=0)

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, risk_limits=limits,
                              starting_capital_cents=10000)

        assert len(track) == 0


# ── Empty/edge cases ────────────────────────────────────────


class TestEdgeCases:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_no_periods(self):
        source = SimpleMarketSource(periods_list=[])
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy)
        track = runner.replay(source)

        assert len(track) == 0
        assert len(vf.build_calls) == 0

    def test_no_events_any_period(self):
        periods = make_periods(24)
        source = SimpleMarketSource(periods_list=periods)  # no events

        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        track = runner.replay(source)

        assert len(track) == 0

    def test_strategy_returns_no_opportunities(self):
        periods = make_periods(24)
        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
        )
        vf = TrackingViewFactory()

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': []})
        track = runner.replay(source)

        assert len(track) == 0


# ── Multiple trades ──────────────────────────────────────────


class TestMultipleTrades:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_two_trades_different_tickers(self):
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: [
                {'event_ticker': 'EVT-1', 'markets': [
                    {'ticker': 'T1'}, {'ticker': 'T2'}
                ]}
            ]},
            settlements={
                'T1': {'result': 'yes', 'settled_at': settlement_time,
                       'event': 'EVT-1', 'series': 'T'},
                'T2': {'result': 'no', 'settled_at': settlement_time,
                       'event': 'EVT-1', 'series': 'T'},
            },
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 1)
        opps = [
            MockOpportunity(ticker='T1', side='yes', limit_price=92, contracts=3),
            MockOpportunity(ticker='T2', side='no', limit_price=8, contracts=3),
        ]

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': opps})
        track = runner.replay(source, starting_capital_cents=10000)

        assert len(track) == 2
        t1 = next(t for t in track.trades if t.ticker == 'T1')
        t2 = next(t for t in track.trades if t.ticker == 'T2')
        assert t1.exit_price == 100   # YES won
        assert t2.exit_price == 100   # NO side won (result='no')


# ── TradeRecord field completeness ───────────────────────────


class TestTradeRecordFields:

    def setup_method(self):
        MockStrategy.instances.clear()

    def test_all_fields_populated(self):
        settlement_time = T0 + timedelta(days=2)
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={'T1': {
                'result': 'yes', 'settled_at': settlement_time,
                'event': 'EVT-1', 'series': 'TSERIES',
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: 3)
        opp = MockOpportunity(
            ticker='T1', event_ticker='EVT-1', side='yes',
            limit_price=92, contracts=5, ev_per_contract=3.0,
            p_event=0.95, p_fill=0.5,
            generating_process='continuous', topic='financial',
        )

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        t = track.trades[0]
        assert t.ticker == 'T1'
        assert t.event_ticker == 'EVT-1'
        assert t.side == 'yes'
        assert t.entry_price == 92
        assert t.contracts == 5
        assert t.exit_price == 100
        assert t.fee_cents == 3
        assert t.series == 'TSERIES'
        assert t.generating_process == 'continuous'
        assert t.topic == 'financial'
        assert abs(t.days_held - 2.0) < 0.02
        assert t.p_event == 0.95
        assert t.p_fill == 0.5
        assert t.edge_estimate == 3.0 / 100.0
        assert '2025-06-01' in t.entry_date


# ── validate_estimator contract ──────────────────────────────


class TestValidateEstimator:

    def test_stub_raises_not_implemented(self):
        """validate_estimator exists with spec signature, raises NotImplementedError."""
        vf = TrackingViewFactory()
        runner = Runner(vf, MockStrategy)

        with pytest.raises(NotImplementedError):
            runner.validate_estimator('event_rate', T0, lambda p, a: {})

    def test_signature_matches_spec(self):
        """validate_estimator has the 3 parameters from spec Section 5.3."""
        import inspect
        sig = inspect.signature(Runner.validate_estimator)
        params = list(sig.parameters.keys())
        assert 'estimator_name' in params
        assert 'split_date' in params
        assert 'metric_fn' in params

    def test_validation_result_type_exists(self):
        """ValidationResult dataclass is importable."""
        from framework.runner import ValidationResult
        result = ValidationResult(
            estimator_name='test', split_date=T0,
            n_predictions=100, metric_values={'gap': 0.05})
        assert result.estimator_name == 'test'
        assert result.metric_values['gap'] == 0.05


# ── TrackRecord consistency with replay.py ───────────────────


class TestReplayConsistency:
    """Runner's settlement logic must produce same TradeRecords as replay.py.

    Spec Section 7.2: "replay produces TrackRecord consistent with existing replay.py"

    These tests replicate replay.py's _record_trade formula (lines 936-955)
    and verify the Runner produces identical TradeRecords when given
    equivalent inputs and cost models.
    """

    def setup_method(self):
        MockStrategy.instances.clear()

    @staticmethod
    def _replay_expected(order, md, ticker, contracts, cost_fn):
        """Replicate replay.py's _record_trade output for comparison.

        Source: trading/replay.py lines 936-955 (verified 2026-04-11).
        """
        result = md['result']
        won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
        exit_price = 100 if won else 0
        fee = cost_fn(order['price'], contracts)
        return TradeRecord(
            ticker=ticker, side=order['side'],
            entry_price=order['price'], contracts=contracts,
            exit_price=exit_price, fee_cents=fee,
            days_held=(
                md['settled_at'] - order['placed_at']
            ).total_seconds() / 86400,
            edge_estimate=order['edge'],
            event_ticker=order['event'],
            series=ticker.split('-')[0],  # replay.py always uses this
            generating_process=order.get('gp', ''),
            topic=order.get('topic', ''),
            entry_date=order['placed_at'].strftime('%Y-%m-%d %H:%M'),
            p_event=order.get('p_event', 0.0) or 0.0,
            p_fill=order.get('p_fill', 0.0) or 0.0,
        )

    @staticmethod
    def _assert_records_match(runner_trade, replay_trade):
        """Assert all TradeRecord fields match between Runner and replay.py."""
        assert runner_trade.ticker == replay_trade.ticker
        assert runner_trade.side == replay_trade.side
        assert runner_trade.entry_price == replay_trade.entry_price
        assert runner_trade.contracts == replay_trade.contracts
        assert runner_trade.exit_price == replay_trade.exit_price
        assert runner_trade.fee_cents == replay_trade.fee_cents
        assert abs(runner_trade.days_held - replay_trade.days_held) < 0.001
        assert runner_trade.edge_estimate == replay_trade.edge_estimate
        assert runner_trade.event_ticker == replay_trade.event_ticker
        assert runner_trade.series == replay_trade.series
        assert runner_trade.generating_process == replay_trade.generating_process
        assert runner_trade.topic == replay_trade.topic
        assert runner_trade.entry_date == replay_trade.entry_date
        assert runner_trade.p_event == replay_trade.p_event
        assert runner_trade.p_fill == replay_trade.p_fill

    def test_winning_yes_matches_replay(self):
        """Winning YES trade: Runner matches replay.py field-for-field."""
        from trading.cost_model import CostModel
        costs = CostModel()

        placed_at = T0
        settled_at = T0 + timedelta(days=3, hours=7)
        ticker = 'KXBTCD-26JUN01-T60399'

        order = {
            'side': 'yes', 'price': 93, 'contracts': 4,
            'contracts_filled': 4, 'event': 'KXBTCD-26JUN01',
            'edge': 0.03, 'placed_at': placed_at,
            'gp': 'continuous_underlyer', 'topic': 'financial',
            'p_event': 0.96, 'p_fill': 0.45,
        }
        md = {
            'result': 'yes', 'settled_at': settled_at,
            'event': 'KXBTCD-26JUN01',
            'series': ticker.split('-')[0],  # KXBTCD
        }

        # What replay.py would produce
        expected = self._replay_expected(
            order, md, ticker, 4, costs.maker_fee)

        # Runner path: View.cost wraps same CostModel
        view = SimpleView(cost_fn=lambda p, c: costs.maker_fee(p, c))
        runner_track = TrackRecord()
        from framework.runner import _record_trade
        _record_trade(order, md, ticker, 4, view, runner_track, TradeRecord)

        self._assert_records_match(runner_track.trades[0], expected)

    def test_losing_no_matches_replay(self):
        """Losing NO trade: Runner matches replay.py field-for-field."""
        from trading.cost_model import CostModel
        costs = CostModel()

        placed_at = T0
        settled_at = T0 + timedelta(days=1, hours=12)
        ticker = 'INXD-25APR11-T35500'

        order = {
            'side': 'no', 'price': 7, 'contracts': 8,
            'contracts_filled': 8, 'event': 'INXD-25APR11',
            'edge': 0.015, 'placed_at': placed_at,
            'gp': 'continuous_underlyer', 'topic': 'financial',
            'p_event': 0.94, 'p_fill': 0.6,
        }
        md = {
            'result': 'yes', 'settled_at': settled_at,
            'event': 'INXD-25APR11',
            'series': ticker.split('-')[0],
        }

        expected = self._replay_expected(
            order, md, ticker, 8, costs.maker_fee)

        view = SimpleView(cost_fn=lambda p, c: costs.maker_fee(p, c))
        runner_track = TrackRecord()
        from framework.runner import _record_trade
        _record_trade(order, md, ticker, 8, view, runner_track, TradeRecord)

        self._assert_records_match(runner_track.trades[0], expected)

    def test_end_to_end_single_trade_matches_replay(self):
        """Full Runner replay produces same TradeRecord as replay.py formula."""
        from trading.cost_model import CostModel
        costs = CostModel()

        placed_at = T0  # order placed in first period
        settled_at = T0 + timedelta(days=2)
        ticker = 'KXBTCD-26JUN01-T60399'
        periods = make_periods(72)

        source = SimpleMarketSource(
            periods_list=periods,
            events_by_period={periods[0]: make_events(periods[0])},
            settlements={ticker: {
                'result': 'yes', 'settled_at': settled_at,
                'event': 'KXBTCD-26JUN01',
                'series': ticker.split('-')[0],
            }},
        )

        vf = TrackingViewFactory(cost_fn=lambda p, c: costs.maker_fee(p, c))
        opp = MockOpportunity(
            ticker=ticker, event_ticker='KXBTCD-26JUN01',
            side='yes', limit_price=93, contracts=4,
            ev_per_contract=3.0, p_event=0.96, p_fill=0.45,
            generating_process='continuous_underlyer', topic='financial',
        )

        runner = Runner(vf, MockStrategy,
                        strategy_kwargs={'opportunities': [opp]})
        track = runner.replay(source, starting_capital_cents=10000)

        # Build expected from replay.py formula
        order_for_replay = {
            'side': 'yes', 'price': 93, 'contracts': 4,
            'event': 'KXBTCD-26JUN01',
            'edge': 3.0 / 100.0,
            'placed_at': placed_at,
            'gp': 'continuous_underlyer', 'topic': 'financial',
            'p_event': 0.96, 'p_fill': 0.45,
        }
        md = {
            'result': 'yes', 'settled_at': settled_at,
            'event': 'KXBTCD-26JUN01',
            'series': ticker.split('-')[0],
        }
        expected = self._replay_expected(
            order_for_replay, md, ticker, 4, costs.maker_fee)

        assert len(track) == 1
        self._assert_records_match(track.trades[0], expected)
