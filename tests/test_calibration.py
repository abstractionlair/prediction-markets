"""Tests for research/calibration.py — pure computation functions."""

from calibration import (
    BucketStats,
    FavoriteStats,
    Observation,
    _dedup_by_source,
    _get_conditioning_price,
    bucket_observations,
    compute_event_rates,
    price_bucket,
    time_bucket,
)


# ─── price_bucket ──────────────────────────────────────────────────

class TestPriceBucket:
    def test_low_price(self):
        assert price_bucket(0.02) == (0.00, 0.05)

    def test_mid_price(self):
        assert price_bucket(0.50) == (0.50, 0.55)

    def test_tail_price(self):
        assert price_bucket(0.92) == (0.90, 0.95)

    def test_boundary_exact(self):
        # 0.05 should go into (0.05, 0.10), not (0.00, 0.05)
        assert price_bucket(0.05) == (0.05, 0.10)

    def test_out_of_range(self):
        assert price_bucket(1.5) is None
        assert price_bucket(-0.1) is None

    def test_just_below_1(self):
        assert price_bucket(0.99) == (0.95, 1.00)


# ─── time_bucket ───────────────────────────────────────────────────

class TestTimeBucket:
    def test_short_horizon(self):
        lo, hi, label = time_bucket(0.5)
        assert label == "0-1h"

    def test_medium_horizon(self):
        lo, hi, label = time_bucket(2.0)
        assert label == "1-3h"

    def test_long_horizon(self):
        lo, hi, label = time_bucket(48.0)
        assert label == "24h+"

    def test_boundary_exact_1h(self):
        lo, hi, label = time_bucket(1.0)
        assert label == "1-3h"

    def test_negative_hours(self):
        assert time_bucket(-1.0) is None


# ─── BucketStats ───────────────────────────────────────────────────

class TestBucketStats:
    def test_empty(self):
        bs = BucketStats()
        assert bs.n == 0
        assert bs.avg_price == 0
        assert bs.empirical_yes_rate == 0

    def test_single_observation(self):
        bs = BucketStats()
        bs.add(0.90, True)
        assert bs.n == 1
        assert bs.avg_price == 0.90
        assert bs.empirical_yes_rate == 1.0
        assert abs(bs.calibration_gap - (-0.10)) < 1e-10  # price (0.9) - rate (1.0)

    def test_multiple_observations(self):
        bs = BucketStats()
        bs.add(0.90, True)
        bs.add(0.90, True)
        bs.add(0.90, False)
        assert bs.n == 3
        assert abs(bs.avg_price - 0.90) < 1e-10
        assert abs(bs.empirical_yes_rate - 2/3) < 1e-10

    def test_calibration_gap_overpriced(self):
        # If price is 0.9 but only wins 80% → gap = 0.9 - 0.8 = +0.1 (overpriced)
        bs = BucketStats()
        for _ in range(8):
            bs.add(0.90, True)
        for _ in range(2):
            bs.add(0.90, False)
        assert bs.calibration_gap > 0


# ─── FavoriteStats ─────────────────────────────────────────────────

class TestFavoriteStats:
    def test_empty(self):
        fs = FavoriteStats()
        assert fs.n == 0
        assert fs.edge == 0

    def test_positive_edge(self):
        fs = FavoriteStats()
        for _ in range(93):
            fs.add(0.90, True)
        for _ in range(7):
            fs.add(0.90, False)
        assert abs(fs.avg_fav_price - 0.90) < 1e-10
        assert abs(fs.fav_win_rate - 0.93) < 1e-10
        assert abs(fs.edge - 0.03) < 1e-10

    def test_negative_edge(self):
        fs = FavoriteStats()
        for _ in range(85):
            fs.add(0.90, True)
        for _ in range(15):
            fs.add(0.90, False)
        assert fs.edge < 0


# ─── _get_conditioning_price ──────────────────────────────────────

def _make_obs(ticker, yes_price, result_yes, hours, series="KXTEST",
              gp="continuous_underlyer", topic="financial",
              yes_bid=None, yes_ask=None, trade_price=0.0):
    """Create a test observation.  If bid/ask not given, synthesize a 2¢ spread."""
    if yes_bid is None:
        yes_bid = yes_price - 0.01
    if yes_ask is None:
        yes_ask = yes_price + 0.01
    return Observation(
        ticker=ticker, yes_bid=yes_bid, yes_ask=yes_ask, yes_mid=yes_price,
        trade_price=trade_price, result_yes=result_yes,
        hours_to_settlement=hours, series=series, source="hourly_candle",
        generating_process=gp, topic=topic,
    )


