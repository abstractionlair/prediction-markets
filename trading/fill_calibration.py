#!/usr/bin/env python3
"""
Fill probability calibration.

For each (generating_process, topic, time_bucket), computes the empirical
fill rate at various limit prices expressed relative to the bid-ask spread:
  relative_price = (limit - bid) / (ask - bid)
    0.0 = limit at bid (passive, lowest fill probability)
    0.5 = limit at midpoint
    1.0 = limit at ask (crosses spread, highest fill probability)

Uses the fill model against historical hourly candle data — same data
source as the replay.

Usage:
    python fill_calibration.py                  # print summary
    python fill_calibration.py --store          # store to DB
    python fill_calibration.py --granularity 20 # 20 price steps (default: 10)
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

from fill_model import FillModel, CandleData

def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    if not dsn:
        raise RuntimeError("CLAUDE_HUB_PG_DSN environment variable not set")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


def calibrate_fills(conn, fill_model=None, n_price_steps=10,
                    side='yes', min_markets_per_cell=30,
                    settled_before=None):
    """Compute fill rates across (process, topic, time, relative_price).

    For each settled market with hourly candle data:
    1. At each candle period, note the bid/ask (the "observation point")
    2. For a grid of limit prices from bid to ask, check if the fill model
       would fill before settlement using remaining candles
    3. Record (filled_or_not, process, topic, hours_to_settlement, relative_price)

    If settled_before is provided, only uses markets that settled before that date.

    Returns list of dicts with aggregated fill rates.
    """
    if fill_model is None:
        fill_model = FillModel(require_volume=False)

    print("Loading classifications...", file=sys.stderr)
    cur = conn.cursor()
    cur.execute("""
        SELECT series_ticker, generating_process, topic
        FROM market_classifications
        WHERE generating_process IS NOT NULL AND topic IS NOT NULL
    """)
    classifications = {row[0]: (row[1], row[2]) for row in cur}
    print(f"  {len(classifications)} classified series", file=sys.stderr)

    print("Loading settled markets...", file=sys.stderr)
    settled_sql = """
        SELECT ticker, event_ticker, result, settled_at
        FROM kalshi_settled_markets
        WHERE result IN ('yes', 'no') AND settled_at != '' AND event_ticker != ''
    """
    if settled_before:
        settled_sql += " AND settled_at::timestamptz < %s"
        cur.execute(settled_sql, (settled_before,))
    else:
        cur.execute(settled_sql)
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
        settled[ticker] = {
            'series': series, 'settled_at': sdt, 'result': result,
        }
    print(f"  {len(settled)} classified settled markets", file=sys.stderr)

    print("Loading hourly candles...", file=sys.stderr)
    cur2 = conn.cursor("fill_cal_candles")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT ticker, period_end,
               yes_bid_close, yes_ask_close,
               yes_bid_high, yes_ask_low,
               COALESCE(volume, 0),
               COALESCE(price_high, 0), COALESCE(price_low, 0)
        FROM kalshi_hourly_candles
        WHERE yes_bid_close IS NOT NULL AND yes_ask_close IS NOT NULL
          AND yes_bid_close > 0 AND yes_ask_close > 0
        ORDER BY ticker, period_end
    """)

    # Group candles by ticker
    ticker_candles = defaultdict(list)
    n_candles = 0
    for (ticker, period_end, bid_close, ask_close,
         bid_high, ask_low, vol, p_high, p_low) in cur2:
        if ticker not in settled:
            continue
        ticker_candles[ticker].append({
            'period_end': period_end,
            'bid_close': float(bid_close),
            'ask_close': float(ask_close),
            'fill_candle': CandleData(
                yes_bid_high=int(round(float(bid_high) * 100)),
                yes_ask_low=int(round(float(ask_low) * 100)),
                volume=vol,
                price_high=int(round(float(p_high) * 100)) if p_high else 0,
                price_low=int(round(float(p_low) * 100)) if p_low else 0,
            ),
        })
        n_candles += 1
    cur2.close()
    print(f"  {n_candles} candles across {len(ticker_candles)} tickers", file=sys.stderr)

    # Relative price grid: 0.0 (bid) to 1.0 (ask)
    rel_prices = [i / n_price_steps for i in range(n_price_steps + 1)]

    # Accumulate fill results conditioned on outcome:
    # (gp, topic, time_bucket, rel_price) -> {
    #   filled_won: int, filled_lost: int, unfilled_won: int, unfilled_lost: int
    # }
    # "won" means the side we're buying settled in our favor.
    TIME_BREAKS = [1, 3, 6, 12, 24, 72, 168]  # hours
    results = defaultdict(lambda: {
        'filled_won': 0, 'filled_lost': 0,
        'unfilled_won': 0, 'unfilled_lost': 0,
    })

    print("Computing fill rates...", file=sys.stderr)
    n_tickers_processed = 0

    for ticker, candles in ticker_candles.items():
        md = settled[ticker]
        series = md['series']
        gp, topic = classifications[series]
        settled_at = md['settled_at']
        result = md['result']

        # Did our side win?  For YES buyer: win if result='yes'.
        # For NO buyer: win if result='no'.
        won = (result == side)

        # Sort candles by time
        candles.sort(key=lambda c: c['period_end'])

        for obs_idx, obs in enumerate(candles):
            period_end = obs['period_end']
            if period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=timezone.utc)
            if period_end >= settled_at:
                continue

            hours_to_settle = (settled_at - period_end).total_seconds() / 3600
            if hours_to_settle <= 0:
                continue

            # Time bucket
            time_bucket = None
            for i, brk in enumerate(TIME_BREAKS):
                if hours_to_settle < brk:
                    time_bucket = f"<{brk}h"
                    break
            if time_bucket is None:
                time_bucket = f">{TIME_BREAKS[-1]}h"

            bid_cents = int(round(obs['bid_close'] * 100))
            ask_cents = int(round(obs['ask_close'] * 100))
            spread = ask_cents - bid_cents
            if spread <= 0:
                continue

            # Remaining candles for fill check
            remaining_candles = [c['fill_candle'] for c in candles[obs_idx + 1:]
                                 if c['period_end'] < settled_at]
            if not remaining_candles:
                continue

            # Test each relative price
            for rel in rel_prices:
                limit_price = bid_cents + int(round(rel * spread))
                limit_price = max(bid_cents, min(ask_cents, limit_price))

                # Check if fill model would fill at this price
                filled = False
                for fc in remaining_candles:
                    if fill_model.check_fill(side, limit_price, 1, fc) > 0:
                        filled = True
                        break

                key = (gp, topic, time_bucket, rel)
                cell = results[key]
                if filled:
                    cell['filled_won' if won else 'filled_lost'] += 1
                else:
                    cell['unfilled_won' if won else 'unfilled_lost'] += 1

        n_tickers_processed += 1
        if n_tickers_processed % 10000 == 0:
            print(f"  {n_tickers_processed}/{len(ticker_candles)} tickers...",
                  file=sys.stderr)

    print(f"  Processed {n_tickers_processed} tickers, "
          f"{len(results)} cells", file=sys.stderr)

    # Build output rows
    rows = []
    for (gp, topic, time_bucket, rel_price), cell in sorted(results.items()):
        total = (cell['filled_won'] + cell['filled_lost']
                 + cell['unfilled_won'] + cell['unfilled_lost'])
        if total < min_markets_per_cell:
            continue
        filled = cell['filled_won'] + cell['filled_lost']
        fill_rate = filled / total if total else 0
        fill_rate_won = cell['filled_won'] / (cell['filled_won'] + cell['unfilled_won']) \
            if (cell['filled_won'] + cell['unfilled_won']) > 0 else 0
        fill_rate_lost = cell['filled_lost'] / (cell['filled_lost'] + cell['unfilled_lost']) \
            if (cell['filled_lost'] + cell['unfilled_lost']) > 0 else 0

        rows.append({
            'generating_process': gp,
            'topic': topic,
            'time_bucket': time_bucket,
            'relative_price': rel_price,
            'fill_rate': fill_rate,
            'fill_rate_won': fill_rate_won,
            'fill_rate_lost': fill_rate_lost,
            'adverse_selection': fill_rate_lost - fill_rate_won,
            'n': total,
            'n_won': cell['filled_won'] + cell['unfilled_won'],
            'n_lost': cell['filled_lost'] + cell['unfilled_lost'],
        })

    return rows


