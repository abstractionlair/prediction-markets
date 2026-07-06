#!/usr/bin/env python3
"""
FLB calibration analysis.

Computes per-series calibration gaps from settled market outcomes joined
to price observations. Uses source-priority deduplication: for each market
ticker, observations come from exactly one source (hourly candles preferred,
then snapshots, then daily candles). This avoids pseudo-replication from
overlapping sources.

The key output is: for a given (series, price bucket, time-to-settlement
bucket), what is the empirical YES rate, and how does it compare to the
price? This tells us whether the market overprices or underprices at that
level.

For our FLB strategy (buy the favorite at 85-97c), we also compute
"favorite-space" edge: observations from both tails (YES=85-97c and
YES=3-15c) are converted to the favorite's perspective and combined.

Edges are computed for multiple price methods:
  - mid:   (bid + ask) / 2 — theoretical midpoint
  - bid:   bid price — price if we rest an order (most favorable entry)
  - ask:   ask price — price if we cross the spread (least favorable entry)
  - trade: last trade price — actual clearing price (where available)

Usage:
    python research/calibration.py                  # full analysis, write to stdout
    python research/calibration.py --series KXNBA*  # filter series by prefix
    python research/calibration.py --out results.md # write markdown to file
    python research/calibration.py --store          # compute adaptive edges and store to DB
"""

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass

import os

import psycopg2


def get_pg_dsn() -> str:
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if not dsn:
        raise RuntimeError("CLAUDE_HUB_PG_DSN environment variable not set")
    return dsn

# ── Configuration ────────────────────────────────────────────────────

MAX_SPREAD = 0.10  # 10c — captures 94-99% of volume across all categories

# Price bucket boundaries (YES price, in dollars)
PRICE_BUCKETS = [
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
    (0.20, 0.25), (0.25, 0.30), (0.30, 0.35), (0.35, 0.40),
    (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60),
    (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80),
    (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.00),
]

# Time-to-settlement bucket boundaries (hours)
TIME_BUCKETS = [
    (0, 1, "0-1h"),
    (1, 3, "1-3h"),
    (3, 6, "3-6h"),
    (6, 12, "6-12h"),
    (12, 24, "12-24h"),
    (24, float("inf"), "24h+"),
]

# Tail zones for favorite-space edge computation (used by report only)
HIGH_TAIL = (0.85, 0.97)
LOW_TAIL = (0.03, 0.15)


# ── Data loading ─────────────────────────────────────────────────────

# Source priority: prefer hourly candles (widest coverage, consistent 1h
# resolution, has trade OHLC), then snapshots (higher resolution but narrow
# coverage, no trade OHLC), then daily candles (coarsest).
# For each market ticker, observations come from exactly one source.
SOURCE_PRIORITY = {'hourly_candle': 0, 'snapshot': 1, 'daily_candle': 2}

# Price methods for multi-version edge computation.
# Each answers: "what's the edge if I enter at this price?"
#   mid:   (bid+ask)/2 — theoretical midpoint
#   bid:   bid price — resting order entry (most favorable for buyer)
#   ask:   ask price — crossing the spread (least favorable for buyer)
#   trade: last trade close price — actual clearing price
PRICE_METHODS = ('mid', 'bid', 'ask', 'trade')

_HOURLY_SQL = """
SELECT
    hc.ticker,
    hc.yes_bid_close AS yes_bid,
    hc.yes_ask_close AS yes_ask,
    (hc.yes_bid_close + hc.yes_ask_close) / 2.0 AS yes_mid,
    COALESCE(hc.price_close, 0) AS trade_price,
    sm.result,
    EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - hc.period_end)) / 3600.0
        AS hours_to_settlement,
    split_part(hc.ticker, '-', 1) AS series,
    'hourly_candle' AS source,
    mc.generating_process,
    mc.topic
FROM prediction_markets.kalshi_hourly_candles hc
JOIN prediction_markets.kalshi_settled_markets sm ON sm.ticker = hc.ticker
LEFT JOIN prediction_markets.market_classifications mc
    ON mc.series_ticker = split_part(hc.ticker, '-', 1)
WHERE sm.result IN ('yes', 'no')
  AND sm.settled_at != ''
  AND hc.period_end < sm.settled_at::timestamptz
  AND hc.yes_bid_close > 0 AND hc.yes_ask_close > 0
  AND (hc.yes_ask_close - hc.yes_bid_close) <= %(max_spread)s
"""

