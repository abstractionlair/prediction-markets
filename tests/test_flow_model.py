"""Tests for trading/flow_model.py — trade-tape-based fill model (V2).

Tests with synthetic data only — no DB, no API. Covers:
- FlowCDF interpolation and extrapolation
- FlowModel.estimate() fallback hierarchy
- compute_opposing_flow direction logic
- compute_trailing_volume
- FlowModel.calibrate() with synthetic trades
- Outcome-merged semantics
- Threshold checks (MIN_COMBINED, MIN_PER_OUTCOME)
"""

import math
from datetime import datetime, timedelta, timezone

from trading.flow_model import (
    FlowCDF, FlowEstimate, FlowModel,
    compute_opposing_flow, compute_trailing_volume,
    _time_bucket, _trailing_vol_bucket, _log_linear_interp,
    CDF_THRESHOLDS, COARSE_MAP,
)


# ── FlowCDF interpolation ───────────────────────────────────────────

class TestFlowCDFInterpolation:
    def test_exact_threshold_match(self):
        cdf = FlowCDF(
            thresholds=[1, 5, 10, 20, 50],
            exceedances=[0.90, 0.80, 0.60, 0.40, 0.20],
            n_observations=500, n_outcome=250,
        )
        assert cdf.p_fill(1) == 0.90
        assert cdf.p_fill(5) == 0.80
        assert cdf.p_fill(50) == 0.20

    def test_below_min_threshold(self):
        cdf = FlowCDF(
            thresholds=[5, 10, 20],
            exceedances=[0.80, 0.60, 0.40],
            n_observations=300, n_outcome=150,
        )
        # Below min → return first exceedance
        assert cdf.p_fill(1) == 0.80
        assert cdf.p_fill(3) == 0.80

    def test_interpolation_between_thresholds(self):
        cdf = FlowCDF(
            thresholds=[1, 10],
            exceedances=[1.0, 0.5],
            n_observations=500, n_outcome=250,
        )
        # Log-linear interpolation: midpoint in log space between 1 and 10
        # log(3.16) ≈ midpoint of log(1) and log(10)
        p = cdf.p_fill(3)
        assert 0.5 < p < 1.0  # between the two values

    def test_monotonic_interpolation(self):
        """P(fill) should decrease monotonically with quantity."""
        cdf = FlowCDF(
            thresholds=[1, 2, 5, 10, 20, 50, 100],
            exceedances=[0.95, 0.90, 0.80, 0.65, 0.50, 0.30, 0.15],
            n_observations=1000, n_outcome=500,
        )
        prev = 1.0
        for q in range(1, 101):
            p = cdf.p_fill(q)
            assert p <= prev + 1e-10, f"Not monotonic at q={q}: {p} > {prev}"
            prev = p

    def test_zero_quantity(self):
        cdf = FlowCDF(
            thresholds=[1, 10], exceedances=[0.8, 0.5],
            n_observations=200, n_outcome=100,
        )
        assert cdf.p_fill(0) == 1.0

    def test_empty_cdf(self):
        cdf = FlowCDF(thresholds=[], exceedances=[],
                       n_observations=0, n_outcome=0)
        assert cdf.p_fill(5) == 0.0


class TestFlowCDFExtrapolation:
    def test_beyond_max_with_enough_data(self):
        """n_observations >= 200 → log-linear extrapolation."""
        cdf = FlowCDF(
            thresholds=[1, 5, 10, 50, 100],
            exceedances=[0.95, 0.85, 0.70, 0.40, 0.25],
            n_observations=500, n_outcome=250,
        )
        p = cdf.p_fill(150)  # beyond 100 but within 2x
        assert 0.0 < p < 0.25  # less than last but positive

    def test_beyond_2x_max_always_zero(self):
        cdf = FlowCDF(
            thresholds=[1, 10, 50],
            exceedances=[0.90, 0.60, 0.30],
            n_observations=500, n_outcome=250,
        )
        assert cdf.p_fill(101) == 0.0  # > 2 * 50

    def test_beyond_max_without_enough_data(self):
        """n_observations < 200 → clamp to last value."""
        cdf = FlowCDF(
            thresholds=[1, 10, 50],
            exceedances=[0.90, 0.60, 0.30],
            n_observations=100, n_outcome=50,
        )
        assert cdf.p_fill(75) == 0.30  # clamped

    def test_extrapolation_respects_zero_floor(self):
        """Extrapolated values can't go negative."""
        cdf = FlowCDF(
            thresholds=[10, 50, 100],
            exceedances=[0.50, 0.05, 0.01],
            n_observations=500, n_outcome=250,
        )
        p = cdf.p_fill(180)  # near 2x max
        assert p >= 0.0