def print_summary(rows, show_adverse_selection=True):
    """Print fill rate summary grouped by (process, topic)."""
    from itertools import groupby

    # Group by (process, topic)
    rows_sorted = sorted(rows, key=lambda r: (r['generating_process'], r['topic']))
    for (gp, topic), group in groupby(rows_sorted,
                                       key=lambda r: (r['generating_process'], r['topic'])):
        group = list(group)
        total_n = sum(r['n'] for r in group)
        print(f"\n{gp} × {topic} ({total_n:,} observations)")

        # Get price steps from first time bucket
        first_tb = group[0]['time_bucket']
        price_steps = sorted(set(r['relative_price'] for r in group
                                 if r['time_bucket'] == first_tb))
        header = " ".join(f"{'r='+format(p, '.1f'):<7}" for p in price_steps)
        print(f"{'time':<8} {header}")

        # Group by time bucket
        by_time = defaultdict(dict)
        for r in group:
            by_time[r['time_bucket']][r['relative_price']] = r

        for tb in sorted(by_time.keys(), key=lambda t: (len(t), t)):
            cells = by_time[tb]
            # Overall fill rate
            vals = " ".join(f"{cells[rp]['fill_rate']:<7.1%}" if rp in cells else "  -    "
                            for rp in price_steps)
            print(f"{tb:<8} {vals}")

            if show_adverse_selection:
                # Fill rate when won
                vals_w = " ".join(
                    f"{cells[rp]['fill_rate_won']:<7.1%}" if rp in cells else "  -    "
                    for rp in price_steps)
                # Fill rate when lost
                vals_l = " ".join(
                    f"{cells[rp]['fill_rate_lost']:<7.1%}" if rp in cells else "  -    "
                    for rp in price_steps)
                # Adverse selection (positive = fills more when losing)
                vals_a = " ".join(
                    f"{cells[rp]['adverse_selection']:+<7.1%}" if rp in cells else "  -    "
                    for rp in price_steps)
                print(f"  won    {vals_w}")
                print(f"  lost   {vals_l}")
                print(f"  adv.s  {vals_a}")
                print()


