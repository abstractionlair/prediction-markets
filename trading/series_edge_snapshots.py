#!/usr/bin/env python3
"""
Per-series FLB edge estimation from snapshot data.

Uses 5-minute snapshot bid/ask (not daily candles) for more accurate
edge estimates at the timescale we actually trade.

For each settled market with snapshot data:
  - Take the last snapshot before settlement (closest to how we'd trade)
  - If the mid-price is in the tail zone (85-97 or 3-15), record it
  - Compare actual win rate to implied win rate from the mid-price

Can also bucket by hours-before-settlement to see how edge varies
with time to resolution.

Usage:
    python series_edge_snapshots.py
    python series_edge_snapshots.py --hours 6   # only snapshots within 6h of settle
    python series_edge_snapshots.py --min-n 20
"""

import argparse
import os

import psycopg2


def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    if not dsn:
        env_path = os.path.expanduser("~/.env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("CLAUDE_HUB_PG_DSN="):
                    dsn = line.split("=", 1)[1].strip().strip("'\"")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


def compute_edges(conn, min_n=10, max_hours=None):
    """Compute per-series edge from snapshot data."""
    cur = conn.cursor()

    hours_filter = ""
    if max_hours:
        hours_filter = f"""
            AND EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - ks.timestamp)) / 3600.0
                BETWEEN 0 AND {max_hours}
        """

    cur.execute(f"""
        WITH last_snap AS (
            SELECT DISTINCT ON (sm.ticker)
                sm.ticker, sm.result,
                SPLIT_PART(sm.ticker, '-', 1) as series,
                ks.yes_bid, ks.yes_ask, ks.no_bid, ks.no_ask,
                (ks.yes_bid + ks.yes_ask) / 2 as yes_mid,
                (ks.no_bid + ks.no_ask) / 2 as no_mid,
                ks.yes_ask - ks.yes_bid as spread,
                EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - ks.timestamp)) / 3600.0
                    as hours_before
            FROM kalshi_settled_markets sm
            JOIN kalshi_snapshots ks ON ks.ticker = sm.ticker
            WHERE sm.result IN ('yes', 'no')
              AND ks.yes_bid > 0 AND ks.yes_ask > 0
              AND ks.yes_ask > ks.yes_bid
              AND (ks.yes_ask - ks.yes_bid) <= 15
              AND ks.timestamp < sm.settled_at::timestamptz
              {hours_filter}
            ORDER BY sm.ticker, ks.timestamp DESC
        ),
        tail_obs AS (
            SELECT series, result, hours_before, spread,
                   CASE
                       WHEN yes_mid >= 85 AND yes_mid <= 97 THEN 'yes_tail'
                       WHEN no_mid >= 85 AND no_mid <= 97 THEN 'no_tail'
                   END as tail_side,
                   CASE
                       WHEN yes_mid >= 85 AND yes_mid <= 97 THEN yes_mid
                       WHEN no_mid >= 85 AND no_mid <= 97 THEN no_mid
                   END as tail_mid,
                   CASE
                       WHEN yes_mid >= 85 AND yes_mid <= 97
                           THEN CASE WHEN result = 'yes' THEN 1 ELSE 0 END
                       WHEN no_mid >= 85 AND no_mid <= 97
                           THEN CASE WHEN result = 'no' THEN 1 ELSE 0 END
                   END as won
            FROM last_snap
            WHERE (yes_mid >= 85 AND yes_mid <= 97)
               OR (no_mid >= 85 AND no_mid <= 97)
        )
        SELECT series,
               COUNT(*) as n,
               ROUND(AVG(won)::numeric * 100, 1) as win_pct,
               ROUND(AVG(tail_mid / 100.0)::numeric * 100, 1) as impl_pct,
               ROUND(AVG(hours_before)::numeric, 1) as avg_hours,
               ROUND(AVG(spread)::numeric, 1) as avg_spread
        FROM tail_obs
        GROUP BY series
        HAVING COUNT(*) >= {min_n}
        ORDER BY COUNT(*) DESC
    """)

    return cur.fetchall()


def compute_by_horizon(conn, min_n=5):
    """Edge by hours-before-settlement bucket."""
    cur = conn.cursor()

    cur.execute("""
        WITH last_snaps AS (
            -- Take ALL snapshots in the tail zone, not just the last one
            SELECT sm.ticker, sm.result,
                   SPLIT_PART(sm.ticker, '-', 1) as series,
                   (ks.yes_bid + ks.yes_ask) / 2 as yes_mid,
                   (ks.no_bid + ks.no_ask) / 2 as no_mid,
                   EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - ks.timestamp)) / 3600.0
                       as hours_before
            FROM kalshi_settled_markets sm
            JOIN kalshi_snapshots ks ON ks.ticker = sm.ticker
            WHERE sm.result IN ('yes', 'no')
              AND ks.yes_bid > 0 AND ks.yes_ask > 0
              AND ks.yes_ask > ks.yes_bid
              AND (ks.yes_ask - ks.yes_bid) <= 15
              AND ks.timestamp < sm.settled_at::timestamptz
              AND ((ks.yes_bid + ks.yes_ask) / 2 >= 85
                OR (ks.no_bid + ks.no_ask) / 2 >= 85)
        ),
        bucketed AS (
            SELECT series,
                   CASE
                       WHEN hours_before < 1 THEN '<1h'
                       WHEN hours_before < 6 THEN '1-6h'
                       WHEN hours_before < 24 THEN '6-24h'
                       WHEN hours_before < 72 THEN '1-3d'
                       ELSE '3d+'
                   END as horizon,
                   CASE
                       WHEN yes_mid >= 85 AND yes_mid <= 97
                           THEN CASE WHEN result = 'yes' THEN 1 ELSE 0 END
                       WHEN no_mid >= 85 AND no_mid <= 97
                           THEN CASE WHEN result = 'no' THEN 1 ELSE 0 END
                   END as won,
                   CASE
                       WHEN yes_mid >= 85 THEN yes_mid / 100.0
                       WHEN no_mid >= 85 THEN no_mid / 100.0
                   END as implied
            FROM last_snaps
        )
        SELECT series, horizon,
               COUNT(*) as n,
               ROUND(AVG(won)::numeric * 100, 1) as win_pct,
               ROUND(AVG(implied)::numeric * 100, 1) as impl_pct
        FROM bucketed
        WHERE won IS NOT NULL
        GROUP BY series, horizon
        HAVING COUNT(*) >= %s
        ORDER BY series,
                 CASE horizon
                     WHEN '<1h' THEN 1 WHEN '1-6h' THEN 2
                     WHEN '6-24h' THEN 3 WHEN '1-3d' THEN 4 ELSE 5
                 END
    """, (min_n,))

    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-n', type=int, default=10)
    parser.add_argument('--hours', type=float, default=None,
                        help='Only use snapshots within N hours of settlement')
    parser.add_argument('--horizons', action='store_true',
                        help='Show edge by time-to-settlement bucket')
    args = parser.parse_args()

    conn = get_conn()

    # Main edge table
    results = compute_edges(conn, min_n=args.min_n, max_hours=args.hours)

    hours_label = f" (within {args.hours}h of settlement)" if args.hours else ""
    print(f"SNAPSHOT-BASED EDGE ANALYSIS{hours_label}")
    print(f"{'Series':>25} {'n':>5} {'Win%':>6} {'Impl%':>6} {'Edge':>6} "
          f"{'AvgHrs':>7} {'Spread':>6}")
    print("-" * 68)

    for series, n, win, impl, hours, spread in results:
        edge = float(win - impl)
        print(f"{series:>25} {n:>5} {float(win):>5.1f}% {float(impl):>5.1f}% "
              f"{edge:>+5.1f}% {float(hours):>6.1f}h {float(spread):>5.1f}c")

    # Horizon breakdown
    if args.horizons:
        print(f"\n\nEDGE BY TIME TO SETTLEMENT")
        horizon_data = compute_by_horizon(conn, min_n=max(5, args.min_n // 2))

        current_series = None
        for series, horizon, n, win, impl in horizon_data:
            if series != current_series:
                print(f"\n  {series}:")
                print(f"    {'Horizon':>8} {'n':>5} {'Win%':>6} {'Impl%':>6} {'Edge':>6}")
                current_series = series
            edge = float(win - impl)
            print(f"    {horizon:>8} {n:>5} {float(win):>5.1f}% {float(impl):>5.1f}% "
                  f"{edge:>+5.1f}%")

    conn.close()


if __name__ == "__main__":
    main()