class TestGetConditioningPrice:
    def test_mid(self):
        obs = _make_obs("T1", 0.90, True, 1.0, yes_bid=0.89, yes_ask=0.91)
        assert _get_conditioning_price(obs, 'mid') == 0.90

    def test_bid(self):
        obs = _make_obs("T1", 0.90, True, 1.0, yes_bid=0.89, yes_ask=0.91)
        assert _get_conditioning_price(obs, 'bid') == 0.89

    def test_ask(self):
        obs = _make_obs("T1", 0.90, True, 1.0, yes_bid=0.89, yes_ask=0.91)
        assert _get_conditioning_price(obs, 'ask') == 0.91

    def test_trade(self):
        obs = _make_obs("T1", 0.90, True, 1.0, trade_price=0.895)
        assert _get_conditioning_price(obs, 'trade') == 0.895

    def test_trade_unavailable(self):
        obs = _make_obs("T1", 0.90, True, 1.0, trade_price=0.0)
        assert _get_conditioning_price(obs, 'trade') is None

    def test_invalid_method(self):
        obs = _make_obs("T1", 0.90, True, 1.0)
        try:
            _get_conditioning_price(obs, 'bogus')
            assert False, "should have raised"
        except ValueError:
            pass

    def test_bid_ask_in_different_buckets(self):
        # bid=0.84 → bucket [0.80,0.85), ask=0.86 → bucket [0.85,0.90)
        obs = _make_obs("T1", 0.85, True, 1.0, yes_bid=0.84, yes_ask=0.86)
        assert price_bucket(_get_conditioning_price(obs, 'bid')) == (0.80, 0.85)
        assert price_bucket(_get_conditioning_price(obs, 'ask')) == (0.85, 0.90)


# ─── compute_event_rates ─────────────────────────────────────────

class TestComputeEventRates:
    def _make_obs_batch(self, n, gp="continuous_underlyer", topic="financial",
                        yes_rate=0.93, price=0.90):
        """Generate n observations at a given price with given YES rate."""
        obs = []
        for i in range(n):
            result_yes = i < int(n * yes_rate)
            obs.append(_make_obs(
                ticker=f"MKT{i}-T{i}",  # unique market per obs
                yes_price=price,
                result_yes=result_yes,
                hours=float(i % 100),
                gp=gp, topic=topic,
            ))
        return obs

    def test_returns_empty_for_insufficient_data(self):
        obs = self._make_obs_batch(30)  # below MIN_TOTAL_MARKETS (50)
        result = compute_event_rates(obs)
        assert result == []

    def test_single_time_bucket(self):
        obs = self._make_obs_batch(50)  # exactly MIN_TOTAL_MARKETS; 50 // 30 = 1
        result = compute_event_rates(obs)
        assert len(result) == 1
        assert result[0]['generating_process'] == 'continuous_underlyer'
        assert result[0]['topic'] == 'financial'
        assert result[0]['price_lo'] == 0.90  # price=0.90 → bucket [0.90, 0.95)

    def test_multiple_time_buckets(self):
        obs = self._make_obs_batch(300)  # 300 markets → 300 // 30 = 10
        result = compute_event_rates(obs)
        assert len(result) == 10

    def test_max_buckets_capped(self):
        obs = self._make_obs_batch(10000)
        result = compute_event_rates(obs)
        assert len(result) == 20  # MAX_BUCKETS

    def test_event_rate_computed(self):
        obs = self._make_obs_batch(50, yes_rate=0.80, price=0.90)
        result = compute_event_rates(obs)
        assert len(result) == 1
        # P(YES) should be ~0.80
        assert 0.75 < result[0]['event_rate'] < 0.85

    def test_smoothed_event_rate_exists(self):
        obs = self._make_obs_batch(300)
        result = compute_event_rates(obs)
        for bucket in result:
            assert 'smoothed_event_rate' in bucket

    def test_price_bucket_in_output(self):
        obs = self._make_obs_batch(50, price=0.92)
        result = compute_event_rates(obs)
        assert result[0]['price_lo'] == 0.90
        assert result[0]['price_hi'] == 0.95

    def test_different_prices_different_buckets(self):
        # 50 observations at 0.90 and 50 at 0.07 — should produce 2 price buckets
        obs_high = self._make_obs_batch(50, price=0.90)
        obs_low = self._make_obs_batch(50, price=0.07)
        # Give low-price obs different tickers
        for i, o in enumerate(obs_low):
            obs_low[i] = _make_obs(
                f"LOW{i}-T{i}", 0.07, o.result_yes, o.hours_to_settlement)
        result = compute_event_rates(obs_high + obs_low)
        price_buckets = {(r['price_lo'], r['price_hi']) for r in result}
        assert (0.90, 0.95) in price_buckets  # 0.90 → [0.90, 0.95)
        assert (0.05, 0.10) in price_buckets  # 0.07 → [0.05, 0.10)

    def test_bid_method_uses_bid_for_bucketing(self):
        # bid=0.84 → bucket [0.80,0.85), even though mid=0.90 → [0.85,0.90)
        obs = []
        for i in range(50):
            obs.append(_make_obs(
                f"MKT{i}-T{i}", 0.90, True, float(i % 100),
                yes_bid=0.84, yes_ask=0.96))
        result = compute_event_rates(obs, price_method='bid')
        assert len(result) == 1
        assert result[0]['price_lo'] == 0.80  # bucketed by bid, not mid

    def test_per_market_weighting(self):
        # 50 markets with 2 obs each — per-market dedup should average within market
        obs = []
        for i in range(50):
            obs.append(_make_obs(f"MKT{i}-T1", 0.90, True, float(i)))
            obs.append(_make_obs(f"MKT{i}-T1", 0.90, True, float(i + 50)))
        result = compute_event_rates(obs)
        assert len(result) == 1
        assert result[0]['n_markets'] == 50

    def test_separate_cells(self):
        obs_a = self._make_obs_batch(50, gp="continuous_underlyer", topic="financial")
        obs_b = self._make_obs_batch(50, gp="convergent_binary", topic="entertainment_sports")
        result = compute_event_rates(obs_a + obs_b)
        cells = {(r['generating_process'], r['topic']) for r in result}
        assert len(cells) == 2

    def test_output_fields(self):
        obs = self._make_obs_batch(50)
        result = compute_event_rates(obs)
        expected_fields = {
            'generating_process', 'topic', 'price_lo', 'price_hi',
            'bucket_index', 'hours_from', 'hours_to', 'n', 'n_markets',
            'event_rate', 'smoothed_event_rate', 'avg_observed_price',
            'se', 'price_method', 'source',
        }
        assert set(result[0].keys()) == expected_fields

    def test_price_method_stored_in_output(self):
        obs = self._make_obs_batch(50)
        for method in ('mid', 'bid', 'ask'):
            result = compute_event_rates(obs, price_method=method)
            assert result[0]['price_method'] == method

    def test_low_price_records_yes_rate_not_no_rate(self):
        # At YES=0.07, with 8% YES rate, the event_rate should be ~0.08 (P(YES)),
        # NOT 0.92. The calibration stores P(YES) at all price levels.
        obs = []
        for i in range(50):
            obs.append(_make_obs(
                f"MKT{i}-T{i}", 0.07, i < 4, float(i % 100)))
        result = compute_event_rates(obs)
        assert len(result) == 1
        assert result[0]['event_rate'] < 0.15  # P(YES) ≈ 8%, not 92%


