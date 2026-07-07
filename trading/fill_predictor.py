"""
Fill prediction model — gradient boosted trees.

Predicts P(fill | pays_off) and P(fill | ¬pays_off) for a proposed
resting limit order, given market context at the time of placement.

Two GBT models trained on simulator output:
  - won_model:  P(fill | pays_off)    trained on observations where outcome=won
  - lost_model: P(fill | ¬pays_off)   trained on observations where outcome=lost

See docs/design/fill-prediction-model-spec.md for full specification.
"""

import bisect
import pickle
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

from trading.flow_model import compute_opposing_flow


# ── Feature engineering ──────────────────────────────────────────────

def _relative_price(side, limit_price_cents, bid_cents, ask_cents):
    """Compute relative price: 0.0 at bid, 1.0 at ask.

    For NO side, the book is flipped so relative_price always means
    "how aggressive is this order" regardless of side.
    """
    if side == 'no':
        bid_cents, ask_cents = 100 - ask_cents, 100 - bid_cents
    spread = ask_cents - bid_cents
    if spread <= 0:
        return 0.5  # degenerate spread
    return max(0.0, min(1.0, (limit_price_cents - bid_cents) / spread))


# ── Observation generation (simulator) ───────────────────────────────

@dataclass
class SimulationConfig:
    """Controls how virtual orders are generated for calibration."""
    # Relative price steps to simulate at
    rel_price_steps: tuple = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    # Quantity steps to simulate
    quantity_steps: tuple = (1, 2, 4, 8, 15, 30)
    # Queue depth multiplier: fraction of OI treated as resting queue ahead of us.
    # Calibrated from 76 real orders (2026-04-02): alpha=0.3 gives +3% gap,
    # alpha=0.5 gives -7%. The queue-unaware simulator (alpha=0) overpredicts
    # fills by ~32pp because it ignores that other resting orders absorb flow.
    queue_alpha: float = 0.3

    DEFAULT = None  # set below

SimulationConfig.DEFAULT = SimulationConfig()


def generate_observations(trades_by_ticker, settled_markets, classifications,
                          candle_data, as_of=None,
                          config=None):
    """Generate training observations from the simulator.

    For each settled market, at each hourly candle snapshot, simulate
    virtual orders at various (side, relative_price, quantity) and record
    whether opposing flow >= quantity.

    Args:
        trades_by_ticker: ticker → [(created_time, count, yes_price, taker_side)]
        settled_markets: ticker → (settled_at, result, event_ticker)
        classifications: series_ticker → (gp, topic)
        candle_data: ticker → [(period_end, bid_cents, ask_cents, volume, open_interest)]
            Sorted by period_end. bid/ask in cents. volume is hourly candle volume.
        as_of: temporal boundary — only markets settled before this.
        config: SimulationConfig for price/quantity steps.

    Returns:
        List of dicts, each an observation with features + label:
        {
            'generating_process', 'topic',
            'relative_price', 'quantity', 'spread',
            'hours_to_settlement', 'trailing_volume_24h', 'open_interest',
            'filled': bool,  # flow >= quantity
            'outcome': 'won' or 'lost',
        }
    """
    if config is None:
        config = SimulationConfig.DEFAULT

    observations = []

    for ticker, candles in candle_data.items():
        settlement = settled_markets.get(ticker)
        if settlement is None:
            continue
        settled_at, result, event_ticker = settlement

        if as_of is not None and settled_at >= as_of:
            continue

        series = event_ticker.split('-')[0] if event_ticker else ''
        cl = classifications.get(series)
        if cl is None:
            continue
        gp, topic = cl

        trades = trades_by_ticker.get(ticker)
        if not trades:
            continue

        trade_times = [t[0] for t in trades]
        settle_idx = bisect.bisect_left(trade_times, settled_at)
        if settle_idx == 0:
            continue

        # Trailing volume prefix sum
        vol_prefix = [0] * (len(trades) + 1)
        for i in range(len(trades)):
            vol_prefix[i + 1] = vol_prefix[i] + trades[i][1]

        for period_end, bid_cents, ask_cents, candle_vol, oi in candles:
            if period_end >= settled_at:
                continue

            hours = (settled_at - period_end).total_seconds() / 3600.0
            if hours <= 0:
                continue

            spread = ask_cents - bid_cents
            if spread <= 0:
                continue

            # Trailing 24h volume from trade tape
            cutoff_24h = period_end - timedelta(hours=24)
            vol_start = bisect.bisect_left(trade_times, cutoff_24h)
            vol_end = bisect.bisect_left(trade_times, period_end)
            trail_vol = int(vol_prefix[vol_end] - vol_prefix[vol_start])

            # Start index for flow computation
            start_idx = bisect.bisect_right(trade_times, period_end)

            for side in ('yes', 'no'):
                won = (result == side)
                outcome = 'won' if won else 'lost'

                # Flip book for NO side
                if side == 'no':
                    side_bid, side_ask = 100 - ask_cents, 100 - bid_cents
                else:
                    side_bid, side_ask = bid_cents, ask_cents
                side_spread = side_ask - side_bid

                for rel in config.rel_price_steps:
                    limit = side_bid + int(round(rel * side_spread))
                    limit = max(side_bid, min(side_ask, limit))

                    # Compute opposing flow from placement to settlement
                    cum_flow = 0
                    for i in range(start_idx, settle_idx):
                        _, t_count, t_yes_price, t_taker_side = trades[i]
                        cum_flow += compute_opposing_flow(
                            side, limit, t_yes_price, t_taker_side, t_count)

                    # Queue depth: fraction of OI assumed to be resting
                    # orders ahead of us. We fill when opposing flow
                    # drains the queue and reaches our position.
                    queue_depth = oi * config.queue_alpha

                    for qty in config.quantity_steps:
                        observations.append({
                            'generating_process': gp,
                            'topic': topic,
                            'relative_price': rel,
                            'quantity': qty,
                            'spread': spread,
                            'hours_to_settlement': hours,
                            'trailing_volume_24h': trail_vol,
                            'open_interest': oi,
                            'filled': cum_flow >= queue_depth + qty,
                            'outcome': outcome,
                        })

    return observations


