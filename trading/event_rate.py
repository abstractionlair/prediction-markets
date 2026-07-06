"""
Event rate estimator.

Computes and serves P(YES | generating_process, topic, price_bucket, time_bucket)
from pre-filtered observations. Has no database access and no knowledge of
temporal boundaries — it calibrates on whatever observations it's given.

The temporal boundary is enforced by the caller (MarketView), which filters
observations before passing them here.

Multi-method calibration: calibrates independently for each price method
(bid, mid, ask, trade), then combines estimates at lookup time. Each method
determines its own bucket membership — an observation with bid=88, ask=92
contributes to the [85,90) bucket for bid calibration and the [90,95) bucket
for ask calibration.
"""

import math
from collections import defaultdict

from buckets import price_bucket, smooth_time_buckets

# Adaptive bucketing parameters — thresholds in distinct markets.
MIN_MARKETS_PER_BUCKET = 30
MAX_BUCKETS = 20
MIN_TOTAL_MARKETS = 50

PRICE_METHODS = ('bid', 'mid', 'ask', 'trade')


class EventRateEstimator:
    """Computes P(YES) from historical observations.

    Usage:
        estimator = EventRateEstimator()
        estimator.calibrate(observations)
        p_yes = estimator.get_event_rate(series, hours,
                    bid_dollars=0.88, ask_dollars=0.92)
    """

    def __init__(self):
        # method -> {(gp, topic, price_lo, price_hi): [(h_from, h_to, rate)]}
        self.rates = {}
        self.series_map = {}  # series -> (gp, topic)

    def set_classifications(self, series_map):
        """Set the series -> (generating_process, topic) mapping."""
        self.series_map = dict(series_map)

    def calibrate(self, observations, price_method=None):
        """Calibrate from a list of observations.

        If price_method is None (default), calibrates all methods.
        If a specific method is given, calibrates only that method
        (backward-compatible with single-method callers).

        Each observation must have: ticker, generating_process, topic,
        yes_bid, yes_ask, yes_mid, trade_price, result_yes, hours_to_settlement.
        """
        methods = PRICE_METHODS if price_method is None else (price_method,)
        for method in methods:
            self._calibrate_method(observations, method)

    def _calibrate_method(self, observations, method):
        """Calibrate a single price method."""
        # Group by (process, topic, price_bucket)
        cells = defaultdict(list)
        for obs in observations:
            price = self._get_price(obs, method)
            if price is None:
                continue
            pb = price_bucket(price)
            if pb is None:
                continue
            gp = obs.generating_process
            topic = obs.topic
            cells[(gp, topic, pb)].append(
                (price, obs.result_yes, obs.hours_to_settlement, obs.ticker)
            )

        method_rates = {}

        for (gp, topic, (pb_lo, pb_hi)), points in cells.items():
            n_markets = len(set(p[3] for p in points))
            if n_markets < MIN_TOTAL_MARKETS:
                continue

            k = min(n_markets // MIN_MARKETS_PER_BUCKET, MAX_BUCKETS)
            k = max(k, 1)

            points.sort(key=lambda x: x[2])  # sort by hours
            n_obs = len(points)
            bucket_size = n_obs / k

            raw_buckets = []
            for i in range(k):
                start = int(round(i * bucket_size))
                end = int(round((i + 1) * bucket_size))
                bucket = points[start:end]
                h_from, h_to = bucket[0][2], bucket[-1][2]

                # Per-market dedup: equal weight per market
                market_data = defaultdict(lambda: {'sum_yes': 0, 'n': 0})
                for _, result_yes, _, ticker in bucket:
                    md = market_data[ticker]
                    md['sum_yes'] += int(result_yes)
                    md['n'] += 1

                n_mkts = len(market_data)
                market_rates = [md['sum_yes'] / md['n'] for md in market_data.values()]
                event_rate = sum(market_rates) / n_mkts
                se = math.sqrt(event_rate * (1 - event_rate) / n_mkts) if n_mkts > 0 else 0

                raw_buckets.append({
                    'hours_from': h_from,
                    'hours_to': h_to,
                    'event_rate': event_rate,
                    'n_markets': n_mkts,
                    'se': se,
                })

            smooth_time_buckets(raw_buckets, value_key='event_rate',
                                weight_key='n_markets')

            method_rates[(gp, topic, pb_lo, pb_hi)] = [
                (b['hours_from'], b['hours_to'], b['smoothed_event_rate'],
                 b['se'], b['n_markets'])
                for b in raw_buckets
            ]

        self.rates[method] = method_rates

    def get_event_rate(self, series, hours_to_settlement,
                       observed_price_dollars=None,
                       bid_dollars=None, ask_dollars=None,
                       trade_dollars=None):
        """Look up P(YES), combining all calibrated price methods.

        Accepts either:
        - observed_price_dollars (backward-compatible: treated as mid)
        - bid_dollars + ask_dollars (+ optional trade_dollars)

        When multiple methods return estimates, averages them.
        """
        classification = self.series_map.get(series)
        if classification is None:
            return None
        gp, topic = classification

        # Build price lookups for each method
        if bid_dollars is not None and ask_dollars is not None:
            mid = (bid_dollars + ask_dollars) / 2.0
            lookups = [
                ('bid', bid_dollars),
                ('mid', mid),
                ('ask', ask_dollars),
            ]
            if trade_dollars is not None and trade_dollars > 0:
                lookups.append(('trade', trade_dollars))
        elif observed_price_dollars is not None:
            # Backward-compatible: single price treated as mid
            lookups = [('mid', observed_price_dollars)]
        else:
            return None

        estimates = []
        for method, price in lookups:
            result = self._lookup_single(method, gp, topic,
                                         hours_to_settlement, price)
            if result is not None:
                estimates.append(result)

        if not estimates:
            return None
        avg_rate = sum(r for r, _, _ in estimates) / len(estimates)
        avg_se = sum(se for _, se, _ in estimates) / len(estimates)
        min_n = min(n for _, _, n in estimates)
        return (avg_rate, avg_se, min_n)

    def _lookup_single(self, method, gp, topic, hours, price_dollars):
        """Look up (rate, se, n_markets) for a single price method, or None."""
        method_rates = self.rates.get(method)
        if not method_rates:
            return None
        pb = price_bucket(price_dollars)
        if pb is None:
            return None
        buckets = method_rates.get((gp, topic, pb[0], pb[1]))
        if not buckets:
            return None
        for h_from, h_to, rate, se, n_markets in buckets:
            if h_from <= hours <= h_to:
                return (rate, se, n_markets)
        return None

    def get_classification(self, series):
        """Return (generating_process, topic) for a series, or None."""
        return self.series_map.get(series)

    @staticmethod
    def _get_price(obs, price_method):
        if price_method == 'mid':
            return obs.yes_mid
        elif price_method == 'bid':
            return obs.yes_bid
        elif price_method == 'ask':
            return obs.yes_ask
        elif price_method == 'trade':
            return obs.trade_price if obs.trade_price > 0 else None
        return None
