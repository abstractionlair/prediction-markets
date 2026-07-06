"""
Trade-tape-based fill model (V2).

Replaces both FillRateEstimator (binary price-touch, size-independent)
and FillModel (capture_rate × candle_volume, uncalibrated) with a single
model calibrated from trade tape records.

Core idea: simulate virtual resting orders at hourly intervals through
each settled market's lifetime. Track cumulative opposing flow from
placement to settlement. Build empirical exceedance CDFs binned by
(category, time_bucket, side, trailing_vol_bucket, limit_price, outcome).

Two interfaces:
  - estimate() → FlowEstimate: P(fill|won) and P(fill|lost) for a proposed
    order. Used by EVStrategy for trade selection.
  - calibrate() → FlowModel: build from trade tape data. Called by MarketView.

See docs/design/fill-model-v2-spec.md (Draft 6) for full specification.
"""

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class FlowCDF:
    """Empirical CDF of cumulative opposing flow over a time window.

    Stores P(flow >= Q) for a set of quantity thresholds.
    Supports log-linear interpolation for arbitrary Q values.
    """
    thresholds: list[int]    # sorted ascending: [1, 2, 5, 10, 20, 50, 100, 200]
    exceedances: list[float] # P(flow >= threshold[i]), monotonically decreasing
    n_observations: int      # total observations in PARENT bin (both outcomes)
    n_outcome: int           # observations in THIS specific CDF

    def p_fill(self, quantity: int) -> float:
        """Interpolate P(flow >= quantity) via log-linear interpolation.

        Extrapolation beyond max threshold: log-linear if n_observations >= 200,
        else return exceedance at max threshold.
        Hard zero at 2x max threshold.
        """
        if quantity <= 0:
            return 1.0
        if not self.thresholds:
            return 0.0

        # Below or at minimum threshold
        if quantity <= self.thresholds[0]:
            return self.exceedances[0]

        # Beyond max threshold — extrapolate or clamp
        if quantity > self.thresholds[-1]:
            if quantity > 2 * self.thresholds[-1]:
                return 0.0
            if self.n_observations >= 200 and len(self.thresholds) >= 2:
                return self._log_linear_extrapolate(quantity)
            return self.exceedances[-1]

        # Find bracketing thresholds and interpolate
        idx = bisect.bisect_right(self.thresholds, quantity) - 1
        if idx >= len(self.thresholds) - 1:
            return self.exceedances[-1]

        t_lo = self.thresholds[idx]
        t_hi = self.thresholds[idx + 1]
        p_lo = self.exceedances[idx]
        p_hi = self.exceedances[idx + 1]

        if t_lo == t_hi or quantity == t_lo:
            return p_lo

        return _log_linear_interp(t_lo, p_lo, t_hi, p_hi, quantity)

    def _log_linear_extrapolate(self, quantity: int) -> float:
        """Log-linear extrapolation beyond max threshold."""
        t_lo = self.thresholds[-2]
        t_hi = self.thresholds[-1]
        p_lo = self.exceedances[-2]
        p_hi = self.exceedances[-1]
        if p_lo <= 0 or p_hi <= 0:
            return 0.0
        result = _log_linear_interp(t_lo, p_lo, t_hi, p_hi, quantity, clamp=False)
        return max(0.0, result)


@dataclass
class FlowEstimate:
    """Fill probability estimates for a proposed order."""
    p_fill_won: float       # P(fill Q contracts | outcome matches our side)
    p_fill_lost: float      # P(fill Q contracts | outcome is against us)
    # When outcome dimension is merged (fallback level 5+),
    # p_fill_won == p_fill_lost. This is correct — adverse selection
    # adjustment drops out for sparse bins.


# ── Helper functions ─────────────────────────────────────────────────

def _log_linear_interp(t_lo, p_lo, t_hi, p_hi, t, clamp=True):
    """Log-linear interpolation between two (threshold, probability) points.

    Interpolates linearly in log(threshold) space, linearly in probability.
    If clamp=False, allows extrapolation beyond the bracket.
    """
    if t_lo <= 0 or t_hi <= 0 or t_lo == t_hi:
        return p_lo
    frac = (math.log(t) - math.log(t_lo)) / (math.log(t_hi) - math.log(t_lo))
    if clamp:
        frac = max(0.0, min(1.0, frac))
    return p_lo + frac * (p_hi - p_lo)