def store_fill_rates(conn, rows, side):
    """Store fill calibration results to DB."""
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM prediction_markets.calibration_fill_rates WHERE side = %s
    """, (side,))
    for r in rows:
        cur.execute("""
            INSERT INTO prediction_markets.calibration_fill_rates
                (generating_process, topic, time_bucket, relative_price, side,
                 fill_rate, fill_rate_won, fill_rate_lost, adverse_selection,
                 n, n_won, n_lost)
            VALUES (%(generating_process)s, %(topic)s, %(time_bucket)s,
                    %(relative_price)s, %(side)s, %(fill_rate)s,
                    %(fill_rate_won)s, %(fill_rate_lost)s, %(adverse_selection)s,
                    %(n)s, %(n_won)s, %(n_lost)s)
        """, {**r, 'side': side})
    conn.commit()
    cur.close()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Fill probability calibration")
    parser.add_argument('--granularity', type=int, default=10,
                        help='Number of price steps from bid to ask (default: 10)')
    parser.add_argument('--store', action='store_true',
                        help='Store results to DB (runs both YES and NO sides)')
    parser.add_argument('--settled-before',
                        help='Only use markets settled before this date (YYYY-MM-DD)')
    args = parser.parse_args()

    conn = get_conn()
    fm = FillModel(require_volume=False)

    if args.store:
        for side in ('yes', 'no'):
            print(f"\n=== {side.upper()} side ===", file=sys.stderr)
            rows = calibrate_fills(conn, fill_model=fm,
                                   n_price_steps=args.granularity,
                                   side=side,
                                   settled_before=args.settled_before)
            n = store_fill_rates(conn, rows, side)
            print(f"Stored {n} rows for {side} side", file=sys.stderr)
        conn.close()
    else:
        rows = calibrate_fills(conn, fill_model=fm,
                               n_price_steps=args.granularity,
                               side='yes',
                               settled_before=args.settled_before)
        conn.close()
        print_summary(rows)
        print(f"\n{len(rows)} cells with sufficient data")


if __name__ == "__main__":
    main()
