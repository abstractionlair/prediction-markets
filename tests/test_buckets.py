"""Tests for trading/buckets.py — shared bucketing and smoothing."""

from trading.buckets import fill_time_bucket, price_bucket, smooth_time_buckets


class TestPriceBucket:
    def test_low(self):
        assert price_bucket(0.02) == (0.00, 0.05)

    def test_mid(self):
        assert price_bucket(0.50) == (0.50, 0.55)

    def test_high(self):
        assert price_bucket(0.92) == (0.90, 0.95)

    def test_boundary(self):
        assert price_bucket(0.05) == (0.05, 0.10)

    def test_out_of_range(self):
        assert price_bucket(1.5) is None
        assert price_bucket(-0.1) is None


class TestFillTimeBucket:
    def test_short(self):
        assert fill_time_bucket(0.5) == "<1h"

    def test_medium(self):
        assert fill_time_bucket(4.0) == "<6h"

    def test_long(self):
        assert fill_time_bucket(200) == ">168h"

    def test_boundary(self):
        # Exactly at break should go to next bucket
        assert fill_time_bucket(1.0) == "<3h"


class TestSmoothTimeBuckets:
    def test_single_bucket(self):
        buckets = [{'event_rate': 0.90, 'n_markets': 100}]
        smooth_time_buckets(buckets)
        assert buckets[0]['smoothed_event_rate'] == 0.90

    def test_uniform_unchanged(self):
        buckets = [{'event_rate': 0.90, 'n_markets': 100} for _ in range(5)]
        smooth_time_buckets(buckets)
        for b in buckets:
            assert abs(b['smoothed_event_rate'] - 0.90) < 1e-10

    def test_outlier_smoothed(self):
        # Middle bucket is an outlier
        buckets = [
            {'event_rate': 0.90, 'n_markets': 100},
            {'event_rate': 0.90, 'n_markets': 100},
            {'event_rate': 0.50, 'n_markets': 100},  # outlier
            {'event_rate': 0.90, 'n_markets': 100},
            {'event_rate': 0.90, 'n_markets': 100},
        ]
        smooth_time_buckets(buckets)
        # Outlier should be pulled toward neighbors
        assert buckets[2]['smoothed_event_rate'] > 0.70

    def test_weight_matters(self):
        buckets = [
            {'event_rate': 0.90, 'n_markets': 1000},
            {'event_rate': 0.50, 'n_markets': 10},
        ]
        smooth_time_buckets(buckets)
        # First bucket: weighted avg of 0.90*1000 + 0.50*10 = 905/1010 ≈ 0.896
        assert abs(buckets[0]['smoothed_event_rate'] - 0.896) < 0.01

    def test_custom_keys(self):
        buckets = [
            {'rate': 0.80, 'weight': 50},
            {'rate': 0.90, 'weight': 50},
        ]
        smooth_time_buckets(buckets, value_key='rate', weight_key='weight')
        assert 'smoothed_rate' in buckets[0]