def compute_opposing_flow(side, limit_price_cents, trade_yes_price,
                          trade_taker_side, trade_count):
    """Compute how many contracts of a trade would fill our resting order.

    Args:
        side: 'yes' or 'no' — our resting side
        limit_price_cents: our limit price in cents
        trade_yes_price: the trade's yes_price in fractional dollars (0.00-1.00)
        trade_taker_side: 'yes' or 'no' — who was the aggressor
        trade_count: number of contracts in this trade

    Returns:
        Number of contracts that would fill (0 if no match).

    YES buy at L: filled by taker_side='no' at yes_price <= L/100
    NO buy at L: filled by taker_side='yes' at yes_price >= (100-L)/100
    """
    if side == 'yes':
        if trade_taker_side == 'no' and trade_yes_price <= limit_price_cents / 100.0:
            return trade_count
    else:  # side == 'no'
        if trade_taker_side == 'yes' and trade_yes_price >= (100 - limit_price_cents) / 100.0:
            return trade_count
    return 0


def compute_trailing_volume(trades, current_time, window_hours=24):
    """Sum trade volumes in the window before current_time.

    Args:
        trades: sorted list of (created_time, count, yes_price, taker_side)
        current_time: the current timestamp
        window_hours: lookback window (default 24h, matching API volume_24h_fp)

    Returns:
        Total contracts traded in [current_time - window, current_time).
    """
    if not trades:
        return 0
    cutoff = current_time - timedelta(hours=window_hours)
    # Binary search for window boundaries
    times = [t[0] for t in trades]
    start = bisect.bisect_left(times, cutoff)
    end = bisect.bisect_left(times, current_time)
    return sum(trades[i][1] for i in range(start, end))


# ── FlowModel ────────────────────────────────────────────────────────

# CDF thresholds for exceedance curves
CDF_THRESHOLDS = [1, 2, 5, 10, 20, 50, 100, 200]

# Limit prices to simulate virtual orders at (cents).
# Every cent from 85 to 97 — the full tail range the strategy uses.
VIRTUAL_ORDER_PRICES = list(range(85, 98))  # [85, 86, ..., 97]

# Time bucket labels and boundaries (hours)
TIME_BUCKETS = [
    ('<1h', 0, 1),
    ('1-3h', 1, 3),
    ('3-6h', 3, 6),
    ('6-12h', 6, 12),
    ('12-24h', 12, 24),
    ('1-3d', 24, 72),
    ('3-7d', 72, 168),
]

# Coarse time bucket mapping
COARSE_MAP = {
    '<1h': '<3h', '1-3h': '<3h',
    '3-6h': '3-12h', '6-12h': '3-12h',
    '12-24h': '12h-3d', '1-3d': '12h-3d',
    '3-7d': '3-7d',
}

# Trailing volume bucket boundaries
VOL_BUCKETS = [
    ('dead', 0, 0),
    ('low', 1, 99),
    ('moderate', 100, 999),
    ('active', 1000, 10000),
    ('high', 10001, float('inf')),
]


def _time_bucket(hours):
    """Map hours-to-settlement to a time bucket label."""
    for label, lo, hi in TIME_BUCKETS:
        if lo <= hours < hi:
            return label
    if hours >= 168:
        return '3-7d'  # cap at longest bucket
    return '<1h'  # negative or tiny


def _trailing_vol_bucket(volume):
    """Map trailing 24h volume to a bucket label."""
    for label, lo, hi in VOL_BUCKETS:
        if lo <= volume <= hi:
            return label
    return 'high'  # shouldn't reach here


def _snap_price(limit_price_cents):
    """Snap a limit price to the nearest VIRTUAL_ORDER_PRICE.

    Clamps to [85, 97] range.
    """
    clamped = max(VIRTUAL_ORDER_PRICES[0],
                  min(VIRTUAL_ORDER_PRICES[-1], limit_price_cents))
    # Find nearest — since VIRTUAL_ORDER_PRICES is every cent, this is just clamping
    idx = bisect.bisect_left(VIRTUAL_ORDER_PRICES, clamped)
    if idx >= len(VIRTUAL_ORDER_PRICES):
        return VIRTUAL_ORDER_PRICES[-1]
    if idx == 0:
        return VIRTUAL_ORDER_PRICES[0]
    # Closest of the two neighbors
    lo = VIRTUAL_ORDER_PRICES[idx - 1]
    hi = VIRTUAL_ORDER_PRICES[idx]
    return lo if (clamped - lo) <= (hi - clamped) else hi