_SNAPSHOT_SQL = """
SELECT
    ks.ticker,
    ks.yes_bid / 100.0 AS yes_bid,
    ks.yes_ask / 100.0 AS yes_ask,
    (ks.yes_bid + ks.yes_ask) / 200.0 AS yes_mid,
    0 AS trade_price,
    sm.result,
    EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - ks.timestamp)) / 3600.0
        AS hours_to_settlement,
    split_part(ks.ticker, '-', 1) AS series,
    'snapshot' AS source,
    mc.generating_process,
    mc.topic
FROM prediction_markets.kalshi_snapshots ks
JOIN prediction_markets.kalshi_settled_markets sm ON sm.ticker = ks.ticker
LEFT JOIN prediction_markets.market_classifications mc
    ON mc.series_ticker = split_part(ks.ticker, '-', 1)
WHERE sm.result IN ('yes', 'no')
  AND sm.settled_at != ''
  AND ks.timestamp < sm.settled_at::timestamptz
  AND ks.yes_bid > 0 AND ks.yes_ask > 0
  AND (ks.yes_ask - ks.yes_bid) / 100.0 <= %(max_spread)s
"""

_DAILY_SQL = """
SELECT
    dc.ticker,
    dc.yes_bid_close / 100.0 AS yes_bid,
    dc.yes_ask_close / 100.0 AS yes_ask,
    (dc.yes_bid_close + dc.yes_ask_close) / 200.0 AS yes_mid,
    COALESCE(dc.price_close, 0) / 100.0 AS trade_price,
    sm.result,
    EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - dc.period_end)) / 3600.0
        AS hours_to_settlement,
    split_part(dc.ticker, '-', 1) AS series,
    'daily_candle' AS source,
    mc.generating_process,
    mc.topic
FROM prediction_markets.kalshi_candlesticks dc
JOIN prediction_markets.kalshi_settled_markets sm ON sm.ticker = dc.ticker
LEFT JOIN prediction_markets.market_classifications mc
    ON mc.series_ticker = split_part(dc.ticker, '-', 1)
WHERE sm.result IN ('yes', 'no')
  AND sm.settled_at != ''
  AND dc.period_end < sm.settled_at::timestamptz
  AND dc.yes_bid_close > 0 AND dc.yes_ask_close > 0
  AND (dc.yes_ask_close - dc.yes_bid_close) / 100.0 <= %(max_spread)s
"""


@dataclass
class Observation:
    ticker: str         # full market ticker (for per-market deduplication)
    yes_bid: float      # YES bid price (dollars)
    yes_ask: float      # YES ask price (dollars)
    yes_mid: float      # (bid + ask) / 2
    trade_price: float  # trade close price (0 if unavailable)
    result_yes: bool    # True if result == 'yes'
    hours_to_settlement: float
    series: str
    source: str         # 'hourly_candle', 'snapshot', 'daily_candle'
    generating_process: str  # from market_classifications
    topic: str  # from market_classifications

    @property
    def yes_price(self):
        """Backward-compatible alias for yes_mid."""
        return self.yes_mid


def _load_source(conn, sql, params, cursor_name, series_filter=None,
                 settled_before=None):
    """Load observations from a single source query."""
    if series_filter:
        sql += " AND split_part(sm.ticker, '-', 1) LIKE %(series_filter)s"
        params = {**params, "series_filter": series_filter}
    if settled_before:
        sql += " AND sm.settled_at::timestamptz < %(settled_before)s"
        params = {**params, "settled_before": settled_before}

    cur = conn.cursor(cursor_name)
    cur.itersize = 50000
    cur.execute(sql, params)

    observations = []
    for row in cur:
        (ticker, yes_bid, yes_ask, yes_mid, trade_price,
         result, hours, series, source, gen_proc, topic) = row
        if hours is None or hours < 0 or series is None:
            continue
        if yes_mid <= 0 or yes_mid >= 1:
            continue
        observations.append(Observation(
            ticker=ticker,
            yes_bid=float(yes_bid),
            yes_ask=float(yes_ask),
            yes_mid=float(yes_mid),
            trade_price=float(trade_price) if trade_price else 0.0,
            result_yes=(result == "yes"),
            hours_to_settlement=float(hours),
            series=series,
            source=source,
            generating_process=gen_proc or "unknown",
            topic=topic or "unknown",
        ))
    cur.close()
    return observations