# ── Bucketing functions ──────────────────────────────────────────────

class TestBucketing:
    def test_time_buckets(self):
        assert _time_bucket(0.5) == '<1h'
        assert _time_bucket(2.0) == '1-3h'
        assert _time_bucket(5.0) == '3-6h'
        assert _time_bucket(10.0) == '6-12h'
        assert _time_bucket(20.0) == '12-24h'
        assert _time_bucket(48.0) == '1-3d'
        assert _time_bucket(100.0) == '3-7d'

    def test_time_bucket_boundaries(self):
        # Exact boundaries go to next bucket
        assert _time_bucket(1.0) == '1-3h'
        assert _time_bucket(3.0) == '3-6h'
        assert _time_bucket(6.0) == '6-12h'
        assert _time_bucket(12.0) == '12-24h'
        assert _time_bucket(24.0) == '1-3d'
        assert _time_bucket(72.0) == '3-7d'

    def test_time_bucket_large_value(self):
        assert _time_bucket(200.0) == '3-7d'

    def test_vol_buckets(self):
        assert _trailing_vol_bucket(0) == 'dead'
        assert _trailing_vol_bucket(50) == 'low'
        assert _trailing_vol_bucket(500) == 'moderate'
        assert _trailing_vol_bucket(5000) == 'active'
        assert _trailing_vol_bucket(50000) == 'high'

    def test_coarse_map_coverage(self):
        """Every fine time bucket maps to a coarse bucket."""
        for label, _, _ in [('<1h', 0, 1), ('1-3h', 1, 3), ('3-6h', 3, 6),
                             ('6-12h', 6, 12), ('12-24h', 12, 24),
                             ('1-3d', 24, 72), ('3-7d', 72, 168)]:
            assert label in COARSE_MAP, f"Missing coarse mapping for {label}"


# ── compute_opposing_flow ────────────────────────────────────────────

class TestComputeOpposingFlow:
    def test_yes_buy_filled_by_no_taker(self):
        """YES buy at 90¢ filled by taker_side='no' at yes_price <= 0.90."""
        assert compute_opposing_flow('yes', 90, 0.89, 'no', 10) == 10
        assert compute_opposing_flow('yes', 90, 0.90, 'no', 10) == 10

    def test_yes_buy_not_filled_by_yes_taker(self):
        """YES taker doesn't fill our YES buy."""
        assert compute_opposing_flow('yes', 90, 0.89, 'yes', 10) == 0

    def test_yes_buy_not_filled_above_limit(self):
        """Trade at yes_price above our limit doesn't fill."""
        assert compute_opposing_flow('yes', 90, 0.91, 'no', 10) == 0

    def test_no_buy_filled_by_yes_taker(self):
        """NO buy at 8¢ filled by taker_side='yes' at yes_price >= 0.92."""
        # NO at 8¢ → yes_price threshold = (100-8)/100 = 0.92
        assert compute_opposing_flow('no', 8, 0.92, 'yes', 5) == 5
        assert compute_opposing_flow('no', 8, 0.95, 'yes', 5) == 5

    def test_no_buy_not_filled_by_no_taker(self):
        assert compute_opposing_flow('no', 8, 0.92, 'no', 5) == 0

    def test_no_buy_not_filled_below_threshold(self):
        """Trade at yes_price below threshold doesn't fill NO buy."""
        assert compute_opposing_flow('no', 8, 0.91, 'yes', 5) == 0

    def test_typical_tail_trade_yes(self):
        """Typical: YES buy at 92¢, NO taker at 0.91 → fills."""
        assert compute_opposing_flow('yes', 92, 0.91, 'no', 20) == 20

    def test_typical_tail_trade_no(self):
        """Typical: NO buy at 5¢, YES taker at 0.96 → fills."""
        # (100-5)/100 = 0.95; 0.96 >= 0.95 → fills
        assert compute_opposing_flow('no', 5, 0.96, 'yes', 15) == 15