class FlowModel:
    """Predicts fill probability from trade-tape flow distributions.

    Built by calibrate() from historical trade data. Used by MarketView
    to serve fill estimates to EVStrategy and replay.

    Bin key structure (7-tuple):
      (gp, topic, time_bucket, side, vol_bucket, price, outcome)

    Fallback hierarchy drops dimensions when bins are sparse:
      Level 1: full key (gp, topic, tb, side, vb, price, outcome)
      Level 2: drop vol  (gp, topic, tb, side, *, price, outcome)
      Level 3: coarse time (gp, topic, ctb, side, *, price, outcome)
      Level 4: drop price (gp, topic, ctb, side, *, *, outcome)
      Level 5: drop outcome (gp, topic, ctb, side, *, *, *)
      Level 6: drop side (gp, topic, ctb, *, *, *, *)
    """

    MIN_COMBINED = 200    # min total observations (both outcomes) in parent bin
    MIN_PER_OUTCOME = 50  # min per outcome after split

    def __init__(self, flow_table):
        self.flow_table = flow_table

    def estimate(self, gp, topic, hours_to_settle, side, quantity,
                 limit_price_cents, trailing_volume):
        """Predict fill probability for a proposed resting order.

        Args:
            gp, topic: market classification
            hours_to_settle: hours until settlement
            side: 'yes' or 'no' — our resting side
            quantity: number of contracts
            limit_price_cents: our limit price (85-97 range)
            trailing_volume: recent volume (API volume_24h_fp or from tape)

        Returns:
            FlowEstimate with conditional fill probabilities,
            or None if insufficient calibration data.
        """
        tb = _time_bucket(hours_to_settle)
        ctb = COARSE_MAP[tb]
        vb = _trailing_vol_bucket(trailing_volume)
        price = _snap_price(limit_price_cents)

        # Walk fallback hierarchy
        levels = [
            # Level 1: full key with price
            {'won': (gp, topic, tb, side, vb, price, 'won'),
             'lost': (gp, topic, tb, side, vb, price, 'lost'),
             'split': True},
            # Level 2: drop trailing vol, keep price
            {'won': (gp, topic, tb, side, '*', price, 'won'),
             'lost': (gp, topic, tb, side, '*', price, 'lost'),
             'split': True},
            # Level 3: coarse time, keep price
            {'won': (gp, topic, ctb, side, '*', price, 'won'),
             'lost': (gp, topic, ctb, side, '*', price, 'lost'),
             'split': True},
            # Level 4: drop price (merge all prices)
            {'won': (gp, topic, ctb, side, '*', '*', 'won'),
             'lost': (gp, topic, ctb, side, '*', '*', 'lost'),
             'split': True},
            # Level 5: drop outcome
            {'merged': (gp, topic, ctb, side, '*', '*', '*'),
             'split': False},
            # Level 6: drop side
            {'merged': (gp, topic, ctb, '*', '*', '*', '*'),
             'split': False},
        ]

        for level in levels:
            if level['split']:
                cdf_won = self.flow_table.get(level['won'])
                cdf_lost = self.flow_table.get(level['lost'])
                if (cdf_won and cdf_lost
                        and cdf_won.n_observations >= self.MIN_COMBINED
                        and cdf_won.n_outcome >= self.MIN_PER_OUTCOME
                        and cdf_lost.n_outcome >= self.MIN_PER_OUTCOME):
                    return FlowEstimate(
                        p_fill_won=cdf_won.p_fill(quantity),
                        p_fill_lost=cdf_lost.p_fill(quantity))
            else:
                cdf = self.flow_table.get(level.get('merged'))
                if cdf and cdf.n_observations >= self.MIN_COMBINED:
                    p = cdf.p_fill(quantity)
                    return FlowEstimate(p_fill_won=p, p_fill_lost=p)

        return None  # insufficient data at all levels

    @classmethod
    def calibrate(cls, trades_by_ticker, settled_markets, classifications,
                  as_of=None):
        """Build model from trade tape data.

        Args:
            trades_by_ticker: dict of ticker → list of
                (created_time, count, yes_price, taker_side).
                Prices are fractional dollars (0.00-1.00).
                Lists must be sorted by created_time.
            settled_markets: ticker → (settled_at, result, event_ticker)
            classifications: series_ticker → (gp, topic)
            as_of: temporal boundary — only markets settled before this date.

        Returns:
            FlowModel instance with calibrated flow_table.
        """
        if not trades_by_ticker:
            return cls({})

        # Step 1-2: Simulate virtual orders and collect flow observations
        # Key: (gp, topic, time_bucket, side, vol_bucket, price, outcome)
        observations = defaultdict(list)

        for ticker, trades in trades_by_ticker.items():
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

            if not trades:
                continue

            # Precompute: extract trade times once, find settlement boundary
            trade_times = [t[0] for t in trades]
            settle_idx = bisect.bisect_left(trade_times, settled_at)

            # Precompute prefix sums of opposing flow per (side, limit).
            prefix = {}
            for side in ('yes', 'no'):
                prefix[side] = {}
                for limit in VIRTUAL_ORDER_PRICES:
                    psum = [0] * (settle_idx + 1)
                    for i in range(settle_idx):
                        t_time, t_count, t_yes_price, t_taker_side = trades[i]
                        flow = compute_opposing_flow(
                            side, limit, t_yes_price, t_taker_side, t_count)
                        psum[i + 1] = psum[i] + flow
                    prefix[side][limit] = psum

            # Precompute trailing volume prefix sum (all trades, not just tail)
            vol_prefix = [0] * (len(trades) + 1)
            for i in range(len(trades)):
                vol_prefix[i + 1] = vol_prefix[i] + trades[i][1]

            first_trade_time = trades[0][0]

            # Generate hourly placement times
            placement_time = first_trade_time
            while placement_time < settled_at:
                hours_to_settle = (settled_at - placement_time).total_seconds() / 3600.0
                if hours_to_settle <= 0:
                    break
                tb = _time_bucket(hours_to_settle)

                # Trailing 24h volume via binary search on precomputed prefix
                cutoff_24h = placement_time - timedelta(hours=24)
                vol_start = bisect.bisect_left(trade_times, cutoff_24h)
                vol_end = bisect.bisect_left(trade_times, placement_time)
                trail_vol = int(vol_prefix[vol_end] - vol_prefix[vol_start])
                vb = _trailing_vol_bucket(trail_vol)

                # Placement index in trades
                start_idx = bisect.bisect_right(trade_times, placement_time)

                for side in ('yes', 'no'):
                    won = (result == side)
                    outcome = 'won' if won else 'lost'

                    for limit in VIRTUAL_ORDER_PRICES:
                        psum = prefix[side][limit]
                        si = min(start_idx, len(psum) - 1)
                        cum_flow = psum[settle_idx] - psum[si]

                        key = (gp, topic, tb, side, vb, limit, outcome)
                        observations[key].append(cum_flow)

                placement_time += timedelta(hours=1)

        # Step 3: Build CDFs at all fallback levels
        flow_table = {}

        # Level 1: full keys (with price) — already in observations
        _build_cdfs_from_observations(observations, flow_table)

        # Level 2: merge trailing vol, keep price
        merged_vol = defaultdict(list)
        for key, flows in observations.items():
            gp, topic, tb, side, vb, price, outcome = key
            merged_vol[(gp, topic, tb, side, '*', price, outcome)].extend(flows)
        _build_cdfs_from_observations(merged_vol, flow_table)

        # Level 3: coarse time, keep price
        merged_coarse_time = defaultdict(list)
        for key, flows in observations.items():
            gp, topic, tb, side, vb, price, outcome = key
            ctb = COARSE_MAP[tb]
            merged_coarse_time[(gp, topic, ctb, side, '*', price, outcome)].extend(flows)
        _build_cdfs_from_observations(merged_coarse_time, flow_table)

        # Level 4: drop price (merge all prices)
        merged_price = defaultdict(list)
        for key, flows in merged_coarse_time.items():
            gp, topic, ctb, side, _, price, outcome = key
            merged_price[(gp, topic, ctb, side, '*', '*', outcome)].extend(flows)
        _build_cdfs_from_observations(merged_price, flow_table)

        # Level 5: drop outcome
        merged_outcome = defaultdict(list)
        for key, flows in merged_price.items():
            gp, topic, ctb, side, _, _, outcome = key
            merged_outcome[(gp, topic, ctb, side, '*', '*', '*')].extend(flows)
        _build_cdfs_from_observations(merged_outcome, flow_table)

        # Level 6: drop side
        merged_side = defaultdict(list)
        for key, flows in merged_outcome.items():
            gp, topic, ctb, side, _, _, _ = key
            merged_side[(gp, topic, ctb, '*', '*', '*', '*')].extend(flows)
        _build_cdfs_from_observations(merged_side, flow_table)

        return cls(flow_table)


