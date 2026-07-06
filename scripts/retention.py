"""Retention sweep for prediction_markets tables.

Applies time-based retention to high-growth tables:
- kalshi_trades:        keep last 120 days (created_time)
- kalshi_snapshots:     keep last 90 days  (timestamp)
- polymarket_snapshots: keep last 120 days (timestamp)
- predictit_snapshots:  keep last 120 days (timestamp)

Special:
- kalshi_candles minute (resolution=1):
  Drops ALL rows. Minute candles are dead data (collection stopped 2026-01-06,
  per the active kalshi-candles-sync.service which only does hourly+daily).
  Kept as one-time cleanup; subsequent runs no-op since nothing writes here.

Run periodically (daily) via the kalshi-retention.timer.

Usage:
    python -m scripts.retention            # run all policies
    python -m scripts.retention --dry-run  # show counts, no deletes
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import psycopg2


POLICIES = [
    # (table, time_column, retention_days, batch_strategy)
    # batch_strategy: 'by_day' for large tables, 'single' for smaller ones
    ("kalshi_trades", "created_time", 120, "by_day"),
    ("kalshi_snapshots", "timestamp", 90, "single"),
    ("polymarket_snapshots", "timestamp", 120, "single"),
    ("predictit_snapshots", "timestamp", 120, "single"),
]

# Special one-shot: drop the entire minute-resolution slice of kalshi_candles
MINUTE_CANDLES_DROP = True


def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if not dsn:
        raise SystemExit("CLAUDE_HUB_PG_DSN not set")
    conn = psycopg2.connect(dsn)
    return conn


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}",
          flush=True)


def count_rows(cur, table: str, time_col: str, days: int) -> int:
    cur.execute(
        f"SELECT COUNT(*) FROM prediction_markets.{table} "
        f"WHERE {time_col} < NOW() - INTERVAL '%s days'",
        (days,),
    )
    return cur.fetchone()[0]


def delete_single(conn, table: str, time_col: str, days: int) -> int:
    cur = conn.cursor()
    log(f"  DELETE {table} older than {days} days (single statement)...")
    t0 = time.monotonic()
    cur.execute(
        f"DELETE FROM prediction_markets.{table} "
        f"WHERE {time_col} < NOW() - INTERVAL '%s days'",
        (days,),
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    dt = time.monotonic() - t0
    log(f"    deleted {n:,} rows in {dt:.1f}s")
    return n


def delete_by_day(conn, table: str, time_col: str, days: int) -> int:
    """Delete in 1-day chunks. Keeps transactions small for big tables.

    Iterates from the oldest day forward until we reach the retention cutoff.
    """
    cur = conn.cursor()
    cur.execute(
        f"SELECT MIN({time_col})::date, "
        f"(NOW() - INTERVAL '%s days')::date AS cutoff "
        f"FROM prediction_markets.{table}",
        (days,),
    )
    row = cur.fetchone()
    oldest, cutoff = row[0], row[1]
    if oldest is None or oldest >= cutoff:
        log(f"  {table}: nothing to delete (oldest={oldest}, cutoff={cutoff})")
        cur.close()
        return 0

    log(f"  {table}: deleting {oldest} → {cutoff} (exclusive) day by day...")
    total = 0
    day = oldest
    t_start = time.monotonic()
    from datetime import timedelta
    while day < cutoff:
        next_day = day + timedelta(days=1)
        t0 = time.monotonic()
        cur.execute(
            f"DELETE FROM prediction_markets.{table} "
            f"WHERE {time_col} >= %s AND {time_col} < %s",
            (day, next_day),
        )
        n = cur.rowcount
        conn.commit()
        total += n
        dt = time.monotonic() - t0
        if n > 0:
            log(f"    {day}: {n:,} rows in {dt:.1f}s "
                f"(running total {total:,})")
        day = next_day
    cur.close()
    log(f"  {table}: total {total:,} rows deleted in "
        f"{time.monotonic() - t_start:.1f}s")
    return total


def drop_minute_candles(conn, batch_size: int = 100_000) -> int:
    """Delete all kalshi_candles rows with resolution=1, batched.

    Minute candles are dead — collection stopped 2026-01-06. Their 93M rows
    take a large slice of kalshi_candles storage. Subsequent runs no-op
    since nothing writes resolution=1.

    Batched via ctid to avoid long-running locks that deadlock with the
    hourly+daily candle sync. Each batch is its own transaction.
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM prediction_markets.kalshi_candles "
                "WHERE resolution = 1")
    n_existing = cur.fetchone()[0]
    if n_existing == 0:
        log("  kalshi_candles minute: already empty, skipping")
        cur.close()
        return 0
    log(f"  kalshi_candles minute: deleting {n_existing:,} rows in "
        f"batches of {batch_size:,}...")
    t_start = time.monotonic()
    total = 0
    while True:
        t0 = time.monotonic()
        cur.execute(
            "DELETE FROM prediction_markets.kalshi_candles "
            "WHERE ctid IN ("
            "  SELECT ctid FROM prediction_markets.kalshi_candles "
            "  WHERE resolution = 1 LIMIT %s"
            ")", (batch_size,))
        n = cur.rowcount
        conn.commit()
        total += n
        if n == 0:
            break
        dt = time.monotonic() - t0
        log(f"    batch: {n:,} rows in {dt:.1f}s "
            f"(running total {total:,} of {n_existing:,})")
    cur.close()
    log(f"    deleted {total:,} rows total in "
        f"{time.monotonic() - t_start:.1f}s")
    return total