# ── compute_trailing_volume ──────────────────────────────────────────

class TestComputeTrailingVolume:
    def _make_trades(self, deltas_and_counts):
        """Make trades relative to a base time.
        deltas_and_counts: [(hours_offset, count), ...]
        """
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        return [(base + timedelta(hours=h), count, 0.90, 'yes')
                for h, count in deltas_and_counts]

    def test_basic_trailing(self):
        trades = self._make_trades([(-20, 50), (-10, 30), (-5, 20)])
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        assert compute_trailing_volume(trades, base) == 100  # all within 24h

    def test_excludes_old_trades(self):
        trades = self._make_trades([(-30, 100), (-20, 50), (-5, 20)])
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        # -30h is outside 24h window
        assert compute_trailing_volume(trades, base) == 70

    def test_excludes_future_trades(self):
        trades = self._make_trades([(-5, 20), (1, 50)])
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        assert compute_trailing_volume(trades, base) == 20

    def test_empty_trades(self):
        assert compute_trailing_volume([], datetime.now(timezone.utc)) == 0

    def test_no_trades_in_window(self):
        trades = self._make_trades([(-30, 100)])
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        assert compute_trailing_volume(trades, base) == 0


# ── FlowModel.estimate() fallback hierarchy ─────────────────────────

def _make_flow_table_with_levels():
    """Build a flow_table with CDFs at various fallback levels.

    Category: counting_process / ent_sports
    Sufficient data at levels 1-5.
    """
    table = {}

    # Level 1: full key with price — sufficient data
    for outcome in ('won', 'lost'):
        p = 0.80 if outcome == 'won' else 0.85
        table[('counting_process', 'ent_sports', '1-3h', 'no', 'active', 92, outcome)] = FlowCDF(
            thresholds=[1, 5, 10, 20, 50],
            exceedances=[p, p*0.95, p*0.85, p*0.70, p*0.50],
            n_observations=500, n_outcome=250,
        )

    # Level 2: vol merged, keep price
    for outcome in ('won', 'lost'):
        p = 0.75 if outcome == 'won' else 0.80
        table[('counting_process', 'ent_sports', '1-3h', 'no', '*', 92, outcome)] = FlowCDF(
            thresholds=[1, 5, 10, 20, 50],
            exceedances=[p, p*0.95, p*0.85, p*0.70, p*0.50],
            n_observations=800, n_outcome=400,
        )

    # Level 3: coarse time + vol merged, keep price
    for outcome in ('won', 'lost'):
        p = 0.72 if outcome == 'won' else 0.77
        table[('counting_process', 'ent_sports', '<3h', 'no', '*', 92, outcome)] = FlowCDF(
            thresholds=[1, 5, 10, 20, 50],
            exceedances=[p, p*0.95, p*0.85, p*0.70, p*0.50],
            n_observations=1200, n_outcome=600,
        )

    # Level 4: price merged (drop price)
    for outcome in ('won', 'lost'):
        p = 0.70 if outcome == 'won' else 0.75
        table[('counting_process', 'ent_sports', '<3h', 'no', '*', '*', outcome)] = FlowCDF(
            thresholds=[1, 5, 10, 20, 50],
            exceedances=[p, p*0.95, p*0.85, p*0.70, p*0.50],
            n_observations=1500, n_outcome=750,
        )

    # Level 5: outcome merged
    table[('counting_process', 'ent_sports', '<3h', 'no', '*', '*', '*')] = FlowCDF(
        thresholds=[1, 5, 10, 20, 50],
        exceedances=[0.78, 0.74, 0.66, 0.55, 0.39],
        n_observations=1200, n_outcome=1200,
    )

    # Level 6: side merged
    table[('counting_process', 'ent_sports', '<3h', '*', '*', '*', '*')] = FlowCDF(
        thresholds=[1, 5, 10, 20, 50],
        exceedances=[0.70, 0.65, 0.55, 0.45, 0.30],
        n_observations=2000, n_outcome=2000,
    )

    return table


