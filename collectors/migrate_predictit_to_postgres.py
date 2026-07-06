#!/usr/bin/env python3
"""
One-time migration: PredictIt SQLite -> PostgreSQL

Migrates markets, contracts, and snapshots from the local SQLite DB
to the prediction_markets schema in PostgreSQL.
"""

import os
import sqlite3
from pathlib import Path

import psycopg2

SQLITE_PATH = Path(__file__).parent / "data" / "predictit.db"


def _get_pg_dsn() -> str:
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if dsn:
        return dsn
    raise RuntimeError("CLAUDE_HUB_PG_DSN not set")


def migrate():
    # Connect to both databases
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(_get_pg_dsn())
    pg_cur = pg_conn.cursor()
    pg_cur.execute("SET search_path TO prediction_markets, public")

    # 1. Migrate markets
    print("Migrating markets...")
    rows = sqlite_conn.execute("SELECT market_id, name, short_name, url, added_at FROM markets").fetchall()
    for row in rows:
        pg_cur.execute("""
            INSERT INTO predictit_markets (market_id, name, short_name, url, added_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (market_id) DO UPDATE SET
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                url = EXCLUDED.url
        """, (row['market_id'], row['name'], row['short_name'], row['url'], row['added_at']))
    pg_conn.commit()
    print(f"  Migrated {len(rows)} markets")

    # 2. Migrate contracts
    print("Migrating contracts...")
    rows = sqlite_conn.execute("SELECT contract_id, market_id, name, short_name FROM contracts").fetchall()
    for row in rows:
        pg_cur.execute("""
            INSERT INTO predictit_contracts (contract_id, market_id, name, short_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (contract_id) DO UPDATE SET
                market_id = EXCLUDED.market_id,
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name
        """, (row['contract_id'], row['market_id'], row['name'], row['short_name']))
    pg_conn.commit()
    print(f"  Migrated {len(rows)} contracts")

    # 3. Migrate snapshots in batches
    print("Migrating snapshots...")
    total = sqlite_conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"  Total snapshots to migrate: {total:,}")

    batch_size = 5000
    offset = 0
    migrated = 0

    while offset < total:
        rows = sqlite_conn.execute(
            "SELECT contract_id, timestamp, last_trade_price, best_buy_yes, "
            "best_sell_yes, best_buy_no, best_sell_no, last_close_price, spread_bps "
            "FROM snapshots ORDER BY id LIMIT ? OFFSET ?",
            (batch_size, offset)
        ).fetchall()

        if not rows:
            break

        # Use executemany with a VALUES template for speed
        args = []
        for row in rows:
            args.append((
                row['contract_id'], row['timestamp'], row['last_trade_price'],
                row['best_buy_yes'], row['best_sell_yes'], row['best_buy_no'],
                row['best_sell_no'], row['last_close_price'], row['spread_bps']
            ))

        pg_cur.executemany("""
            INSERT INTO predictit_snapshots
                (contract_id, timestamp, last_trade_price, best_buy_yes,
                 best_sell_yes, best_buy_no, best_sell_no, last_close_price, spread_bps)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, args)
        pg_conn.commit()

        migrated += len(rows)
        offset += batch_size
        if migrated % 50000 == 0 or migrated == total:
            print(f"  Migrated {migrated:,}/{total:,} snapshots ({migrated*100//total}%)")

    # Verify
    print("\nVerification:")
    pg_cur.execute("SELECT COUNT(*) FROM predictit_markets")
    print(f"  PG markets: {pg_cur.fetchone()[0]}")
    pg_cur.execute("SELECT COUNT(*) FROM predictit_contracts")
    print(f"  PG contracts: {pg_cur.fetchone()[0]}")
    pg_cur.execute("SELECT COUNT(*) FROM predictit_snapshots")
    print(f"  PG snapshots: {pg_cur.fetchone()[0]:,}")
    pg_cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM predictit_snapshots")
    row = pg_cur.fetchone()
    print(f"  Time range: {row[0]} to {row[1]}")

    pg_conn.close()
    sqlite_conn.close()
    print("\nMigration complete!")


if __name__ == "__main__":
    migrate()