# ── Model ────────────────────────────────────────────────────────────

# Categorical features encoded as integers
GP_ENCODING = {
    'continuous_underlyer': 0,
    'convergent_binary': 1,
    'counting_process': 2,
    'scheduled_release': 3,
    'hazard_process': 4,
}

TOPIC_ENCODING = {
    'financial': 0,
    'entertainment_sports': 1,
    'weather_climate': 2,
    'government_policy': 3,
    'science_technology': 4,
    'crypto': 5,
    'other': 6,
}


def _encode_features(obs_list):
    """Convert observation dicts to feature matrix.

    Returns (X, y) where X is a numpy array and y is a boolean array.
    """
    n = len(obs_list)
    # Features: relative_price, quantity, spread, hours_to_settlement,
    #           trailing_volume_24h, open_interest, gp_encoded, topic_encoded
    X = np.zeros((n, 8), dtype=np.float32)
    y = np.zeros(n, dtype=np.bool_)

    for i, obs in enumerate(obs_list):
        X[i, 0] = obs['relative_price']
        X[i, 1] = obs['quantity']
        X[i, 2] = obs['spread']
        X[i, 3] = obs['hours_to_settlement']
        X[i, 4] = obs['trailing_volume_24h']
        X[i, 5] = obs['open_interest']
        X[i, 6] = GP_ENCODING.get(obs['generating_process'], len(GP_ENCODING))
        X[i, 7] = TOPIC_ENCODING.get(obs['topic'], len(TOPIC_ENCODING))
        y[i] = obs['filled']

    return X, y


FEATURE_NAMES = [
    'relative_price', 'quantity', 'spread', 'hours_to_settlement',
    'trailing_volume_24h', 'open_interest', 'generating_process', 'topic',
]


@dataclass
class FillEstimate:
    """Fill probability estimates for a proposed order."""
    p_fill_won: float     # P(fill | pays_off)
    p_fill_lost: float    # P(fill | ¬pays_off)


class FillPredictor:
    """Predicts fill probability using gradient boosted trees.

    Two models: one for P(fill | pays_off), one for P(fill | ¬pays_off).
    """

    def __init__(self, won_model, lost_model):
        self.won_model = won_model
        self.lost_model = lost_model

    def estimate(self, side, limit_price_cents, quantity,
                 bid_cents, ask_cents, hours_to_settlement,
                 generating_process, topic,
                 trailing_volume_24h, open_interest):
        """Predict fill probability for a proposed resting order.

        Returns FillEstimate, or None if inputs are degenerate.
        """
        spread = ask_cents - bid_cents
        if spread <= 0:
            return None

        rel_price = _relative_price(side, limit_price_cents, bid_cents, ask_cents)

        X = np.array([[
            rel_price,
            quantity,
            spread,
            hours_to_settlement,
            trailing_volume_24h,
            open_interest,
            GP_ENCODING.get(generating_process, len(GP_ENCODING)),
            TOPIC_ENCODING.get(topic, len(TOPIC_ENCODING)),
        ]], dtype=np.float32)

        p_won = self.won_model.predict_proba(X)[0, 1]
        p_lost = self.lost_model.predict_proba(X)[0, 1]

        return FillEstimate(p_fill_won=float(p_won), p_fill_lost=float(p_lost))

    @classmethod
    def train(cls, observations, **gbt_kwargs):
        """Train from simulator observations.

        Args:
            observations: list of dicts from generate_observations()
            gbt_kwargs: passed to GradientBoostingClassifier

        Returns:
            FillPredictor instance.
        """
        won_obs = [o for o in observations if o['outcome'] == 'won']
        lost_obs = [o for o in observations if o['outcome'] == 'lost']

        defaults = dict(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            min_samples_leaf=50,
        )
        defaults.update(gbt_kwargs)

        X_won, y_won = _encode_features(won_obs)
        X_lost, y_lost = _encode_features(lost_obs)

        won_model = GradientBoostingClassifier(**defaults)
        won_model.fit(X_won, y_won)

        lost_model = GradientBoostingClassifier(**defaults)
        lost_model.fit(X_lost, y_lost)

        return cls(won_model, lost_model)

    def save(self, path):
        """Save trained models to disk.

        Uses pickle — only load files you created yourself.
        """
        with open(path, 'wb') as f:
            pickle.dump({'won_model': self.won_model,
                         'lost_model': self.lost_model}, f)

    @classmethod
    def load(cls, path):
        """Load trained models from disk.

        Uses pickle — only load files you trust.
        """
        with open(path, 'rb') as f:
            d = pickle.load(f)  # noqa: S301 — trusted local files only
        return cls(d['won_model'], d['lost_model'])


