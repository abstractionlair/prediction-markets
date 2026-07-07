"""Tests for ev_strategy.py — EV computation and strategy logic."""

from trading.ev_strategy import compute_trade_ev, EVStrategy, EVOpportunity, MAX_CAPITAL_PER_ORDER_CENTS
from trading.flow_model import FlowCDF, FlowModel


class TestComputeTradeEV:
    def test_positive_ev_yes_side(self):
        # P(YES)=0.95, fills 30% when winning, 70% when losing
        # Limit=90¢ (YES buy). Fee=1¢.
        ev = compute_trade_ev(
            p_event=0.95, p_fill_won=0.30, p_fill_lost=0.70,
            limit_price=90, side='yes', fee=1)
        # 0.95 * 0.30 * 10 - 0.05 * 0.70 * 90 - (0.95*0.30 + 0.05*0.70) * 1
        # = 2.85 - 3.15 - 0.32 = -0.62
        assert abs(ev - (-0.62)) < 0.01

    def test_positive_ev_no_side(self):
        # P(YES)=0.05, NO buyer. P(NO)=0.95.
        # Fill when NO wins (YES=no): 30%. Fill when NO loses (YES=yes): 70%.
        # Limit=90¢ for NO. Fee=1¢.
        ev = compute_trade_ev(
            p_event=0.05, p_fill_won=0.30, p_fill_lost=0.70,
            limit_price=90, side='no', fee=1)
        # p_win=0.95, p_lose=0.05
        # 0.95 * 0.30 * 10 - 0.05 * 0.70 * 90 - fill_total * 1
        assert abs(ev - (-0.62)) < 0.01

    def test_zero_fill_gives_zero_ev(self):
        ev = compute_trade_ev(
            p_event=0.95, p_fill_won=0.0, p_fill_lost=0.0,
            limit_price=90, side='yes', fee=1)
        assert ev == 0.0

    def test_certain_fill_certain_win(self):
        # P(YES)=1.0, always fills. Limit=90. Fee=1.
        ev = compute_trade_ev(
            p_event=1.0, p_fill_won=1.0, p_fill_lost=0.5,
            limit_price=90, side='yes', fee=1)
        # 1.0 * 1.0 * 10 - 0 - 1.0 * 1 = 9.0
        assert abs(ev - 9.0) < 0.01

    def test_crossing_spread_always_fills(self):
        # At ask (rel=1.0), fill rate = 100% regardless of outcome
        ev = compute_trade_ev(
            p_event=0.95, p_fill_won=1.0, p_fill_lost=1.0,
            limit_price=93, side='yes', fee=1)
        # 0.95 * 1.0 * 7 - 0.05 * 1.0 * 93 - 1.0 * 1
        # = 6.65 - 4.65 - 1.0 = 1.0
        assert abs(ev - 1.0) < 0.01

    def test_no_edge_gives_negative_ev_from_fees(self):
        # P(YES)=0.90, price=90¢. No edge. Equal fill rates. Fee=1.
        ev = compute_trade_ev(
            p_event=0.90, p_fill_won=0.50, p_fill_lost=0.50,
            limit_price=90, side='yes', fee=1)
        # 0.90 * 0.50 * 10 - 0.10 * 0.50 * 90 - 0.50 * 1
        # = 4.5 - 4.5 - 0.5 = -0.5
        assert ev < 0

    def test_adverse_selection_destroys_edge(self):
        # Strong calibration edge (95% vs 90¢) but severe adverse selection
        ev_no_adverse = compute_trade_ev(
            p_event=0.95, p_fill_won=0.50, p_fill_lost=0.50,
            limit_price=90, side='yes', fee=1)
        ev_adverse = compute_trade_ev(
            p_event=0.95, p_fill_won=0.10, p_fill_lost=0.90,
            limit_price=90, side='yes', fee=1)
        # Adverse selection should reduce EV
        assert ev_adverse < ev_no_adverse

    def test_symmetry_yes_no(self):
        # YES at 90¢ with P(YES)=0.95 should give same EV as
        # NO at 90¢ with P(YES)=0.05 (same price, symmetric situation)
        ev_yes = compute_trade_ev(
            p_event=0.95, p_fill_won=0.30, p_fill_lost=0.70,
            limit_price=90, side='yes', fee=1)
        ev_no = compute_trade_ev(
            p_event=0.05, p_fill_won=0.30, p_fill_lost=0.70,
            limit_price=90, side='no', fee=1)
        # Identical: same p_win, same fill rates, same price, same fee
        assert abs(ev_yes - ev_no) < 0.01


# ── EVOpportunity ────────────────────────────────────────────────

class TestEVOpportunity:
    def test_total_ev_field(self):
        opp = EVOpportunity(
            ticker='T', event_ticker='E', series='S',
            side='no', limit_price=92, ev_per_contract=2.5,
            total_ev=12.5, p_event=0.05, p_fill=0.7,
            contracts=5, days_to_settle=1.0,
            generating_process='gp', topic='topic',
        )
        assert opp.total_ev == 12.5
        assert opp.ev_per_contract * opp.contracts == opp.total_ev


# ── _quantity_steps ──────────────────────────────────────────────

