"""
Fill rate estimator.

Computes and serves P(fill|won) and P(fill|lost) per
(generating_process, topic, time_bucket, relative_price, side)
from pre-filtered ticker data. Has no database access.

The temporal boundary is enforced by the caller (MarketView), which filters
ticker data before passing it here.
"""

from collections import defaultdict
from datetime import timezone

from trading.buckets import fill_time_bucket
from trading.fill_model import FillModel


class FillRateEstimator:
    """Computes conditional fill rates from historical candle data.

    Usage:
        estimator = FillRateEstimator()
        estimator.calibrate(ticker_data, n_price_steps=10)
        p_fill_won, p_fill_lost = estimator.get_fill_rates(gp, topic, hours, rel, side)
    """

    def __init__(self):
        # (gp, topic, time_bucket, side) -> [(rel_price, fill_won, fill_lost)]
        self.rates = {}

    def calibrate(self, ticker_data, n_price_steps=10, min_per_cell=30):
        """Calibrate from pre-filtered ticker data.

        ticker_data: dict of ticker -> {
            gp, topic, settled_at, result,
            candles: [{period_end, bid_cents, ask_cents, fill_candle}, ...]
        }

        ticker_data is pre-filtered by the caller — only contains tickers
        that settled before the temporal boundary.
        """
        self.rates = {}
        fm = FillModel(require_volume=False)
        rel_prices = [i / n_price_steps for i in range(n_price_steps + 1)]

        accum = defaultdict(lambda: {
            'filled_won': 0, 'filled_lost': 0,
            'unfilled_won': 0, 'unfilled_lost': 0,
        })

        for ticker, td in ticker_data.items():
            candles = td['candles']
            settled_at = td['settled_at']

            for side in ('yes', 'no'):
                won = (td['result'] == side)
                for obs_idx, obs in enumerate(candles):
                    pe = obs['period_end']
                    if pe.tzinfo is None:
                        pe = pe.replace(tzinfo=timezone.utc)
                    if pe >= settled_at:
                        continue
                    hours = (settled_at - pe).total_seconds() / 3600
                    if hours <= 0:
                        continue
                    tb = fill_time_bucket(hours)

                    bid = obs['bid_cents']
                    ask = obs['ask_cents']
                    if side == 'no':
                        bid, ask = 100 - ask, 100 - bid
                    spread = ask - bid
                    if spread <= 0:
                        continue

                    remaining = [c['fill_candle'] for c in candles[obs_idx + 1:]
                                 if c['period_end'] < settled_at]
                    if not remaining:
                        continue

                    for rel in rel_prices:
                        limit = bid + int(round(rel * spread))
                        limit = max(bid, min(ask, limit))
                        filled = any(fm.check_fill(side, limit, 1, fc) > 0
                                     for fc in remaining)
                        key = (td['gp'], td['topic'], tb, side, rel)
                        cell = accum[key]
                        if filled:
                            cell['filled_won' if won else 'filled_lost'] += 1
                        else:
                            cell['unfilled_won' if won else 'unfilled_lost'] += 1

        # Build lookup
        for (gp, topic, tb, side, rel), cell in accum.items():
            total_won = cell['filled_won'] + cell['unfilled_won']
            total_lost = cell['filled_lost'] + cell['unfilled_lost']
            total = total_won + total_lost
            if total < min_per_cell:
                continue
            fr_won = cell['filled_won'] / total_won if total_won > 0 else 0
            fr_lost = cell['filled_lost'] / total_lost if total_lost > 0 else 0
            key = (gp, topic, tb, side)
            if key not in self.rates:
                self.rates[key] = []
            self.rates[key].append((rel, fr_won, fr_lost))

        for key in self.rates:
            self.rates[key].sort()

    def get_fill_rates(self, gp, topic, hours_to_settlement, relative_price, side):
        """Return (P(fill|won), P(fill|lost)) with linear interpolation.

        Returns None if no data for this cell.
        """
        tb = fill_time_bucket(hours_to_settlement)
        key = (gp, topic, tb, side)
        points = self.rates.get(key)
        if not points:
            return None

        below = above = None
        for rel, fr_won, fr_lost in points:
            if rel <= relative_price:
                below = (rel, fr_won, fr_lost)
            if rel >= relative_price and above is None:
                above = (rel, fr_won, fr_lost)

        if below is None and above is None:
            return None
        if below is None:
            return (above[1], above[2])
        if above is None:
            return (below[1], below[2])
        if below[0] == above[0]:
            return (below[1], below[2])

        t = (relative_price - below[0]) / (above[0] - below[0])
        return (below[1] + t * (above[1] - below[1]),
                below[2] + t * (above[2] - below[2]))