# ── Validation ───────────────────────────────────────────────────────

def validate(model, test_observations, group_by=None):
    """Compare model predictions against simulator on test data.

    Args:
        model: FillPredictor instance
        test_observations: list of dicts from generate_observations()
        group_by: optional key name to group results by (e.g. 'generating_process')

    Returns:
        dict of group_key → {n, tape_fill_rate, pred_fill_rate, gap}
    """
    results = defaultdict(lambda: {'n': 0, 'tape_fills': 0, 'pred_sum': 0.0})

    for obs in test_observations:
        X = np.array([[
            obs['relative_price'],
            obs['quantity'],
            obs['spread'],
            obs['hours_to_settlement'],
            obs['trailing_volume_24h'],
            obs['open_interest'],
            GP_ENCODING.get(obs['generating_process'], len(GP_ENCODING)),
            TOPIC_ENCODING.get(obs['topic'], len(TOPIC_ENCODING)),
        ]], dtype=np.float32)

        if obs['outcome'] == 'won':
            pred = model.won_model.predict_proba(X)[0, 1]
        else:
            pred = model.lost_model.predict_proba(X)[0, 1]

        key = obs.get(group_by, 'ALL') if group_by else 'ALL'
        r = results[key]
        r['n'] += 1
        r['tape_fills'] += int(obs['filled'])
        r['pred_sum'] += pred

    out = {}
    for key, r in sorted(results.items(), key=lambda x: -x[1]['n']):
        n = r['n']
        tape = r['tape_fills'] / n
        pred = r['pred_sum'] / n
        out[key] = {'n': n, 'tape_fill_rate': tape,
                    'pred_fill_rate': pred, 'gap': tape - pred}

    return out


def print_validation(model, test_observations):
    """Print validation summary across multiple groupings."""
    print(f"\n{'='*70}")
    print(f"FILL PREDICTOR VALIDATION ({len(test_observations)} observations)")
    print(f"{'='*70}")

    def _print_group(title, results):
        print(f"\n  --- {title} ---")
        for key, r in results.items():
            print(f"  {str(key):35s}  n={r['n']:7d}  "
                  f"tape={r['tape_fill_rate']:5.1%}  "
                  f"pred={r['pred_fill_rate']:5.1%}  "
                  f"gap={r['gap']:+5.1%}")

    # Overall + by outcome
    _print_group("Overall", validate(model, test_observations))
    won = [o for o in test_observations if o['outcome'] == 'won']
    lost = [o for o in test_observations if o['outcome'] == 'lost']
    _print_group("Won", validate(model, won))
    _print_group("Lost", validate(model, lost))

    # By category
    _print_group("By category", validate(model, test_observations,
                                          group_by='generating_process'))

    # By category × outcome
    _print_group("By category (won)", validate(model, won,
                                                group_by='generating_process'))
    _print_group("By category (lost)", validate(model, lost,
                                                 group_by='generating_process'))

    # By relative_price × outcome
    for obs in test_observations:
        obs['_rel_bucket'] = round(obs['relative_price'], 1)
    for obs in won:
        obs['_rel_bucket'] = round(obs['relative_price'], 1)
    for obs in lost:
        obs['_rel_bucket'] = round(obs['relative_price'], 1)
    _print_group("By relative_price", validate(model, test_observations,
                                                group_by='_rel_bucket'))
    _print_group("By relative_price (won)", validate(model, won,
                                                      group_by='_rel_bucket'))
    _print_group("By relative_price (lost)", validate(model, lost,
                                                       group_by='_rel_bucket'))

    # By quantity × outcome
    _print_group("By quantity", validate(model, test_observations,
                                          group_by='quantity'))
    _print_group("By quantity (won)", validate(model, won,
                                                group_by='quantity'))
    _print_group("By quantity (lost)", validate(model, lost,
                                                 group_by='quantity'))