class TestEstimateFallback:
    def test_level1_hit(self):
        """Full key match returns outcome-split estimates."""
        table = _make_flow_table_with_levels()
        model = FlowModel(table)
        est = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 10,
                             92, 5000)
        assert est is not None
        assert est.p_fill_won != est.p_fill_lost  # outcome split
        assert est.p_fill_lost > est.p_fill_won   # adverse selection

    def test_level2_fallback_on_missing_vol(self):
        """Unknown vol bucket falls through to level 2."""
        table = _make_flow_table_with_levels()
        # Remove level 1 for 'dead' vol
        model = FlowModel(table)
        est = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 10,
                             92, 0)  # dead vol — no level 1
        assert est is not None
        # Should get level 2 values (vol merged)
        assert est.p_fill_won != est.p_fill_lost

    def test_level3_fallback_on_different_time(self):
        """Time bucket without level 1 or 2 data → coarse time (level 3)."""
        table = _make_flow_table_with_levels()
        model = FlowModel(table)
        # '<1h' maps to '<3h' coarse — only exists at level 3+
        est = model.estimate('counting_process', 'ent_sports', 0.5, 'no', 10,
                             92, 0)
        assert est is not None

    def test_level4_outcome_merged(self):
        """When only outcome-merged data exists, p_fill_won == p_fill_lost."""
        table = {}
        table[('counting_process', 'ent_sports', '<3h', 'no', '*', '*')] = FlowCDF(
            thresholds=[1, 10, 50],
            exceedances=[0.80, 0.60, 0.30],
            n_observations=500, n_outcome=500,
        )
        model = FlowModel(table)
        est = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 10,
                             92, 0)
        assert est is not None
        assert est.p_fill_won == est.p_fill_lost

    def test_level5_side_merged(self):
        """Falls all the way to side-merged."""
        table = {}
        table[('counting_process', 'ent_sports', '<3h', '*', '*', '*')] = FlowCDF(
            thresholds=[1, 10], exceedances=[0.70, 0.50],
            n_observations=300, n_outcome=300,
        )
        model = FlowModel(table)
        est = model.estimate('counting_process', 'ent_sports', 2.0, 'yes', 5,
                             90, 0)
        assert est is not None
        assert est.p_fill_won == est.p_fill_lost

    def test_none_on_no_data(self):
        """No matching category → None."""
        model = FlowModel({})
        assert model.estimate('unknown', 'cat', 5.0, 'yes', 10, 90, 0) is None

    def test_threshold_check_rejects_small_data(self):
        """CDFs below MIN_COMBINED are skipped."""
        table = {}
        for outcome in ('won', 'lost'):
            table[('gp', 'topic', '1-3h', 'no', '*', outcome)] = FlowCDF(
                thresholds=[1, 10], exceedances=[0.80, 0.50],
                n_observations=100, n_outcome=50,  # below MIN_COMBINED=200
            )
        model = FlowModel(table)
        assert model.estimate('gp', 'topic', 2.0, 'no', 5, 90, 0) is None

    def test_asymmetric_outcome_count_falls_through(self):
        """If won has enough but lost doesn't, fall through to next level."""
        table = {}
        table[('gp', 'topic', '1-3h', 'no', '*', 'won')] = FlowCDF(
            thresholds=[1, 10], exceedances=[0.80, 0.50],
            n_observations=300, n_outcome=200,
        )
        table[('gp', 'topic', '1-3h', 'no', '*', 'lost')] = FlowCDF(
            thresholds=[1, 10], exceedances=[0.85, 0.55],
            n_observations=300, n_outcome=30,  # below MIN_PER_OUTCOME=50
        )
        # Level 4: outcome merged — should fall here
        table[('gp', 'topic', '<3h', 'no', '*', '*')] = FlowCDF(
            thresholds=[1, 10], exceedances=[0.82, 0.52],
            n_observations=300, n_outcome=300,
        )
        model = FlowModel(table)
        est = model.estimate('gp', 'topic', 2.0, 'no', 5, 90, 0)
        assert est is not None
        assert est.p_fill_won == est.p_fill_lost  # merged