def _dedup_by_source(observations):
    """Keep observations from highest-priority source per ticker.

    For each ticker, finds the best available source (lowest priority number)
    and discards observations from other sources. This ensures no ticker
    contributes duplicate observations from overlapping data sources.
    """
    ticker_best = {}
    for obs in observations:
        p = SOURCE_PRIORITY.get(obs.source, 99)
        if obs.ticker not in ticker_best or p < ticker_best[obs.ticker]:
            ticker_best[obs.ticker] = p
    return [obs for obs in observations
            if SOURCE_PRIORITY.get(obs.source, 99) == ticker_best[obs.ticker]]


def load_observations(conn, series_filter=None, settled_before=None):
    """Load observations from all sources, deduplicated by source priority.

    If settled_before is provided (ISO date string or datetime), only includes
    observations from markets that settled before that date.
    """
    params = {"max_spread": MAX_SPREAD}

    all_obs = []
    for label, sql, cname in [("hourly candles", _HOURLY_SQL, "cal_hourly"),
                               ("snapshots", _SNAPSHOT_SQL, "cal_snap"),
                               ("daily candles", _DAILY_SQL, "cal_daily")]:
        batch = _load_source(conn, sql, params, cname, series_filter,
                             settled_before=settled_before)
        print(f"  {label}: {len(batch):,} observations, "
              f"{len(set(o.ticker for o in batch)):,} tickers", file=sys.stderr)
        all_obs.extend(batch)

    before = len(all_obs)
    all_obs = _dedup_by_source(all_obs)
    dropped = before - len(all_obs)
    if dropped:
        print(f"  dedup: dropped {dropped:,} overlapping observations", file=sys.stderr)

    return all_obs


# ── Bucketing ────────────────────────────────────────────────────────

def price_bucket(p):
    """Return (lo, hi) for a YES price."""
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def time_bucket(hours):
    """Return (lo, hi, label) for hours-to-settlement."""
    for lo, hi, label in TIME_BUCKETS:
        if lo <= hours < hi:
            return (lo, hi, label)
    return None


@dataclass
class BucketStats:
    n: int = 0
    sum_price: float = 0.0
    sum_yes: int = 0

    def add(self, yes_price, result_yes):
        self.n += 1
        self.sum_price += yes_price
        self.sum_yes += int(result_yes)

    @property
    def avg_price(self):
        return self.sum_price / self.n if self.n else 0

    @property
    def empirical_yes_rate(self):
        return self.sum_yes / self.n if self.n else 0

    @property
    def calibration_gap(self):
        """avg(YES price) - avg(empirical YES rate). Positive = YES overpriced."""
        return self.avg_price - self.empirical_yes_rate


@dataclass
class FavoriteStats:
    """Stats in favorite-space for edge computation."""
    n: int = 0
    sum_fav_price: float = 0.0
    sum_fav_wins: int = 0

    def add(self, fav_price, fav_wins):
        self.n += 1
        self.sum_fav_price += fav_price
        self.sum_fav_wins += int(fav_wins)

    @property
    def avg_fav_price(self):
        return self.sum_fav_price / self.n if self.n else 0

    @property
    def fav_win_rate(self):
        return self.sum_fav_wins / self.n if self.n else 0

    @property
    def edge(self):
        """avg(fav_wins) - avg(fav_price). Positive = favorite underpriced = our strategy profits."""
        return self.fav_win_rate - self.avg_fav_price


