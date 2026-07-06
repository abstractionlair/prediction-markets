#!/usr/bin/env python3
"""Kalshi trades ingestion — historical backfill and live collection.

Implements the data system spec for the kalshi_trades dataset:
- Historical backfill from /historical/trades (monthly windows, resumable)
- Live collection from /markets/trades (post-cutoff, resumable)
- Sync mode for daily cron (checks cutoff, downloads live trades)
- Splice: historical replaces live via conditional upsert

Uses framework: RateLimiter, RunLogger, ProgressTracker, with_retry.

Usage:
    python -m collectors.kalshi.trades backfill [--resume]
    python -m collectors.kalshi.trades collect [--resume]
    python -m collectors.kalshi.trades sync
    python -m collectors.kalshi.trades status
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import requests
from psycopg2.extras import execute_values

# Add project root so imports work when run as script or module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from data.ingestion.rate_limiter import RateLimiter
from data.ingestion.retry import with_retry
from data.ingestion.run_logger import RunLogger, ProgressTracker, get_last_run

from trading.kalshi_client import KalshiClient

# --- Configuration ---

DATASET_ID = "kalshi_trades"
SOURCE = "kalshi"
DEFAULT_QPS = 25.0          # Kalshi Advanced Tier = 30; leave headroom
API_PAGE_SIZE = 1000        # max per API page
DB_BATCH_SIZE = 5000        # rows per DB insert


# --- API layer ---

def kalshi_get(client: KalshiClient, path: str, params: dict | None = None) -> requests.Response:
    """Authenticated Kalshi GET returning raw Response for retry handling.

    Unlike KalshiClient.get() (which calls raise_for_status and returns JSON),
    this returns the raw Response so with_retry can inspect status codes and
    distinguish retryable (429, 5xx) from non-retryable (403, 404) errors.
    """
    full_path = f"/trade-api/v2{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if qs:
            full_path += "?" + qs
    return client.session.get(
        client.base_url + full_path,
        headers=client._headers("GET", full_path),
        timeout=30,
    )


def api_get(client: KalshiClient, limiter: RateLimiter, path: str,
            params: dict | None = None) -> dict:
    """Rate-limited, retrying API call. Returns parsed JSON."""
    limiter.acquire()
    resp = with_retry(lambda: kalshi_get(client, path, params))
    resp.raise_for_status()
    return resp.json()


# --- Row mapping ---

def _parse_trade(t: dict, origin: str) -> tuple | None:
    """Map an API trade dict to a DB row tuple.

    Converts API dollar strings to integer cents (spec Section 2.3).
    Returns None if the trade is malformed (missing required fields).
    """
    trade_id = t.get("trade_id", "")
    ticker = t.get("ticker", "")
    if not trade_id or not ticker:
        return None

    return (
        trade_id,
        ticker,
        t.get("created_time", ""),
        int(float(t.get("count_fp", "0"))),
        _dollars_to_cents(t.get("yes_price_dollars", "0")),
        _dollars_to_cents(t.get("no_price_dollars", "0")),
        t.get("taker_side", ""),
        origin,
    )


def _dollars_to_cents(dollar_str: str) -> int:
    """Convert API fixed-point dollar string to integer cents.

    '0.85' -> 85, '0.03' -> 3, '1.00' -> 100
    """
    return round(float(dollar_str) * 100)


# --- DB operations ---

def _flush_trades_historical(cur, buffer: list[tuple]) -> int:
    """Insert historical trades with splice-aware upsert.

    Historical has precedence: replaces live rows, never overwrites historical.
    Returns number of rows affected (inserted or updated).
    """
    if not buffer:
        return 0
    execute_values(cur, """
        INSERT INTO prediction_markets.kalshi_trades
            (trade_id, ticker, created_time, count, yes_price, no_price,
             taker_side, origin)
        VALUES %s
        ON CONFLICT (trade_id) DO UPDATE SET
            ticker = EXCLUDED.ticker,
            created_time = EXCLUDED.created_time,
            count = EXCLUDED.count,
            yes_price = EXCLUDED.yes_price,
            no_price = EXCLUDED.no_price,
            taker_side = EXCLUDED.taker_side,
            origin = EXCLUDED.origin,
            recorded_at = now()
        WHERE prediction_markets.kalshi_trades.origin = 'live'
    """, buffer, page_size=1000)
    return cur.rowcount


def _flush_trades_live(cur, buffer: list[tuple]) -> int:
    """Insert live trades. Never overwrites existing rows (any origin).

    Returns number of rows inserted.
    """
    if not buffer:
        return 0
    execute_values(cur, """
        INSERT INTO prediction_markets.kalshi_trades
            (trade_id, ticker, created_time, count, yes_price, no_price,
             taker_side, origin)
        VALUES %s
        ON CONFLICT (trade_id) DO NOTHING
    """, buffer, page_size=1000)
    return cur.rowcount


# --- Backfill: historical trades ---

def backfill(conn, client: KalshiClient, qps: float = DEFAULT_QPS, resume: bool = False):
    """Download all historical trades in monthly windows.

    Each month is independently resumable. On resume, months with existing
    data are skipped (checked via the target table, not ingestion_runs).
    """
    from dateutil.relativedelta import relativedelta

    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(DATASET_ID, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "backfill"})

    cur = conn.cursor()

    try:
        # Get cutoff from Kalshi
        cutoff_resp = api_get(client, limiter, "/historical/cutoff")
        cutoff_ts = cutoff_resp.get("trades_created_ts", "")
        cutoff_dt = datetime.fromisoformat(cutoff_ts.replace("Z", "+00:00"))
        print(f"Trade cutoff: {cutoff_ts}")

        # Build monthly windows from earliest to cutoff
        windows = []
        earliest = datetime(2021, 1, 1, tzinfo=timezone.utc)
        window_end = cutoff_dt
        while window_end > earliest:
            window_start = window_end.replace(day=1)
            if window_start == window_end:
                window_start = window_start - relativedelta(months=1)
            windows.append((window_start, window_end))
            window_end = window_start
        windows.reverse()

        total_fetched = 0
        total_inserted = 0
        completed_months = []
        start_wall = time.time()

        for window_start, window_end in windows:
            label = window_start.strftime("%Y-%m")
            min_ts = int(window_start.timestamp())
            max_ts = int(window_end.timestamp())

            # Resume: skip months that already have historical data
            if resume:
                cur.execute("""
                    SELECT count(*) FROM prediction_markets.kalshi_trades
                    WHERE origin = 'historical'
                      AND created_time >= %s AND created_time < %s
                """, (window_start, window_end))
                existing = cur.fetchone()[0]
                if existing > 0:
                    print(f"  {label}: {existing:,} already downloaded, skipping")
                    total_fetched += existing
                    completed_months.append(label)
                    continue

            # Download this month
            month_fetched = 0
            month_inserted = 0
            cursor = None
            batch_buffer = []

            while True:
                params = {"limit": API_PAGE_SIZE, "min_ts": min_ts, "max_ts": max_ts}
                if cursor:
                    params["cursor"] = cursor

                try:
                    data = api_get(client, limiter, "/historical/trades", params)
                except Exception as e:
                    print(f"  {label}: API error: {e}. Waiting 5s...")
                    time.sleep(5)
                    continue

                trades = data.get("trades", [])
                cursor = data.get("cursor")
                month_fetched += len(trades)

                for t in trades:
                    row = _parse_trade(t, "historical")
                    if row:
                        batch_buffer.append(row)

                if len(batch_buffer) >= DB_BATCH_SIZE:
                    inserted = _flush_trades_historical(cur, batch_buffer)
                    conn.commit()
                    month_inserted += inserted
                    batch_buffer = []

                if not cursor or not trades:
                    break

            # Flush remainder
            if batch_buffer:
                inserted = _flush_trades_historical(cur, batch_buffer)
                conn.commit()
                month_inserted += inserted

            total_fetched += month_fetched
            total_inserted += month_inserted
            completed_months.append(label)

            logger.record_progress(
                rows_fetched=month_fetched,
                rows_inserted=month_inserted,
            )
            logger.set_metadata({
                "mode": "backfill",
                "completed_months": completed_months,
                "cutoff": cutoff_ts,
            })

            if month_fetched > 0:
                elapsed = time.time() - start_wall
                print(f"  {label}: {month_fetched:,} trades, {month_inserted:,} new "
                      f"(total: {total_fetched:,}, {elapsed / 60:.1f}m elapsed)")
            else:
                print(f"  {label}: 0 trades")

        # Post-backfill ANALYZE
        print("Running ANALYZE on kalshi_trades...")
        old_autocommit = conn.autocommit
        conn.autocommit = True
        cur.execute("ANALYZE prediction_markets.kalshi_trades")
        conn.autocommit = old_autocommit
        print("ANALYZE complete")

        elapsed = time.time() - start_wall
        logger.finish("completed")
        print(f"\nBackfill complete: {total_fetched:,} fetched, {total_inserted:,} inserted "
              f"in {elapsed / 60:.1f}m")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


# --- Collect: live trades ---

def collect(conn, client: KalshiClient, qps: float = DEFAULT_QPS, resume: bool = False):
    """Download live trades from the post-cutoff window.

    Uses GET /markets/trades paginating backward from now.
    """
    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(DATASET_ID, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "collect"})

    cur = conn.cursor()

    try:
        # Get cutoff — we want trades AFTER this
        cutoff_resp = api_get(client, limiter, "/historical/cutoff")
        cutoff_ts = cutoff_resp.get("trades_created_ts", "")
        cutoff_dt = datetime.fromisoformat(cutoff_ts.replace("Z", "+00:00"))
        cutoff_unix = int(cutoff_dt.timestamp())
        print(f"Cutoff: {cutoff_ts} — downloading live trades after this")

        if resume:
            cur.execute("""
                SELECT max(created_time) FROM prediction_markets.kalshi_trades
                WHERE origin = 'live'
            """)
            row = cur.fetchone()
            if row[0]:
                print(f"Last live trade: {row[0]}")

        total_fetched = 0
        total_inserted = 0
        batch_buffer = []
        cursor = None
        start_wall = time.time()
        pages = 0

        while True:
            params = {"limit": API_PAGE_SIZE, "min_ts": cutoff_unix}
            if cursor:
                params["cursor"] = cursor

            try:
                data = api_get(client, limiter, "/markets/trades", params)
            except Exception as e:
                print(f"  API error: {e}. Waiting 5s...")
                time.sleep(5)
                continue

            trades = data.get("trades", [])
            cursor = data.get("cursor")
            pages += 1
            total_fetched += len(trades)

            for t in trades:
                row = _parse_trade(t, "live")
                if row:
                    batch_buffer.append(row)

            if len(batch_buffer) >= DB_BATCH_SIZE:
                inserted = _flush_trades_live(cur, batch_buffer)
                conn.commit()
                total_inserted += inserted
                batch_buffer = []

            if pages % 100 == 0:
                elapsed = time.time() - start_wall
                last_time = trades[-1].get("created_time", "?") if trades else "?"
                print(f"  Page {pages}: {total_fetched:,} fetched, {total_inserted:,} new, "
                      f"last={last_time}, {elapsed / 60:.1f}m")
                logger.record_progress(
                    rows_fetched=total_fetched,
                    rows_inserted=total_inserted,
                    cursor=cursor,
                )

            if not cursor or not trades:
                break

        # Flush remainder
        if batch_buffer:
            inserted = _flush_trades_live(cur, batch_buffer)
            conn.commit()
            total_inserted += inserted

        elapsed = time.time() - start_wall
        logger.record_progress(rows_fetched=total_fetched, rows_inserted=total_inserted)
        logger.finish("completed")
        print(f"\nCollect complete: {total_fetched:,} fetched, {total_inserted:,} new "
              f"in {elapsed / 60:.1f}m")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


# --- Sync: daily cron entry point ---

def sync(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """Daily sync: download live trades, check for cutoff advancement.

    Designed to run from a systemd timer (daily). Does NOT re-run
    the full historical backfill — only collects new live trades
    and warns if the cutoff has advanced past our historical data.
    """
    limiter = RateLimiter(SOURCE, qps, conn)
    cur = conn.cursor()

    print(f"{'=' * 60}")
    print(f"kalshi_trades sync — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 60}")

    # Check if cutoff has advanced past our historical data
    cutoff_resp = api_get(client, limiter, "/historical/cutoff")
    cutoff_ts = cutoff_resp.get("trades_created_ts", "")
    cutoff_dt = datetime.fromisoformat(cutoff_ts.replace("Z", "+00:00"))
    print(f"Historical cutoff: {cutoff_ts}")

    cur.execute("""
        SELECT max(created_time) FROM prediction_markets.kalshi_trades
        WHERE origin = 'historical'
    """)
    last_hist = cur.fetchone()[0]
    if last_hist and last_hist < cutoff_dt:
        print(f"WARNING: Historical data ends at {last_hist}, cutoff is {cutoff_dt}")
        print("Run 'backfill --resume' to fill the gap.")
    elif last_hist:
        print(f"Historical data: up to date ({last_hist})")

    cur.close()

    # Download live trades
    print(f"\n--- Live trades ---")
    collect(conn, client, qps=qps, resume=True)

    # Update health cache
    print(f"\n--- Health cache ---")
    from data.health.check import update_cache
    updated = update_cache(conn)
    print(f"Updated health cache for {updated} dataset(s)")

    print(f"\n{'=' * 60}")
    print("Sync complete")


# --- Status ---

def status(conn):
    """Show download progress and health status."""
    cur = conn.cursor()

    # Use index-backed queries instead of full COUNT on 228M+ rows
    cur.execute("""
        SELECT DISTINCT origin FROM prediction_markets.kalshi_trades
    """)
    origins = [r[0] for r in cur]
    print("Kalshi trades origins:", ", ".join(origins))

    # Freshness bounds (fast with index on created_time)
    cur.execute("""
        SELECT min(created_time), max(created_time)
        FROM prediction_markets.kalshi_trades
    """)
    min_t, max_t = cur.fetchone()
    print(f"  Time range: {min_t} to {max_t}")

    # Approximate row count from health cache
    cur.execute("""
        SELECT row_count, max_freshness, last_computed
        FROM prediction_markets.dataset_health_cache
        WHERE dataset_id = %s
    """, (DATASET_ID,))
    row = cur.fetchone()
    if row:
        print(f"  Approx rows: {row[0]:,} (cached at {row[2]})")
    else:
        print("  Row count: health cache not populated")

    # Last ingestion run
    last = get_last_run(DATASET_ID, conn)
    if last:
        print(f"\nLast ingestion run:")
        print(f"  Status: {last.status}")
        print(f"  Started: {last.started_at}")
        print(f"  Finished: {last.finished_at}")
        print(f"  Rows fetched: {last.rows_fetched:,}")
        print(f"  Rows inserted: {last.rows_inserted:,}")
        if last.error_message:
            print(f"  Error: {last.error_message[:200]}")
    else:
        print("\nNo ingestion runs recorded yet.")

    # Health status
    from data.health.check import check_one
    health = check_one(DATASET_ID, conn)
    if health:
        print(f"\nHealth: {health.health_status}")
        print(f"  Max freshness: {health.max_freshness}")
        print(f"  Row count: {health.row_count:,}" if health.row_count else "  Row count: unknown")

    cur.close()


# --- CLI ---

def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    if not dsn:
        print("Error: CLAUDE_HUB_PG_DSN not set", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(dsn)


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi trades ingestion — backfill + live collection",
    )
    parser.add_argument(
        "command",
        choices=["backfill", "collect", "sync", "status"],
        help="backfill: historical trades | collect: live trades | sync: daily cron | status: show progress",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from last position")
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS,
                        help=f"API queries per second (default: {DEFAULT_QPS})")
    args = parser.parse_args()

    conn = get_conn()

    try:
        if args.command == "status":
            status(conn)
        else:
            client = KalshiClient()
            if args.command == "backfill":
                backfill(conn, client, qps=args.qps, resume=args.resume)
            elif args.command == "collect":
                collect(conn, client, qps=args.qps, resume=args.resume)
            elif args.command == "sync":
                sync(conn, client, qps=args.qps)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