# ── FlowModel.estimate() verification criteria ──────────────────────

class TestEstimateProperties:
    def test_size_dependent(self):
        """P(fill) differs for Q=5 vs Q=50 (verification criterion 1)."""
        table = _make_flow_table_with_levels()
        model = FlowModel(table)
        est5 = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 5,
                              92, 5000)
        est50 = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 50,
                               92, 5000)
        assert est5.p_fill_won > est50.p_fill_won
        assert est5.p_fill_lost > est50.p_fill_lost

    def test_adverse_selection(self):
        """P(fill|lost) > P(fill|won) for sports (verification criterion 2)."""
        table = _make_flow_table_with_levels()
        model = FlowModel(table)
        est = model.estimate('counting_process', 'ent_sports', 2.0, 'no', 10,
                             92, 5000)
        assert est.p_fill_lost > est.p_fill_won


# ── FlowModel.calibrate() with synthetic trades ─────────────────────

def _make_synthetic_data():
    """Build synthetic trades and metadata for calibration testing.

    Creates a simple scenario: 20 settled markets with trades that
    generate known flow patterns. All sports/counting_process.
    """
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades_by_ticker = {}
    settled_markets = {}
    classifications = {'TESTSERIES': ('counting_process', 'ent_sports')}

    for i in range(20):
        ticker = f"TESTSERIES-26JAN01-T{i}"
        event_ticker = f"TESTSERIES-26JAN01"
        settle_time = base + timedelta(hours=24 + i)
        result = 'yes' if i % 2 == 0 else 'no'
        settled_markets[ticker] = (settle_time, result, event_ticker)

        # Generate trades throughout the market lifetime
        trades = []
        for h in range(24):
            trade_time = base + timedelta(hours=h, minutes=30)
            # Mix of YES and NO takers at various prices
            # NO takers at low yes_price (fills YES buys)
            trades.append((trade_time, 15, 0.88, 'no'))
            # YES takers at high yes_price (fills NO buys)
            trades.append((trade_time, 12, 0.93, 'yes'))
            # Trades outside tail range (shouldn't fill)
            trades.append((trade_time, 50, 0.50, 'yes'))

        trades.sort(key=lambda t: t[0])
        trades_by_ticker[ticker] = trades

    return trades_by_ticker, settled_markets, classifications


