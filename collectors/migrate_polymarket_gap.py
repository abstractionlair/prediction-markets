#!/usr/bin/env python3
"""
One-time catch-up migration: sync Polymarket snapshots from SQLite to PostgreSQL
for rows that came in during the migration gap (since ~21:32 UTC on 2026-03-15).

Usage:
    python migrate_polymarket_gap.py             # Dry run (show count only)
    python migrate_polymarket_gap.py --execute   # Actually insert rows
"""

import argparse
import os
import sqlite3
from pathlib import Path

import psycopg2

SQLITE_DB = Path(__file__).parent / "data" / "polymarket.db"
GAP_CUTOFF = "2026-03-15T21:32:00"


def get_pg_dsn() -> str:
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if dsn:
        return dsn
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("CLAUDE_HUB_PG_DSN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("CLAUDE_HUB_PG_DSN not set")


def main():
    parser = argparse.ArgumentParser(description="Catch-up Polymarket snapshots from SQLite to PostgreSQL")
    parser.add_argument("--execute", action="store_true", help="Actually insert rows (default: dry run)")
    args = parser.parse_args()

    # Connect to SQLite
    if not SQLITE_DB.exists():
        print(f"SQLite database not found at {SQLITE_DB}")
        return

    sqlite_conn = sqlite3.connect(str(SQLITE_DB))
    sqlite_cur = sqlite_conn.cursor()

    # Count gap rows
    sqlite_cur.execute(
        "SELECT COUNT(*) FROM snapshots WHERE timestamp > ?",
        (GAP_CUTOFF,)
    )
    count = sqlite_cur.fetchone()[0]
    print(f"Found {count:,} Polymarket snapshot rows after {GAP_CUTOFF}")

    if not args.execute:
        print("Dry run. Use --execute to actually insert.")
        sqlite_conn.close()
        return

    # Connect to PostgreSQL
    pg_conn = psycopg2.connect(get_pg_dsn())
    pg_cur = pg_conn.cursor()
    pg_cur.execute("SET search_path TO prediction_markets, public")

    # Read gap rows from SQLite
    sqlite_cur.execute("""
        SELECT market_id, timestamp, best_bid, best_ask, mid_price,
               spread_bps, bid_depth, ask_depth, volume_24h
        FROM snapshots
        WHERE timestamp > ?
        ORDER BY timestamp
    """, (GAP_CUTOFF,))

    inserted = 0
    batch_size = 1000
    batch = []

    for row in sqlite_cur:
        batch.append(row)
        if len(batch) >= batch_size:
            psycopg2.extras = __import__('psycopg2.extras', fromlist=['extras'])
            for r in batch:
                pg_cur.execute("""
                    INSERT INTO polymarket_snapshots
                        (market_id, timestamp, best_bid, best_ask, mid_price,
                         spread_bps, bid_depth, ask_depth, volume_24h)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, r)
                inserted += 1
            pg_conn.commit()
            batch = []
            if inserted % 5000 == 0:
                print(f"  Inserted {inserted:,} rows...")

    # Final batch
    for r in batch:
        pg_cur.execute("""
            INSERT INTO polymarket_snapshots
                (market_id, timestamp, best_bid, best_ask, mid_price,
                 spread_bps, bid_depth, ask_depth, volume_24h)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, r)
        inserted += 1
    pg_conn.commit()

    print(f"\nDone. Inserted {inserted:,} rows into polymarket_snapshots.")

    # Verify
    pg_cur.execute("SELECT MAX(timestamp) FROM polymarket_snapshots")
    pg_max = pg_cur.fetchone()[0]
    print(f"PostgreSQL max timestamp is now: {pg_max}")

    pg_conn.close()
    sqlite_conn.close()


if __name__ == "__main__":
    main()