def bucket_observations(observations):
    """Bucket observations along multiple dimensions.

    Returns dict of dicts, keyed by grouping level:
        'series_price_time': dict[(series, price_label, time_label)] -> BucketStats
        'series_time_fav': dict[(series, time_label)] -> FavoriteStats
        'series_fav': dict[series] -> FavoriteStats
        'process_price_time': dict[(generating_process, price_label, time_label)] -> BucketStats
        'process_time_fav': dict[(generating_process, time_label)] -> FavoriteStats
        'process_fav': dict[generating_process] -> FavoriteStats
        'topic_fav': dict[topic] -> FavoriteStats
        'process_topic_fav': dict[(generating_process, topic)] -> FavoriteStats
        'overall_price': dict[price_label] -> BucketStats
    """
    r = {
        'series_price_time': defaultdict(BucketStats),
        'series_time_fav': defaultdict(FavoriteStats),
        'series_fav': defaultdict(FavoriteStats),
        'process_price_time': defaultdict(BucketStats),
        'process_time_fav': defaultdict(FavoriteStats),
        'process_fav': defaultdict(FavoriteStats),
        'topic_fav': defaultdict(FavoriteStats),
        'process_topic_fav': defaultdict(FavoriteStats),
        'overall_price': defaultdict(BucketStats),
    }

    for obs in observations:
        pb = price_bucket(obs.yes_price)
        tb = time_bucket(obs.hours_to_settlement)
        if pb is None or tb is None:
            continue

        _, _, time_label = tb
        price_label = f"{pb[0]:.2f}-{pb[1]:.2f}"
        gp = obs.generating_process
        topic = obs.topic

        # YES-space calibration
        r['series_price_time'][(obs.series, price_label, time_label)].add(obs.yes_price, obs.result_yes)
        r['process_price_time'][(gp, price_label, time_label)].add(obs.yes_price, obs.result_yes)
        r['overall_price'][price_label].add(obs.yes_price, obs.result_yes)

        # Favorite-space edge (both tails)
        in_high_tail = HIGH_TAIL[0] <= obs.yes_price <= HIGH_TAIL[1]
        in_low_tail = LOW_TAIL[0] <= obs.yes_price <= LOW_TAIL[1]

        if in_high_tail:
            fav_price = obs.yes_price
            fav_wins = obs.result_yes
        elif in_low_tail:
            fav_price = 1.0 - obs.yes_price
            fav_wins = not obs.result_yes
        else:
            continue

        r['series_time_fav'][(obs.series, time_label)].add(fav_price, fav_wins)
        r['series_fav'][obs.series].add(fav_price, fav_wins)
        r['process_time_fav'][(gp, time_label)].add(fav_price, fav_wins)
        r['process_fav'][gp].add(fav_price, fav_wins)
        r['topic_fav'][topic].add(fav_price, fav_wins)
        r['process_topic_fav'][(gp, topic)].add(fav_price, fav_wins)

    return r


# ── Event rate computation ─────────────────────────────────────────

# Adaptive bucketing parameters — thresholds are in distinct markets, not
# raw observations, because per-market dedup makes market count the true N.
# SE ≈ sqrt(p(1-p)/n_markets); for p=0.90 and n=30, SE ≈ 5.5% per bucket.
MIN_MARKETS_PER_BUCKET = 30
MAX_BUCKETS = 20
MIN_TOTAL_MARKETS = 50


def _get_conditioning_price(obs, price_method):
    """Return the YES-side price for a given price method, or None if unavailable."""
    if price_method not in PRICE_METHODS:
        raise ValueError(f"Unknown price_method: {price_method!r}")
    if price_method == 'mid':
        return obs.yes_mid
    elif price_method == 'bid':
        return obs.yes_bid
    elif price_method == 'ask':
        return obs.yes_ask
    elif price_method == 'trade':
        return obs.trade_price if obs.trade_price > 0 else None