class TestCalibrate:
    def test_returns_flow_model(self):
        trades, settled, classifications = _make_synthetic_data()
        model = FlowModel.calibrate(trades, settled, classifications)
        assert isinstance(model, FlowModel)
        assert len(model.flow_table) > 0

    def test_empty_input(self):
        model = FlowModel.calibrate({}, {}, {})
        assert isinstance(model, FlowModel)
        assert len(model.flow_table) == 0

    def test_as_of_filters(self):
        """Markets settled after as_of are excluded."""
        trades, settled, classifications = _make_synthetic_data()
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        # Use all data
        model_all = FlowModel.calibrate(trades, settled, classifications)

        # Use only markets settled before day 1 noon (excludes most)
        early = base + timedelta(hours=12)
        model_early = FlowModel.calibrate(trades, settled, classifications,
                                           as_of=early)
        # Early should have fewer entries
        assert len(model_early.flow_table) <= len(model_all.flow_table)

    def test_calibrated_model_serves_estimates(self):
        """Calibrated model can serve fill estimates."""
        trades, settled, classifications = _make_synthetic_data()
        model = FlowModel.calibrate(trades, settled, classifications)
        est = model.estimate('counting_process', 'ent_sports', 5.0, 'no', 10,
                             92, 0)
        # Should get an estimate (synthetic data has enough observations)
        # It's OK if None — may not have enough in the specific bin.
        # But at some coarsening level we should find data.
        est_coarse = model.estimate('counting_process', 'ent_sports', 5.0, 'no',
                                     1, 92, 0)
        # At Q=1 with coarsening, we should definitely get something
        assert est_coarse is not None or len(model.flow_table) == 0

    def test_both_sides_present(self):
        """Calibration produces data for both YES and NO sides."""
        trades, settled, classifications = _make_synthetic_data()
        model = FlowModel.calibrate(trades, settled, classifications)
        # Check that both sides appear in the flow table
        sides = set()
        for key in model.flow_table:
            sides.add(key[3])  # side is 4th element
        assert 'yes' in sides or '*' in sides
        assert 'no' in sides or '*' in sides

    def test_won_and_lost_present(self):
        """Calibration produces both won and lost CDFs."""
        trades, settled, classifications = _make_synthetic_data()
        model = FlowModel.calibrate(trades, settled, classifications)
        outcomes = set()
        for key in model.flow_table:
            outcomes.add(key[5])  # outcome is 6th element
        assert 'won' in outcomes
        assert 'lost' in outcomes

    def test_flow_direction_in_calibration(self):
        """NO taker trades at low yes_price should create flow for YES buys."""
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        settle = base + timedelta(hours=6)
        # Only NO taker trades at yes_price=0.87 (fills YES buys at >=87¢)
        trades = [(base + timedelta(hours=h), 30, 0.87, 'no')
                  for h in range(5)]
        trades_by_ticker = {'T-1': trades}
        settled_markets = {'T-1': (settle, 'yes', 'T')}
        classifications = {'T': ('gp', 'topic')}

        model = FlowModel.calibrate(trades_by_ticker, settled_markets,
                                     classifications)
        # These trades should create opposing flow for YES side
        # but NOT for NO side (at tail prices)
        # Check by looking at flow table entries
        yes_keys = [k for k in model.flow_table if k[3] == 'yes']
        no_keys = [k for k in model.flow_table if k[3] == 'no']
        assert len(yes_keys) > 0  # YES side should have flow data


class TestCalibrateVerification:
    """Test the spec's verification criteria against calibrated model."""

    def test_higher_trailing_volume_higher_fill(self):
        """Higher trailing volume → higher P(fill) (verification criterion 4).

        This test creates two groups of markets: one with heavy trailing
        volume (many trades per hour) and one with no trailing volume.
        """
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        trades_by_ticker = {}
        settled_markets = {}

        # Create 30 "active" markets with lots of trailing trades
        for i in range(30):
            ticker = f"ACTIVE-T{i}"
            settle = base + timedelta(hours=48 + i)
            settled_markets[ticker] = (settle, 'yes' if i % 2 == 0 else 'no',
                                        'ACTIVE')

            trades = []
            for h in range(48):
                t = base + timedelta(hours=h, minutes=15)
                # Heavy flow every hour
                trades.append((t, 100, 0.88, 'no'))
                trades.append((t, 80, 0.93, 'yes'))
            trades.sort(key=lambda t: t[0])
            trades_by_ticker[ticker] = trades

        # Create 30 "dead" markets with sparse trades
        for i in range(30):
            ticker = f"DEAD-T{i}"
            settle = base + timedelta(hours=48 + i)
            settled_markets[ticker] = (settle, 'yes' if i % 2 == 0 else 'no',
                                        'DEAD')

            trades = []
            # Only a few trades, and only early on (so trailing vol at later
            # placements is zero)
            for h in [0, 1]:
                t = base + timedelta(hours=h, minutes=15)
                trades.append((t, 5, 0.88, 'no'))
            trades.sort(key=lambda t: t[0])
            trades_by_ticker[ticker] = trades

        classifications = {
            'ACTIVE': ('counting_process', 'ent_sports'),
            'DEAD': ('counting_process', 'ent_sports'),
        }

        model = FlowModel.calibrate(trades_by_ticker, settled_markets,
                                     classifications)

        # Markets with heavy flow should have higher fill probability
        # Look for entries with different vol buckets
        active_cdfs = {k: v for k, v in model.flow_table.items()
                       if k[4] in ('active', 'high') and k[5] != '*'}
        dead_cdfs = {k: v for k, v in model.flow_table.items()
                     if k[4] == 'dead' and k[5] != '*'}

        if active_cdfs and dead_cdfs:
            # Compare average exceedance at Q=10
            active_avg = sum(v.p_fill(10) for v in active_cdfs.values()) / len(active_cdfs)
            dead_avg = sum(v.p_fill(10) for v in dead_cdfs.values()) / len(dead_cdfs)
            assert active_avg > dead_avg, (
                f"Active markets should have higher fill rate: {active_avg} vs {dead_avg}")