class TestQuantitySteps:
    def test_small_max(self):
        assert EVStrategy._quantity_steps(8) == [1, 2, 4, 5, 8]

    def test_includes_max(self):
        steps = EVStrategy._quantity_steps(12)
        assert steps[-1] == 12

    def test_exact_step(self):
        steps = EVStrategy._quantity_steps(20)
        assert 20 in steps

    def test_max_1(self):
        assert EVStrategy._quantity_steps(1) == [1]

    def test_max_3(self):
        steps = EVStrategy._quantity_steps(3)
        assert steps == [1, 2, 3]

    def test_large_max(self):
        steps = EVStrategy._quantity_steps(50)
        assert steps == [1, 2, 4, 5, 8, 10, 15, 20, 30, 50]


# ── _find_best_order with FlowModel ─────────────────────────────

class _FakeView:
    """Minimal view implementing the framework View API for testing."""

    def __init__(self, flow_model=None, event_rate_val=None,
                 classifications=None, fill_rates_fn=None):
        self._flow_model = flow_model
        self._event_rate_val = event_rate_val or (0.95, 0.01, 100)
        self._classifications = classifications or {}
        self._fill_rates_fn = fill_rates_fn

    def fill_probability(self, side, limit_price, quantity, market_state):
        """View.fill_probability() API."""
        if self._flow_model is not None:
            gp = market_state['generating_process']
            topic = market_state['topic']
            hours = market_state['hours_to_settlement']
            trailing_vol = market_state.get('trailing_volume', 0)
            return self._flow_model.estimate(gp, topic, hours, side, quantity,
                                              limit_price, trailing_vol)
        if self._fill_rates_fn is not None:
            return self._fill_rates_fn(side, limit_price, quantity, market_state)
        return None

    def event_rate(self, series, hours, **kwargs):
        return self._event_rate_val

    def classification(self, series):
        return self._classifications.get(series)

    def cost(self, price_cents, contracts, is_maker=True):
        from trading.cost_model import KALSHI_COSTS
        if is_maker:
            return KALSHI_COSTS.maker_fee(price_cents, contracts)
        return KALSHI_COSTS.taker_fee(price_cents, contracts)


def _make_test_flow_table():
    """Flow table with data at level 5 (outcome merged) for testing."""
    table = {}
    # Enough data at outcome-merged level for gp/topic/<3h/no
    table[('gp', 'topic', '<3h', 'no', '*', '*', '*')] = FlowCDF(
        thresholds=[1, 2, 5, 10, 20, 50],
        exceedances=[0.90, 0.85, 0.75, 0.60, 0.45, 0.25],
        n_observations=1000, n_outcome=1000,
    )
    # YES side has lower fill rates
    table[('gp', 'topic', '<3h', 'yes', '*', '*', '*')] = FlowCDF(
        thresholds=[1, 2, 5, 10, 20, 50],
        exceedances=[0.80, 0.75, 0.60, 0.45, 0.30, 0.15],
        n_observations=1000, n_outcome=1000,
    )
    return table


class TestFindBestOrder:
    def test_returns_result(self):
        flow_table = _make_test_flow_table()
        model = FlowModel(flow_table)
        # P(YES)=0.95 → YES buyer wins 95% → good for YES at 88-93¢
        view = _FakeView(model, event_rate_val=(0.95, 0.01, 100))
        strategy = EVStrategy(view)
        result = strategy._find_best_order(
            view, 'gp', 'topic', 2.0, 88, 93, 0.95, 500, 8)
        assert result is not None
        side, limit, q, total_ev, ev_per, p_fill = result
        assert total_ev > 0
        assert side == 'yes'  # YES side has edge at P(YES)=0.95

    def test_quantity_affects_total_ev(self):
        """Joint search should explore multiple quantities."""
        flow_table = _make_test_flow_table()
        model = FlowModel(flow_table)
        view = _FakeView(model)
        strategy = EVStrategy(view)
        r1 = strategy._find_best_order(
            view, 'gp', 'topic', 2.0, 88, 93, 0.95, 500, 1)
        r8 = strategy._find_best_order(
            view, 'gp', 'topic', 2.0, 88, 93, 0.95, 500, 8)
        if r1 and r8:
            assert r8[3] >= r1[3]  # total_ev with more contracts >= less

    def test_capital_constraint(self):
        """Orders exceeding MAX_CAPITAL_PER_ORDER_CENTS are skipped."""
        flow_table = _make_test_flow_table()
        model = FlowModel(flow_table)
        view = _FakeView(model)
        strategy = EVStrategy(view)
        # At 95¢, max q = floor(400/95) = 4
        result = strategy._find_best_order(
            view, 'gp', 'topic', 2.0, 93, 96, 0.95, 500, 100)
        if result:
            side, limit, q, total_ev, ev_per, p_fill = result
            assert q * limit <= MAX_CAPITAL_PER_ORDER_CENTS

    def test_skips_out_of_range_side(self):
        """Sides with bid>97 or ask<85 are skipped."""
        flow_table = _make_test_flow_table()
        model = FlowModel(flow_table)
        view = _FakeView(model)
        strategy = EVStrategy(view)
        # YES bid=98, ask=99 → out of range for YES; NO bid=1, ask=2 → out of range
        result = strategy._find_best_order(
            view, 'gp', 'topic', 2.0, 98, 99, 0.05, 500, 8)
        assert result is None