def compute_event_rates(observations, price_method='mid'):
    """Compute P(YES) with adaptive time bucketing per (process, topic, price_bucket) cell.

    For each observation, the conditioning price (bid/mid/ask/trade) determines
    which price bucket the observation falls in. Cells with fewer than
    MIN_TOTAL_MARKETS distinct tickers are skipped. Within qualifying cells,
    observations are sorted by hours_to_settlement and equipartitioned into
    k = min(n_markets // MIN_MARKETS_PER_BUCKET, MAX_BUCKETS) time buckets.

    Stores P(YES) — the fraction of times the contract settled YES — along
    with the average observed conditioning price for diagnostics.

    After computing raw per-bucket rates, applies market-count-weighted 5-bucket
    moving average smoothing across time to reduce noise.

    Returns list of dicts ready for DB insertion.
    """
    # Group observations by (process, topic, price_bucket)
    # Each price method independently determines bucket membership via its own price.
    cells = defaultdict(list)
    # (gp, topic, price_bucket) -> [(observed_price, result_yes, hours, ticker, source)]
    for obs in observations:
        price = _get_conditioning_price(obs, price_method)
        if price is None:
            continue
        pb = price_bucket(price)
        if pb is None:
            continue
        cells[(obs.generating_process, obs.topic, pb)].append(
            (price, obs.result_yes, obs.hours_to_settlement, obs.ticker, obs.source)
        )

    results = []
    for (gp, topic, (pb_lo, pb_hi)), points in cells.items():
        n_markets_total = len(set(p[3] for p in points))
        if n_markets_total < MIN_TOTAL_MARKETS:
            continue

        k = min(n_markets_total // MIN_MARKETS_PER_BUCKET, MAX_BUCKETS)
        k = max(k, 1)

        # Sort by time and equipartition observations into k pieces.
        points.sort(key=lambda x: x[2])
        n_obs_total = len(points)
        bucket_size = n_obs_total / k

        raw_buckets = []
        for i in range(k):
            start = int(round(i * bucket_size))
            end = int(round((i + 1) * bucket_size))
            bucket = points[start:end]

            hours_from = bucket[0][2]
            hours_to = bucket[-1][2]
            n_obs = len(bucket)

            # Track dominant source for provenance
            source_counts = defaultdict(int)
            for _, _, _, _, src in bucket:
                source_counts[src] += 1
            dominant_source = max(source_counts, key=source_counts.get)

            # Group by market ticker, compute per-market averages.
            # Each market contributes one observation (averaged across its
            # candles in this bucket) to avoid pseudo-replication.
            market_data = defaultdict(lambda: {'sum_price': 0, 'sum_yes': 0, 'n': 0})
            for price, result_yes, h, ticker, _ in bucket:
                md = market_data[ticker]
                md['sum_price'] += price
                md['sum_yes'] += int(result_yes)
                md['n'] += 1

            n_markets = len(market_data)
            market_rates = []
            market_prices = []
            for md in market_data.values():
                market_prices.append(md['sum_price'] / md['n'])
                market_rates.append(md['sum_yes'] / md['n'])

            avg_observed_price = sum(market_prices) / n_markets
            event_rate = sum(market_rates) / n_markets

            # SE of event rate from distinct markets
            se = math.sqrt(event_rate * (1 - event_rate) / n_markets) if n_markets > 0 else 0

            raw_buckets.append({
                'generating_process': gp,
                'topic': topic,
                'price_lo': pb_lo,
                'price_hi': pb_hi,
                'bucket_index': i,
                'hours_from': hours_from,
                'hours_to': hours_to,
                'n': n_obs,
                'n_markets': n_markets,
                'event_rate': event_rate,
                'avg_observed_price': avg_observed_price,
                'se': se,
                'price_method': price_method,
                'source': dominant_source,
            })

        # Smooth event_rate across time with market-count-weighted 5-bucket moving average
        for i, b in enumerate(raw_buckets):
            neighbors = [raw_buckets[j] for j in range(max(0, i-2), min(k, i+3))]
            total_m = sum(nb['n_markets'] for nb in neighbors)
            b['smoothed_event_rate'] = (
                sum(nb['n_markets'] * nb['event_rate'] for nb in neighbors) / total_m
            )

        results.extend(raw_buckets)

    return results


def store_event_rates(conn, rates):
    """Write event rate results to prediction_markets.calibration_rates.

    Truncates and replaces all rows (full refresh across all price methods).
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM prediction_markets.calibration_rates")
    for r in rates:
        cur.execute("""
            INSERT INTO prediction_markets.calibration_rates
                (generating_process, topic, price_lo, price_hi,
                 bucket_index, hours_from, hours_to, n, n_markets,
                 event_rate, smoothed_event_rate, avg_observed_price,
                 se, price_method, source)
            VALUES (%(generating_process)s, %(topic)s, %(price_lo)s, %(price_hi)s,
                    %(bucket_index)s, %(hours_from)s, %(hours_to)s,
                    %(n)s, %(n_markets)s, %(event_rate)s, %(smoothed_event_rate)s,
                    %(avg_observed_price)s, %(se)s, %(price_method)s, %(source)s)
        """, r)
    conn.commit()
    cur.close()
    return len(rates)


# ── Reporting ────────────────────────────────────────────────────────

def _fav_table(p, data, key_label, min_n=30):
    """Helper: print a favorite-space edge table."""
    p(f"| {key_label} | N | Avg Fav Price | Fav Win Rate | Edge |")
    p(f"|{'─'*len(key_label)}|--:|--------------:|-------------:|-----:|")
    for key, stats in sorted(data.items(), key=lambda x: -x[1].edge):
        if stats.n >= min_n:
            label = key if isinstance(key, str) else " × ".join(str(k) for k in key)
            p(f"| {label} | {stats.n:,} | {stats.avg_fav_price:.4f} | "
              f"{stats.fav_win_rate:.4f} | {stats.edge:+.4f} |")


def report(observations, out=sys.stdout):
    """Generate the calibration report."""
    p = lambda *args, **kwargs: print(*args, **kwargs, file=out)

    p("# FLB Calibration Report")
    p()
    p(f"Observations: {len(observations):,}")
    p(f"Spread filter: ≤ {MAX_SPREAD*100:.0f}¢")
    p("See research/CALIBRATION_METHODOLOGY.md for methodology details.")
    p()

    # Source breakdown
    by_source = defaultdict(int)
    for obs in observations:
        by_source[obs.source] += 1
    p("## Data Sources")
    p()
    p("| Source | Observations |")
    p("|--------|-------------|")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
        p(f"| {src} | {n:,} |")
    p()

    b = bucket_observations(observations)

    # ── Overall calibration curve ──
    p("## Overall Calibration Curve")
    p()
    p("| Price Bucket | N | Avg Price | Emp YES | Gap |")
    p("|-------------|--:|----------:|--------:|----:|")
    for lo, hi in PRICE_BUCKETS:
        label = f"{lo:.2f}-{hi:.2f}"
        s = b['overall_price'].get(label)
        if s and s.n >= 10:
            p(f"| {label} | {s.n:,} | {s.avg_price:.4f} | "
              f"{s.empirical_yes_rate:.4f} | {s.calibration_gap:+.4f} |")
    p()

    # ── Edge by generating process ──
    p("## Edge by Generating Process")
    p()
    p("Positive edge = favorite wins more than priced = our strategy profits.")
    p()
    _fav_table(p, b['process_fav'], "Generating Process", min_n=30)
    p()

    # ── Edge by topic ──
    p("## Edge by Topic")
    p()
    _fav_table(p, b['topic_fav'], "Topic", min_n=30)
    p()

    # ── Edge by generating process × topic ──
    p("## Edge by Generating Process × Topic")
    p()
    _fav_table(p, b['process_topic_fav'], "Process × Topic", min_n=30)
    p()

    # ── Edge by generating process × time horizon ──
    p("## Edge by Generating Process × Time-to-Settlement")
    p()
    p("| Process | Time | N | Avg Fav Price | Fav Win Rate | Edge |")
    p("|---------|------|--:|--------------:|-------------:|-----:|")
    process_set = sorted({gp for gp, _ in b['process_time_fav'].keys()})
    for gp in process_set:
        any_printed = False
        for _, _, time_label in TIME_BUCKETS:
            stats = b['process_time_fav'].get((gp, time_label))
            if stats and stats.n >= 20:
                p(f"| {gp} | {time_label} | {stats.n:,} | "
                  f"{stats.avg_fav_price:.4f} | {stats.fav_win_rate:.4f} | {stats.edge:+.4f} |")
                any_printed = True
        if any_printed:
            p("| | | | | | |")
    p()

    # ── Calibration curve by generating process ──
    p("## Calibration Curve by Generating Process")
    p()
    for gp in process_set:
        p(f"### {gp}")
        p()
        p("| Price Bucket | Time | N | Avg Price | Emp YES | Gap |")
        p("|-------------|------|--:|----------:|--------:|----:|")
        for lo, hi in PRICE_BUCKETS:
            price_label = f"{lo:.2f}-{hi:.2f}"
            for _, _, time_label in TIME_BUCKETS:
                s = b['process_price_time'].get((gp, price_label, time_label))
                if s and s.n >= 20:
                    p(f"| {price_label} | {time_label} | {s.n:,} | "
                      f"{s.avg_price:.4f} | {s.empirical_yes_rate:.4f} | {s.calibration_gap:+.4f} |")
        p()

    # ── Per-series edge (all times) ──
    p("## Per-Series Edge (Favorite-Space, Both Tails Combined)")
    p()
    _fav_table(p, b['series_fav'], "Series", min_n=30)
    p()

    # ── Per-series edge by time horizon ──
    p("## Per-Series Edge by Time-to-Settlement")
    p()
    p("| Series | Time | N | Avg Fav Price | Fav Win Rate | Edge |")
    p("|--------|------|--:|--------------:|-------------:|-----:|")
    series_set = sorted({s for s, _ in b['series_time_fav'].keys()})
    for series in series_set:
        any_printed = False
        for _, _, time_label in TIME_BUCKETS:
            stats = b['series_time_fav'].get((series, time_label))
            if stats and stats.n >= 20:
                p(f"| {series} | {time_label} | {stats.n:,} | "
                  f"{stats.avg_fav_price:.4f} | {stats.fav_win_rate:.4f} | {stats.edge:+.4f} |")
                any_printed = True
        if any_printed:
            p("| | | | | | |")
    p()


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FLB calibration analysis")
    parser.add_argument("--series", help="Filter series by SQL LIKE pattern (e.g. 'KXNBA%%')")
    parser.add_argument("--out", help="Write output to file instead of stdout")
    parser.add_argument("--min-n", type=int, default=30,
                        help="Minimum observations for per-series edge (default: 30)")
    parser.add_argument("--store", action="store_true",
                        help="Compute event rates and store to DB (calibration_rates table)")
    parser.add_argument("--settled-before",
                        help="Only use markets settled before this date (YYYY-MM-DD)")
    args = parser.parse_args()

    conn = psycopg2.connect(get_pg_dsn())
    cutoff_label = f" (settled before {args.settled_before})" if args.settled_before else ""
    print(f"Loading observations{cutoff_label}...", file=sys.stderr)
    observations = load_observations(conn, series_filter=args.series,
                                     settled_before=args.settled_before)
    print(f"Loaded {len(observations):,} observations", file=sys.stderr)

    if args.store:
        all_rates = []
        for method in PRICE_METHODS:
            rates = compute_event_rates(observations, price_method=method)
            all_rates.extend(rates)
            cells = set((r['generating_process'], r['topic']) for r in rates)
            price_buckets = set((r['price_lo'], r['price_hi']) for r in rates)
            print(f"  {method}: {len(rates)} rows, {len(cells)} (process×topic) cells, "
                  f"{len(price_buckets)} price buckets", file=sys.stderr)

        n_rows = store_event_rates(conn, all_rates)
        print(f"Stored {n_rows} total rows across {len(PRICE_METHODS)} price methods",
              file=sys.stderr)
        conn.close()
        return

    conn.close()

    if args.out:
        with open(args.out, "w") as f:
            report(observations, out=f)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        report(observations)


if __name__ == "__main__":
    main()