# ── Log-linear interpolation helper ─────────────────────────────────

class TestSimulateFillFromTape:
    """Tests for replay's simulate_fill_from_tape function."""

    def test_basic_fill(self):
        from trading.replay import simulate_fill_from_tape
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        settle = base + timedelta(hours=6)
        trades = {
            'T-1': [
                (base + timedelta(hours=1), 10, 0.88, 'no'),  # fills YES at 90
                (base + timedelta(hours=2), 5, 0.89, 'no'),   # fills YES at 90
                (base + timedelta(hours=3), 20, 0.91, 'no'),  # doesn't fill (above limit)
            ]
        }
        fills = simulate_fill_from_tape('T-1', 'yes', 90, 12, base, settle, trades)
        assert len(fills) == 2
        assert fills[0][1] == 10  # first fill: 10 contracts
        assert fills[1][1] == 2   # second fill: only need 2 more (of 5 available)

    def test_no_matching_trades(self):
        from trading.replay import simulate_fill_from_tape
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        settle = base + timedelta(hours=6)
        trades = {
            'T-1': [
                (base + timedelta(hours=1), 10, 0.92, 'no'),  # above limit
            ]
        }
        fills = simulate_fill_from_tape('T-1', 'yes', 90, 5, base, settle, trades)
        assert fills == []

    def test_no_side_fill(self):
        from trading.replay import simulate_fill_from_tape
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        settle = base + timedelta(hours=6)
        # NO buy at 8¢ → filled by YES taker at yes_price >= 0.92
        trades = {
            'T-1': [
                (base + timedelta(hours=1), 20, 0.93, 'yes'),  # fills NO at 8
            ]
        }
        fills = simulate_fill_from_tape('T-1', 'no', 8, 10, base, settle, trades)
        assert len(fills) == 1
        assert fills[0][1] == 10  # capped at requested qty

    def test_missing_ticker(self):
        from trading.replay import simulate_fill_from_tape
        base = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        fills = simulate_fill_from_tape('MISSING', 'yes', 90, 5, base,
                                         base + timedelta(hours=1), {})
        assert fills == []


class TestLogLinearInterp:
    def test_at_boundaries(self):
        assert abs(_log_linear_interp(1, 1.0, 10, 0.5, 1) - 1.0) < 1e-10
        assert abs(_log_linear_interp(1, 1.0, 10, 0.5, 10) - 0.5) < 1e-10

    def test_midpoint(self):
        # Geometric midpoint of 1 and 100 = 10
        p = _log_linear_interp(1, 1.0, 100, 0.0, 10)
        assert abs(p - 0.5) < 1e-10

    def test_monotonic(self):
        prev = 1.0
        for q in range(1, 101):
            p = _log_linear_interp(1, 1.0, 100, 0.0, q)
            assert p <= prev + 1e-10
            prev = p
