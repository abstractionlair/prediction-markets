"""
Expanding-window calibration for out-of-sample replay.

Preloads all observations and fill data once, then provides day-by-day
recalibration using only data from markets settled before each date.
Avoids re-querying the database on each recalibration.
"""

from collections import defaultdict
from datetime import datetime, timezone

from cost_model import KALSHI_COSTS
from fill_model import FillModel, CandleData


class ExpandingEventRates:
    """Event rate calibration that recalibrates on demand.

    Preloads all observations once. On recalibrate(cutoff), filters to
    observations from markets settled before cutoff and rebuilds the
    (process, topic, price_bucket, time) -> P(YES) lookup.
    """

    # Match calibration.py constants
    PRICE_BUCKETS = [(i/20, (i+1)/20) for i in range(20)]
    MIN_MARKETS_PER_BUCKET = 30
    MAX_BUCKETS = 20
    MIN_TOTAL_MARKETS = 50

    def __init__(self, conn):
        self.observations = []  # (ticker, series, gp, topic, settled_at, yes_price, result_yes, hours)
        self.series_map = {}    # series -> (gp, topic)
        self.rates = {}         # (gp, topic, p_lo, p_hi) -> [(h_from, h_to, rate)]
        self._load(conn)

    def _load(self, conn):
        """Load all observations and classifications."""
        cur = conn.cursor()

        # Classifications
        cur.execute("""
            SELECT series_ticker, generating_process, topic
            FROM prediction_markets.market_classifications
            WHERE generating_process IS NOT NULL AND topic IS NOT NULL
        """)
        for series, gp, topic in cur:
            self.series_map[series] = (gp, topic)

        # All hourly candle observations joined to settlements
        # Load once — we'll filter by settled_at in Python
        print("  Loading event rate observations...", end="", flush=True)
        cur2 = conn.cursor("ew_obs")
        cur2.itersize = 100000
        cur2.execute("""
            SELECT hc.ticker,
                   split_part(hc.ticker, '-', 1) AS series,
                   sm.settled_at::timestamptz AS settled_at,
                   hc.yes_bid_close AS yes_bid,
                   hc.yes_ask_close AS yes_ask,
                   (hc.yes_bid_close + hc.yes_ask_close) / 2.0 AS yes_mid,
                   sm.result,
                   EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - hc.period_end)) / 3600.0
                       AS hours_to_settlement
            FROM prediction_markets.kalshi_hourly_candles hc
            JOIN prediction_markets.kalshi_settled_markets sm ON sm.ticker = hc.ticker
            WHERE sm.result IN ('yes', 'no')
              AND sm.settled_at != ''
              AND hc.period_end < sm.settled_at::timestamptz
              AND hc.yes_bid_close > 0 AND hc.yes_ask_close > 0
              AND (hc.yes_ask_close - hc.yes_bid_close) <= 0.10
        """)
        for row in cur2:
            ticker, series, settled_at, bid, ask, mid, result, hours = row
            if hours is None or hours < 0:
                continue
            mid_f = float(mid)
            if mid_f <= 0 or mid_f >= 1:
                continue
            gp_topic = self.series_map.get(series)
            if gp_topic is None:
                continue
            self.observations.append((
                ticker, series, gp_topic[0], gp_topic[1],
                settled_at,
                float(bid), float(ask), mid_f,
                result == 'yes', float(hours),
            ))
        cur2.close()
        cur.close()
        print(f" {len(self.observations):,} observations")

    def _price_bucket(self, p):
        for lo, hi in self.PRICE_BUCKETS:
            if lo <= p < hi:
                return (lo, hi)
        return None

    def recalibrate(self, cutoff_dt, price_method='mid'):
        """Rebuild lookup using only observations settled before cutoff."""
        # Filter and group
        cells = defaultdict(list)
        for (ticker, series, gp, topic, settled_at,
             bid, ask, mid, result_yes, hours) in self.observations:
            if settled_at >= cutoff_dt:
                continue
            if price_method == 'mid':
                price = mid
            elif price_method == 'bid':
                price = bid
            elif price_method == 'ask':
                price = ask
            else:
                price = mid
            pb = self._price_bucket(price)
            if pb is None:
                continue
            cells[(gp, topic, pb)].append((price, result_yes, hours, ticker))

        self.rates = {}
        for (gp, topic, (pb_lo, pb_hi)), points in cells.items():
            n_markets = len(set(p[3] for p in points))
            if n_markets < self.MIN_TOTAL_MARKETS:
                continue
            k = min(n_markets // self.MIN_MARKETS_PER_BUCKET, self.MAX_BUCKETS)
            k = max(k, 1)
            points.sort(key=lambda x: x[2])
            n_obs = len(points)
            bucket_size = n_obs / k

            buckets = []
            for i in range(k):
                start = int(round(i * bucket_size))
                end = int(round((i + 1) * bucket_size))
                bucket = points[start:end]
                h_from, h_to = bucket[0][2], bucket[-1][2]

                market_data = defaultdict(lambda: {'sum_yes': 0, 'n': 0})
                for _, ry, _, t in bucket:
                    md = market_data[t]
                    md['sum_yes'] += int(ry)
                    md['n'] += 1
                rates = [md['sum_yes'] / md['n'] for md in market_data.values()]
                event_rate = sum(rates) / len(rates)
                buckets.append((h_from, h_to, event_rate))

            # Smooth
            for i in range(len(buckets)):
                lo = max(0, i - 2)
                hi = min(len(buckets), i + 3)
                neighbors = buckets[lo:hi]
                buckets[i] = (buckets[i][0], buckets[i][1],
                              sum(b[2] for b in neighbors) / len(neighbors))

            self.rates[(gp, topic, pb_lo, pb_hi)] = buckets

    def get_event_rate(self, series, hours, price_dollars):
        cl = self.series_map.get(series)
        if cl is None:
            return None
        gp, topic = cl
        pb = self._price_bucket(price_dollars)
        if pb is None:
            return None
        buckets = self.rates.get((gp, topic, pb[0], pb[1]))
        if not buckets:
            return None
        for h_from, h_to, rate in buckets:
            if h_from <= hours <= h_to:
                return rate
        return None

    def get_classification(self, series):
        return self.series_map.get(series)


class ExpandingFillRates:
    """Fill rate calibration that recalibrates on demand.

    Preloads per-ticker candle + settlement data once. On recalibrate(cutoff),
    filters to tickers settled before cutoff and rebuilds fill rate lookup.
    """

    TIME_BREAKS = [1, 3, 6, 12, 24, 72, 168]

    def __init__(self, conn, n_price_steps=10):
        self.n_price_steps = n_price_steps
        self.ticker_data = {}   # ticker -> {series, gp, topic, settled_at, result, candles}
        self.rates = {}         # (gp, topic, time_bucket, side) -> [(rel, fill_won, fill_lost)]
        self._load(conn)

    def _load(self, conn):
        """Load all candle + settlement data."""
        cur = conn.cursor()

        # Classifications
        cur.execute("""
            SELECT series_ticker, generating_process, topic
            FROM prediction_markets.market_classifications
            WHERE generating_process IS NOT NULL AND topic IS NOT NULL
        """)
        classifications = {row[0]: (row[1], row[2]) for row in cur}

        # Settlements
        cur.execute("""
            SELECT ticker, event_ticker, result, settled_at
            FROM prediction_markets.kalshi_settled_markets
            WHERE result IN ('yes', 'no') AND settled_at != '' AND event_ticker != ''
        """)
        settled = {}
        for ticker, event, result, settled_at in cur:
            try:
                if isinstance(settled_at, str):
                    sdt = datetime.fromisoformat(settled_at.replace('Z', '+00:00'))
                else:
                    sdt = settled_at
                    if sdt.tzinfo is None:
                        sdt = sdt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            series = event.split('-')[0]
            if series not in classifications:
                continue
            gp, topic = classifications[series]
            settled[ticker] = {
                'series': series, 'gp': gp, 'topic': topic,
                'settled_at': sdt, 'result': result,
            }

        # Candles
        print("  Loading fill calibration candles...", end="", flush=True)
        cur2 = conn.cursor("ew_fill")
        cur2.itersize = 100000
        cur2.execute("""
            SELECT ticker, period_end,
                   yes_bid_close, yes_ask_close,
                   yes_bid_high, yes_ask_low,
                   COALESCE(volume, 0),
                   COALESCE(price_high, 0), COALESCE(price_low, 0)
            FROM prediction_markets.kalshi_hourly_candles
            WHERE yes_bid_close IS NOT NULL AND yes_ask_close IS NOT NULL
              AND yes_bid_close > 0 AND yes_ask_close > 0
            ORDER BY ticker, period_end
        """)
        ticker_candles = defaultdict(list)
        n = 0
        for (ticker, period_end, bid_close, ask_close,
             bid_high, ask_low, vol, p_high, p_low) in cur2:
            if ticker not in settled:
                continue
            ticker_candles[ticker].append({
                'period_end': period_end,
                'bid_cents': int(round(float(bid_close) * 100)),
                'ask_cents': int(round(float(ask_close) * 100)),
                'fill_candle': CandleData(
                    yes_bid_high=int(round(float(bid_high) * 100)),
                    yes_ask_low=int(round(float(ask_low) * 100)),
                    volume=vol,
                    price_high=int(round(float(p_high) * 100)) if p_high else 0,
                    price_low=int(round(float(p_low) * 100)) if p_low else 0,
                ),
            })
            n += 1
        cur2.close()
        cur.close()

        # Merge into ticker_data
        for ticker, candles in ticker_candles.items():
            md = settled[ticker]
            candles.sort(key=lambda c: c['period_end'])
            self.ticker_data[ticker] = {**md, 'candles': candles}

        print(f" {n:,} candles, {len(self.ticker_data):,} tickers")

    def _time_bucket(self, hours):
        for brk in self.TIME_BREAKS:
            if hours < brk:
                return f"<{brk}h"
        return f">{self.TIME_BREAKS[-1]}h"

    def recalibrate(self, cutoff_dt, min_per_cell=30):
        """Rebuild fill rate lookup using tickers settled before cutoff."""
        fm = FillModel(require_volume=False)
        rel_prices = [i / self.n_price_steps for i in range(self.n_price_steps + 1)]

        accum = defaultdict(lambda: {
            'filled_won': 0, 'filled_lost': 0,
            'unfilled_won': 0, 'unfilled_lost': 0,
        })

        for ticker, td in self.ticker_data.items():
            if td['settled_at'] >= cutoff_dt:
                continue
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
                    tb = self._time_bucket(hours)

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

        self.rates = {}
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

        # Sort each key's list by relative price
        for key in self.rates:
            self.rates[key].sort()

    def get_fill_rates(self, gp, topic, hours, relative_price, side):
        tb = self._time_bucket(hours)
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


class LegacyMarketView:
    """Adapter: wraps ExpandingEventRates + ExpandingFillRates to match MarketView interface.

    EVStrategy calls view.event_rate(), view.fill_rates(), etc.
    This adapter delegates to the legacy estimators so we can compare
    the two implementations head-to-head.
    """

    def __init__(self, event_rates: ExpandingEventRates, fill_rates: ExpandingFillRates,
                 costs=KALSHI_COSTS):
        self._event_rates = event_rates
        self._fill_rates = fill_rates
        self.costs = costs

    def recalibrate(self, cutoff_dt, recal_fills=False):
        """Recalibrate both estimators up to cutoff_dt."""
        self._event_rates.recalibrate(cutoff_dt)
        if recal_fills:
            self._fill_rates.recalibrate(cutoff_dt)

    def event_rate(self, series, hours_to_settlement,
                   observed_price_dollars=None, *,
                   bid_dollars=None, ask_dollars=None, trade_dollars=None):
        # Legacy only calibrates mid — compute mid from bid/ask if provided
        if observed_price_dollars is not None:
            price = observed_price_dollars
        elif bid_dollars is not None and ask_dollars is not None:
            price = (bid_dollars + ask_dollars) / 2.0
        else:
            return None
        rate = self._event_rates.get_event_rate(series, hours_to_settlement, price)
        if rate is None:
            return None
        return (rate, 0.0, 9999)  # legacy has no SE/n_markets tracking

    def fill_rates(self, gp, topic, hours_to_settlement, relative_price, side):
        return self._fill_rates.get_fill_rates(gp, topic, hours_to_settlement,
                                               relative_price, side)

    def classification(self, series):
        return self._event_rates.get_classification(series)

    def maker_fee(self, price_cents, contracts):
        return self.costs.maker_fee(price_cents, contracts)

    @property
    def stats(self):
        n_event_cells = len(self._event_rates.rates)
        n_fill_cells = len(self._fill_rates.rates)
        return f"legacy: {n_event_cells} event cells, {n_fill_cells} fill cells"
