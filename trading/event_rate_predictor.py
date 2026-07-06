"""
Event rate prediction model — gradient boosted trees.

Predicts P(YES) for a market given its current state: price, spread,
open interest, volume, hours to settlement, and market classification.

Replaces the binned EventRateEstimator for markets where additional
features (spread, OI, volume) improve calibration beyond price alone.

The key insight: the favorite-longshot bias varies by market efficiency.
At the same mid-price, a tight-spread high-OI market is better calibrated
than a wide-spread low-OI market. The GBT learns these interactions.
"""

import math

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

from fill_predictor import GP_ENCODING, TOPIC_ENCODING


FEATURE_NAMES = [
    'mid_price', 'spread', 'hours_to_settlement',
    'open_interest', 'volume',
    'generating_process', 'topic',
]


def _encode_features(obs_list):
    """Convert observation dicts to feature matrix.

    Returns (X, y) where X is float32 features and y is bool labels.
    """
    n = len(obs_list)
    X = np.zeros((n, len(FEATURE_NAMES)), dtype=np.float32)
    y = np.zeros(n, dtype=np.bool_)

    for i, obs in enumerate(obs_list):
        X[i, 0] = obs['mid_price']
        X[i, 1] = obs['spread']
        X[i, 2] = obs['hours_to_settlement']
        X[i, 3] = obs['open_interest']
        X[i, 4] = obs['volume']
        X[i, 5] = GP_ENCODING.get(obs['generating_process'], len(GP_ENCODING))
        X[i, 6] = TOPIC_ENCODING.get(obs['topic'], len(TOPIC_ENCODING))
        y[i] = obs['result_yes']

    return X, y


class EventRatePredictor:
    """Predicts P(YES) using gradient boosted trees.

    Uses market microstructure features (spread, OI, volume) alongside
    price and time to improve calibration over binned estimates.
    """

    def __init__(self, model):
        self.model = model

    def predict(self, mid_price, spread, hours_to_settlement,
                open_interest, volume, generating_process, topic):
        """Predict P(YES) for a single market.

        Args:
            mid_price: mid price in dollars (0.0-1.0)
            spread: bid-ask spread in dollars (e.g. 0.04)
            hours_to_settlement: hours until settlement
            open_interest: open interest (contracts)
            volume: trailing volume (contracts)
            generating_process: string
            topic: string

        Returns:
            float P(YES) in [0, 1]
        """
        X = np.array([[
            mid_price,
            spread,
            hours_to_settlement,
            open_interest,
            volume,
            GP_ENCODING.get(generating_process, len(GP_ENCODING)),
            TOPIC_ENCODING.get(topic, len(TOPIC_ENCODING)),
        ]], dtype=np.float32)

        return float(self.model.predict_proba(X)[0, 1])

    @classmethod
    def train(cls, observations, **gbt_kwargs):
        """Train from observation dicts.

        Each observation needs: mid_price, spread, hours_to_settlement,
        open_interest, volume, generating_process, topic, result_yes.
        """
        defaults = dict(
            n_estimators=300,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=100,
        )
        defaults.update(gbt_kwargs)

        X, y = _encode_features(observations)
        model = GradientBoostingClassifier(**defaults)
        model.fit(X, y)
        return cls(model)


def validate(predictor, test_observations, group_by=None):
    """Compare predicted P(YES) against actual outcomes.

    Returns dict of group_key → {n, pred_yes_rate, actual_yes_rate, gap}.
    """
    from collections import defaultdict
    results = defaultdict(lambda: {'n': 0, 'pred_sum': 0.0, 'actual_sum': 0})

    for obs in test_observations:
        pred = predictor.predict(
            obs['mid_price'], obs['spread'], obs['hours_to_settlement'],
            obs['open_interest'], obs['volume'],
            obs['generating_process'], obs['topic'])

        key = obs.get(group_by, 'ALL') if group_by else 'ALL'
        r = results[key]
        r['n'] += 1
        r['pred_sum'] += pred
        r['actual_sum'] += int(obs['result_yes'])

    out = {}
    for key, r in sorted(results.items(), key=lambda x: -x[1]['n']):
        n = r['n']
        out[key] = {
            'n': n,
            'pred_yes_rate': r['pred_sum'] / n,
            'actual_yes_rate': r['actual_sum'] / n,
            'gap': r['pred_sum'] / n - r['actual_sum'] / n,
        }
    return out


def validate_as_edge_model(predictor, test_observations, group_by=None):
    """Evaluate as an edge model: does predicted P(win) match actual win rate?

    For tail orders, we buy the favorite side. P(win) = max(P(YES), 1-P(YES)).
    The edge model's job is to identify when P(win) > price, so calibration
    of P(win) is what matters.

    Returns dict of group_key → {n, avg_p_win, actual_win_rate, gap, avg_price}.
    """
    from collections import defaultdict
    results = defaultdict(lambda: {'n': 0, 'p_win_sum': 0.0, 'win_sum': 0,
                                    'price_sum': 0.0})

    for obs in test_observations:
        pred = predictor.predict(
            obs['mid_price'], obs['spread'], obs['hours_to_settlement'],
            obs['open_interest'], obs['volume'],
            obs['generating_process'], obs['topic'])

        mid = obs['mid_price']
        # The strategy buys the favorite side
        if mid >= 0.5:
            p_win = pred
            won = obs['result_yes']
            price = mid
        else:
            p_win = 1.0 - pred
            won = not obs['result_yes']
            price = 1.0 - mid

        # Only evaluate tail markets (price >= 0.85)
        if price < 0.85:
            continue

        key = obs.get(group_by, 'ALL') if group_by else 'ALL'
        r = results[key]
        r['n'] += 1
        r['p_win_sum'] += p_win
        r['win_sum'] += int(won)
        r['price_sum'] += price

    out = {}
    for key, r in sorted(results.items(), key=lambda x: -x[1]['n']):
        n = r['n']
        if n == 0:
            continue
        out[key] = {
            'n': n,
            'avg_p_win': r['p_win_sum'] / n,
            'actual_win_rate': r['win_sum'] / n,
            'gap': r['p_win_sum'] / n - r['win_sum'] / n,
            'avg_price': r['price_sum'] / n,
        }
    return out