def _build_cdfs_from_observations(obs_dict, flow_table):
    """Build FlowCDFs from observation lists and add to flow_table.

    For outcome-split keys (last element is 'won'/'lost'), n_observations is
    the combined count from both outcomes in the parent bin, and n_outcome is
    this CDF's count. For merged keys (last element is '*'),
    n_outcome == n_observations.

    Works with any key length — outcome is always the last element.
    """
    # First pass: compute parent combined counts for outcome-split CDFs
    parent_counts = {}
    for key, flows in obs_dict.items():
        outcome = key[-1]
        if outcome not in ('won', 'lost'):
            continue
        parent = key[:-1]
        parent_counts[parent] = parent_counts.get(parent, 0) + len(flows)

    for key, flows in obs_dict.items():
        if not flows:
            continue
        outcome = key[-1]

        n_outcome = len(flows)
        if outcome in ('won', 'lost'):
            parent = key[:-1]
            n_observations = parent_counts.get(parent, n_outcome)
        else:
            n_observations = n_outcome

        exceedances = []
        for threshold in CDF_THRESHOLDS:
            count_above = sum(1 for f in flows if f >= threshold)
            exceedances.append(count_above / n_outcome)

        flow_table[key] = FlowCDF(
            thresholds=list(CDF_THRESHOLDS),
            exceedances=exceedances,
            n_observations=n_observations,
            n_outcome=n_outcome,
        )
