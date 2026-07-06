"""Generate training data for the fill hazard model.

v2 design (2026-04-19, post-review):
  - 500 settled tickers, stratified by generating_process
  - 3 placement times per ticker at lifecycle fractions (0.25, 0.5, 0.75)
    from the earliest depth snapshot through close_time — robust to short markets
  - Both sides (YES + NO), ~10 prices per side (symmetric grid, relative to
    own-side best bid in distance-to-touch space)
  - Varied quantity (log-uniform over [1, 100])
  - `would_cross_spread` flagged per row (keep for analysis; filter at use)
  - Target: ~20-30K virtual orders after skips

Changes from v1 (per Codex + Gemini review):
  - Fixed simulator NO-side and sweep bugs (in fill_simulator.py)
  - Multiple placements across lifecycle (was 1)
  - Varied quantity (was fixed 10)
  - Queue-state decomposition features added (in virtual_order.py)
  - Lifecycle-fraction placement (was absolute 2-6h pre-close)

Output: CSV with one row per (ticker, time, side, price, qty) evaluation.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.fill_simulator import load_trades
from research.virtual_order import (
    VirtualOrderResult,
    evaluate_virtual_order,
    get_depth_snapshot,
)


# --- Sampling parameters (v2) ---

DEFAULT_TICKERS_PER_CLASS = 100
DEFAULT_CLASSES = [
    "continuous_underlyer",
    "scheduled_release",
    "convergent_binary",
    "hazard_process",
    "counting_process",
]

# Placement times as fractions of (earliest_depth_snapshot → close_time).
# This naturally handles both short and long markets.
LIFECYCLE_FRACTIONS_V2 = [0.25, 0.5, 0.75]       # v2 spec, 3 points
LIFECYCLE_FRACTIONS_V3 = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]  # v3 scale-up
LIFECYCLE_FRACTIONS = LIFECYCLE_FRACTIONS_V2

# Quantity grid (log-uniform spacing over realistic range)
QUANTITY_GRID = [1, 5, 10, 25, 100]

# Price grid offsets (cents from own-side best bid)
# Positive = more aggressive (higher YES bid / higher NO bid)
# Negative = less aggressive (behind)
PRICE_OFFSETS = [-20, -10, -5, -2, -1, 0, 1, 2, 5]


# --- Ticker selection ---


def select_tickers(conn, n_per_class: int = DEFAULT_TICKERS_PER_CLASS,
                   classes: list[str] = None, seed: int = 42) -> list[dict]:
    """Select settled tickers stratified by generating_process.

    Criteria:
    - result is 'yes' or 'no' (market resolved)
    - close_time is within the depth-data era (Apr 12+)
    - at least one depth snapshot exists for this ticker
    - series has a classification

    Returns list of dicts with ticker, close_time, result, generating_process, topic.
    """
    classes = classes or DEFAULT_CLASSES
    cur = conn.cursor()

    all_rows = []
    for gp in classes:
        cur.execute("""
            SELECT sm.ticker, sm.close_time, sm.result,
                   mc.generating_process, mc.topic, mc.payoff_type,
                   sm.event_ticker
            FROM prediction_markets.kalshi_settled_markets sm
            JOIN prediction_markets.kalshi_settled_events se
              ON sm.event_ticker = se.event_ticker
            JOIN prediction_markets.market_classifications mc
              ON se.series_ticker = mc.series_ticker
            WHERE sm.close_time >= '2026-04-12'
              AND sm.close_time <= NOW()
              AND sm.result IN ('yes', 'no')
              AND mc.generating_process = %s
              AND EXISTS (
                  SELECT 1 FROM prediction_markets.kalshi_snapshots s
                  WHERE s.ticker = sm.ticker
                    AND s.yes_levels IS NOT NULL
                    AND s.timestamp < sm.close_time
              )
            ORDER BY random()
            LIMIT %s
        """, (gp, n_per_class * 3))  # overshoot; we'll filter & take n_per_class
        rows = cur.fetchall()
        for r in rows:
            all_rows.append({
                "ticker": r[0],
                "close_time": r[1],
                "result": r[2],
                "generating_process": r[3],
                "topic": r[4],
                "payoff_type": r[5],
                "event_ticker": r[6],
            })

    # Final stratified sample
    random.seed(seed)
    by_class = {}
    for row in all_rows:
        by_class.setdefault(row["generating_process"], []).append(row)
    sampled = []
    for gp, rows in by_class.items():
        random.shuffle(rows)
        sampled.extend(rows[:n_per_class])

    cur.close()
    return sampled


# --- Placement time selection ---


def pick_placement_times(conn, ticker: str, close_time: datetime,
                          fractions: list[float] = None) -> list[datetime]:
    """Pick depth-snapshot timestamps at lifecycle fractions.

    Lifecycle = [earliest depth snapshot for this ticker] through close_time.
    For each requested fraction, find the snapshot closest (at or before) to
    the target time.

    Returns a list of placement times. May be fewer than len(fractions) if
    the ticker has very few depth snapshots. Empty list if no depth data.
    """
    fractions = fractions or LIFECYCLE_FRACTIONS
    cur = conn.cursor()
    cur.execute("""
        SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
        FROM prediction_markets.kalshi_snapshots
        WHERE ticker = %s
          AND yes_levels IS NOT NULL
          AND timestamp < %s
    """, (ticker, close_time))
    row = cur.fetchone()
    if not row or not row[0]:
        cur.close()
        return []
    earliest, latest, n_snapshots = row

    if n_snapshots < 2:
        # Not enough to pick multiple times; use the single snapshot.
        cur.close()
        return [earliest]

    lifecycle = close_time - earliest
    picks = []
    seen_ts = set()
    for f in fractions:
        target = earliest + lifecycle * f
        cur.execute("""
            SELECT timestamp
            FROM prediction_markets.kalshi_snapshots
            WHERE ticker = %s
              AND timestamp <= %s
              AND yes_levels IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
        """, (ticker, target))
        r = cur.fetchone()
        if r and r[0] not in seen_ts:
            picks.append(r[0])
            seen_ts.add(r[0])
    cur.close()
    return picks


# --- Price grid ---


def price_grid_yes(yes_bid: int) -> list[int]:
    """Candidate YES-buy prices relative to best YES bid.

    Returned prices are clipped to [1, 99].
    """
    return sorted({
        max(1, min(99, yes_bid + off)) for off in PRICE_OFFSETS
    })


def price_grid_no(yes_ask: int) -> list[int]:
    """Candidate NO-buy prices (in yes_price terms).

    Best NO bid is at yes_price = (100 - yes_ask). Offsets are in
    "aggressiveness" terms — positive = higher NO bid (lower yes_price).
    """
    best_no_bid_yes_price = 100 - yes_ask
    return sorted({
        max(1, min(99, best_no_bid_yes_price - off)) for off in PRICE_OFFSETS
    })


# --- Main generator ---


def evaluate_ticker(conn, ticker_info: dict) -> list[dict]:
    """Evaluate virtual orders for one ticker across multiple placement times.

    For each placement time: iterate (side, price, quantity) combinations.
    Returns list of flat dicts, one per virtual order.
    """
    ticker = ticker_info["ticker"]
    close_time = ticker_info["close_time"]

    placements = pick_placement_times(conn, ticker, close_time)
    if not placements:
        return []

    # Load trades ONCE per ticker (from earliest placement - 2h through close)
    t_first = min(placements)
    trade_start = t_first - timedelta(hours=2)
    trades = load_trades(conn, ticker, t0=trade_start, t_end=close_time)

    rows = []
    for t_place in placements:
        snap = get_depth_snapshot(conn, ticker, t_place)
        if snap is None:
            continue
        yes_bid = snap["yes_bid"]
        yes_ask = snap["yes_ask"]
        if yes_bid is None or yes_ask is None:
            continue

        horizon = min(timedelta(days=7), close_time - t_place)
        if horizon <= timedelta(0):
            continue

        for side in ("yes", "no"):
            prices = price_grid_yes(yes_bid) if side == "yes" else price_grid_no(yes_ask)
            for price in prices:
                for quantity in QUANTITY_GRID:
                    r: VirtualOrderResult | None = evaluate_virtual_order(
                        conn, ticker, t_place, side, price, quantity,
                        horizon=horizon,
                        prefetched_trades=trades,
                        prefetched_snapshot=snap,
                    )
                    if r is None:
                        continue
                    d = asdict(r)
                    # Datetime → ISO string for CSV
                    for k, v in list(d.items()):
                        if isinstance(v, datetime):
                            d[k] = v.isoformat()
                    # Add metadata
                    d["generating_process"] = ticker_info["generating_process"]
                    d["topic"] = ticker_info["topic"]
                    d["payoff_type"] = ticker_info["payoff_type"]
                    d["market_result"] = ticker_info["result"]
                    d["close_time"] = close_time.isoformat()
                    if side == "yes":
                        d["pays_off"] = (ticker_info["result"] == "yes")
                    else:
                        d["pays_off"] = (ticker_info["result"] == "no")
                    rows.append(d)
    return rows


def generate(output_path: str, n_per_class: int = DEFAULT_TICKERS_PER_CLASS,
             classes: list[str] = None, seed: int = 42):
    """Run the generator and write results to CSV."""
    conn = psycopg2.connect(os.environ["CLAUDE_HUB_PG_DSN"])

    print(f"Selecting tickers (n_per_class={n_per_class}, seed={seed})...")
    tickers = select_tickers(conn, n_per_class=n_per_class,
                              classes=classes, seed=seed)
    print(f"  Selected {len(tickers)} tickers across "
          f"{len(set(t['generating_process'] for t in tickers))} classes")
    for gp in set(t["generating_process"] for t in tickers):
        n = sum(1 for t in tickers if t["generating_process"] == gp)
        print(f"    {gp}: {n}")

    all_rows = []
    n_no_snapshot = 0
    for i, t_info in enumerate(tickers, 1):
        rows = evaluate_ticker(conn, t_info)
        if rows:
            all_rows.extend(rows)
        else:
            n_no_snapshot += 1
        if i % 25 == 0:
            print(f"  [{i}/{len(tickers)}] rows so far: {len(all_rows):,}, "
                  f"skipped (no snapshot): {n_no_snapshot}")

    print(f"\nTotal rows: {len(all_rows):,} "
          f"(skipped {n_no_snapshot} tickers)")

    if not all_rows:
        print("No rows to write.")
        conn.close()
        return

    # Write CSV
    fieldnames = list(all_rows[0].keys())
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)
    print(f"Wrote {output_path}")

    # Summary stats
    n_filled = sum(1 for r in all_rows if r["fully_filled"])
    n_partial = sum(1 for r in all_rows if 0 < r["contracts_filled"] < r["quantity"])
    print(f"\nSummary:")
    print(f"  Fully filled: {n_filled} ({100*n_filled/len(all_rows):.1f}%)")
    print(f"  Partial: {n_partial} ({100*n_partial/len(all_rows):.1f}%)")
    print(f"  Unfilled: {len(all_rows)-n_filled-n_partial} "
          f"({100*(len(all_rows)-n_filled-n_partial)/len(all_rows):.1f}%)")

    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="work/training_data_v1.csv",
                        help="CSV output path")
    parser.add_argument("--n-per-class", type=int, default=DEFAULT_TICKERS_PER_CLASS)
    parser.add_argument("--classes", type=str, default=None,
                        help="Comma-separated generating_process classes")
    parser.add_argument("--placements", choices=["v2", "v3"], default="v2",
                        help="Lifecycle-fraction schedule (v2=3 points, v3=10 points)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    global LIFECYCLE_FRACTIONS
    LIFECYCLE_FRACTIONS = (LIFECYCLE_FRACTIONS_V3 if args.placements == "v3"
                            else LIFECYCLE_FRACTIONS_V2)

    classes = args.classes.split(",") if args.classes else None
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    generate(args.output, n_per_class=args.n_per_class,
             classes=classes, seed=args.seed)


if __name__ == "__main__":
    main()
