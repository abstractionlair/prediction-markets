#!/usr/bin/env python3
"""
Per-series FLB edge estimation.

Computes the empirical edge (actual win rate - implied win rate) for
tail-priced contracts across all series with candlestick data. Used to
maintain the FLB_SERIES whitelist in trader.py.

Usage:
    python series_edge.py              # Show all series with n >= 10
    python series_edge.py --min-n 30   # Require more observations
    python series_edge.py --update     # Print updated FLB_SERIES dict
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


def compute_series_edges(conn, min_n=10):
    """Compute per-series FLB edge from settled markets with candle data.

    For each series, looks at tail-priced contracts (mid 85-97 or 3-15)
    and compares actual win rate to implied win rate from the price.
    """
    cur = conn.cursor()

    cur.execute("""
        WITH tail_trades AS (
            SELECT SPLIT_PART(sm.ticker, '-', 1) as series,
                   sm.result,
                   (kc.yes_bid_high + kc.yes_ask_low) / 2 as mid,
                   CASE
                       WHEN (kc.yes_bid_high + kc.yes_ask_low) / 2 >= 85 THEN 'yes_tail'
                       WHEN (kc.yes_bid_high + kc.yes_ask_low) / 2 <= 15 THEN 'no_tail'
                   END as tail_side,
                   ROW_NUMBER() OVER (
                       PARTITION BY sm.ticker ORDER BY kc.period_end DESC
                   ) as rn
            FROM kalshi_settled_markets sm
            JOIN kalshi_candlesticks kc ON kc.ticker = sm.ticker
            WHERE sm.result IN ('yes', 'no')
              AND kc.yes_bid_high > 0 AND kc.yes_ask_low > 0
              AND kc.yes_ask_low >= kc.yes_bid_high
              AND (kc.yes_ask_low - kc.yes_bid_high) <= 15
              AND ((kc.yes_bid_high + kc.yes_ask_low) / 2 >= 85
                OR (kc.yes_bid_high + kc.yes_ask_low) / 2 <= 15)
        )
        SELECT series,
               COUNT(*) as n,
               AVG(CASE WHEN tail_side = 'yes_tail' AND result = 'yes' THEN 1.0
                        WHEN tail_side = 'no_tail' AND result = 'no' THEN 1.0
                        ELSE 0.0 END) as win_rate,
               AVG(CASE WHEN tail_side = 'yes_tail' THEN mid / 100.0
                        WHEN tail_side = 'no_tail' THEN (100.0 - mid) / 100.0
                        END) as implied_wr
        FROM tail_trades
        WHERE rn = 1
        GROUP BY series
        HAVING COUNT(*) >= %s
        ORDER BY COUNT(*) DESC
    """, (min_n,))

    results = []
    for series, n, win_rate, implied_wr in cur.fetchall():
        edge_pct = round(float((win_rate - implied_wr) * 100), 1)
        results.append({
            'series': series,
            'n': n,
            'win_rate': round(float(win_rate * 100), 1),
            'implied_wr': round(float(implied_wr * 100), 1),
            'edge_pct': edge_pct,
        })

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-n', type=int, default=10,
                        help='Minimum observations per series')
    parser.add_argument('--update', action='store_true',
                        help='Print updated FLB_SERIES dict for trader.py')
    args = parser.parse_args()

    conn = get_conn()
    results = compute_series_edges(conn, min_n=args.min_n)
    conn.close()

    # Current whitelist for comparison. Populate from your own calibration
    # (the active/blocked series in your config or DB); empty by default so
    # this tool reports every series as a fresh candidate.
    FLB_SERIES: set[str] = set()
    RFLB_SERIES: set[str] = set()

    # Display
    print(f"{'Series':>25} {'n':>5} {'Win%':>6} {'Impl%':>6} {'Edge':>6} {'Status':>10}")
    print("-" * 65)

    positive = []
    negative = []
    new_candidates = []

    for r in results:
        series = r['series']
        edge = r['edge_pct']

        if series in FLB_SERIES:
            status = "ACTIVE"
        elif series in RFLB_SERIES:
            status = "BLOCKED"
        elif edge >= 0.5:
            status = "** NEW **"
            new_candidates.append(r)
        elif edge <= -0.5:
            status = "neg"
        else:
            status = "neutral"

        if edge >= 0.5:
            positive.append(r)
        elif edge <= -0.5:
            negative.append(r)

        print(f"{series:>25} {r['n']:>5} {r['win_rate']:>5.1f}% {r['implied_wr']:>5.1f}% "
              f"{edge:>+5.1f}% {status:>10}")

    print(f"\nSummary:")
    print(f"  Total series with n >= {args.min_n}: {len(results)}")
    print(f"  Positive edge (>= 0.5%): {len(positive)}")
    print(f"  Negative edge (<= -0.5%): {len(negative)}")
    print(f"  Currently active: {len(FLB_SERIES)}")
    print(f"  Currently blocked: {len(RFLB_SERIES)}")
    print(f"  New candidates: {len(new_candidates)}")

    if new_candidates:
        print(f"\n  NEW CANDIDATES (edge >= 0.5%, not in whitelist):")
        for r in sorted(new_candidates, key=lambda x: -x['edge_pct']):
            print(f"    {r['series']:>25}: edge={r['edge_pct']:>+5.1f}% "
                  f"n={r['n']} win={r['win_rate']:.1f}%")

    # Check if any active series should be reconsidered
    reconsidered = [r for r in results
                    if r['series'] in FLB_SERIES and r['edge_pct'] < 0.5]
    if reconsidered:
        print(f"\n  RECONSIDER (active but edge < 0.5% now):")
        for r in reconsidered:
            print(f"    {r['series']:>25}: edge={r['edge_pct']:>+5.1f}% "
                  f"n={r['n']} (was {FLB_SERIES[r['series']]}%)")

    # Check if any blocked series are now positive
    unblocked = [r for r in results
                 if r['series'] in RFLB_SERIES and r['edge_pct'] >= 0.5]
    if unblocked:
        print(f"\n  UNBLOCK? (blocked but edge now >= 0.5%):")
        for r in unblocked:
            print(f"    {r['series']:>25}: edge={r['edge_pct']:>+5.1f}% n={r['n']}")

    if args.update:
        print(f"\n\n# Updated FLB_SERIES for trader.py:")
        print("FLB_SERIES = {")
        for r in sorted(results, key=lambda x: -x['edge_pct']):
            if r['edge_pct'] >= 0.5 and r['series'] not in RFLB_SERIES:
                print(f"    '{r['series']}': {r['edge_pct']},")
        print("}")
        print("\nRFLB_SERIES = {")
        for r in sorted(results, key=lambda x: x['edge_pct']):
            if r['edge_pct'] <= -0.5:
                print(f"    '{r['series']}',  # edge={r['edge_pct']:+.1f}% n={r['n']}")
        print("}")


if __name__ == "__main__":
    main()