# ─── bucket_observations (report helper, still uses favorite-space) ──

class TestBucketObservations:
    def test_high_tail_bucketed_as_favorite(self):
        obs = [_make_obs("MKT1-T1", 0.90, True, 2.0)]
        buckets = bucket_observations(obs)
        fs = buckets['process_fav']['continuous_underlyer']
        assert fs.n == 1
        assert abs(fs.avg_fav_price - 0.90) < 1e-10
        assert fs.fav_win_rate == 1.0

    def test_low_tail_inverted_to_favorite(self):
        obs = [_make_obs("MKT1-T1", 0.10, False, 2.0)]
        buckets = bucket_observations(obs)
        fs = buckets['process_fav']['continuous_underlyer']
        assert fs.n == 1
        assert abs(fs.avg_fav_price - 0.90) < 1e-10
        assert fs.fav_win_rate == 1.0

    def test_low_tail_loss(self):
        obs = [_make_obs("MKT1-T1", 0.10, True, 2.0)]
        buckets = bucket_observations(obs)
        fs = buckets['process_fav']['continuous_underlyer']
        assert fs.n == 1
        assert fs.fav_win_rate == 0.0

    def test_midrange_not_in_favorite_space(self):
        obs = [_make_obs("MKT1-T1", 0.50, True, 2.0)]
        buckets = bucket_observations(obs)
        assert buckets['process_fav']['continuous_underlyer'].n == 0
        pb = price_bucket(0.50)
        price_label = f"{pb[0]:.2f}-{pb[1]:.2f}"
        assert buckets['overall_price'][price_label].n == 1


# ─── _dedup_by_source ────────────────────────────────────────────

class TestDedupBySource:
    def _obs(self, ticker, source):
        return Observation(
            ticker=ticker, yes_bid=0.89, yes_ask=0.91, yes_mid=0.90,
            trade_price=0.0, result_yes=True, hours_to_settlement=1.0,
            series="KXTEST", source=source,
            generating_process="continuous_underlyer", topic="financial",
        )

    def test_hourly_preferred_over_snapshot(self):
        obs = [self._obs("MKT-T1", "hourly_candle"),
               self._obs("MKT-T1", "snapshot")]
        result = _dedup_by_source(obs)
        assert len(result) == 1
        assert result[0].source == "hourly_candle"

    def test_snapshot_preferred_over_daily(self):
        obs = [self._obs("MKT-T1", "snapshot"),
               self._obs("MKT-T1", "daily_candle")]
        result = _dedup_by_source(obs)
        assert len(result) == 1
        assert result[0].source == "snapshot"

    def test_different_tickers_kept(self):
        obs = [self._obs("MKT-T1", "hourly_candle"),
               self._obs("MKT-T2", "snapshot")]
        result = _dedup_by_source(obs)
        assert len(result) == 2

    def test_multiple_obs_from_best_source_kept(self):
        obs = [self._obs("MKT-T1", "hourly_candle"),
               self._obs("MKT-T1", "hourly_candle"),
               self._obs("MKT-T1", "snapshot")]
        result = _dedup_by_source(obs)
        assert len(result) == 2
        assert all(r.source == "hourly_candle" for r in result)