def vacuum_tables(conn, tables: list[str]):
    """Run regular VACUUM (not FULL) on tables to allow space reuse.

    Doesn't block reads or writes. Reclaims space inside the file for future
    inserts; doesn't shrink the file. To shrink, VACUUM FULL would be
    needed (but it takes an exclusive lock — risky to do while collectors
    are running).
    """
    # VACUUM cannot run inside a transaction
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)
    cur = conn.cursor()
    for t in tables:
        log(f"  VACUUM ANALYZE {t}...")
        t0 = time.monotonic()
        try:
            cur.execute(f"VACUUM ANALYZE prediction_markets.{t}")
            log(f"    done in {time.monotonic() - t0:.1f}s")
        except Exception as e:
            log(f"    FAILED: {e}")
    cur.close()
    conn.set_isolation_level(old_isolation)


def run(dry_run: bool = False, skip_minute_candles: bool = False,
        skip_vacuum: bool = False):
    conn = get_conn()
    conn.autocommit = False

    log("=== Prediction-markets retention sweep ===")
    if dry_run:
        log("DRY RUN — counting only")

    cur = conn.cursor()
    if dry_run:
        for table, time_col, days, _ in POLICIES:
            n = count_rows(cur, table, time_col, days)
            log(f"  {table} > {days}d: {n:,} rows to delete")
        if MINUTE_CANDLES_DROP and not skip_minute_candles:
            cur.execute("SELECT COUNT(*) FROM prediction_markets.kalshi_candles "
                        "WHERE resolution = 1")
            n = cur.fetchone()[0]
            log(f"  kalshi_candles resolution=1: {n:,} rows to delete")
        cur.close()
        conn.close()
        return

    cur.close()

    # 1. Drop minute candles (one-shot)
    if MINUTE_CANDLES_DROP and not skip_minute_candles:
        log("Phase 1: minute candle one-shot delete")
        drop_minute_candles(conn)

    # 2. Apply table policies
    log("Phase 2: time-based retention")
    affected_tables = []
    for table, time_col, days, strategy in POLICIES:
        if strategy == "by_day":
            n = delete_by_day(conn, table, time_col, days)
        else:
            n = delete_single(conn, table, time_col, days)
        if n > 0:
            affected_tables.append(table)

    # 3. VACUUM (lets space inside the table file be reused; doesn't shrink)
    if not skip_vacuum and (affected_tables or MINUTE_CANDLES_DROP):
        log("Phase 3: VACUUM ANALYZE")
        to_vacuum = list(set(affected_tables + ["kalshi_candles"]))
        vacuum_tables(conn, to_vacuum)

    conn.close()
    log("=== Done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-minute-candles", action="store_true",
                        help="Don't drop kalshi_candles resolution=1 rows")
    parser.add_argument("--skip-vacuum", action="store_true",
                        help="Skip VACUUM after deletes")
    args = parser.parse_args()
    run(dry_run=args.dry_run,
        skip_minute_candles=args.skip_minute_candles,
        skip_vacuum=args.skip_vacuum)
