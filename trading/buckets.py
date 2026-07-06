"""
Shared bucketing functions and constants.

Used by calibration pipelines (research/calibration.py, fill_calibration.py)
and estimators (event_rate.py, fill_rate.py). Single source of truth for
bucket boundaries and smoothing.
"""

# Price bucket boundaries (YES price, in dollars)
PRICE_BUCKETS = [
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
    (0.20, 0.25), (0.25, 0.30), (0.30, 0.35), (0.35, 0.40),
    (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60),
    (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80),
    (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.00),
]

# Fill rate time bucket boundaries (hours)
FILL_TIME_BREAKS = [1, 3, 6, 12, 24, 72, 168]


def price_bucket(p):
    """Return (lo, hi) for a YES price in dollars, or None if out of range."""
    for lo, hi in PRICE_BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def fill_time_bucket(hours):
    """Return a time bucket label for fill rate calibration."""
    for brk in FILL_TIME_BREAKS:
        if hours < brk:
            return f"<{brk}h"
    return f">{FILL_TIME_BREAKS[-1]}h"


def smooth_time_buckets(raw_buckets, value_key='event_rate', weight_key='n_markets',
                        window=2):
    """Apply weighted moving average smoothing across time-ordered buckets.

    Smooths raw_buckets[i][value_key] using a window of [i-window, i+window+1]
    neighbors, weighted by each neighbor's weight_key.

    Modifies raw_buckets in place, adding a 'smoothed_{value_key}' entry.
    """
    smoothed_key = f"smoothed_{value_key}"
    k = len(raw_buckets)
    for i in range(k):
        lo = max(0, i - window)
        hi = min(k, i + window + 1)
        neighbors = raw_buckets[lo:hi]
        total_w = sum(nb[weight_key] for nb in neighbors)
        if total_w > 0:
            raw_buckets[i][smoothed_key] = (
                sum(nb[weight_key] * nb[value_key] for nb in neighbors) / total_w
            )
        else:
            raw_buckets[i][smoothed_key] = raw_buckets[i][value_key]
