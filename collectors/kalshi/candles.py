#!/usr/bin/env python3
"""Kalshi candles ingestion — historical backfill and live collection.

Implements the data system spec for kalshi_candles (Section 1.5 multi-resolution):
- Three registry entries share one storage table: minute, hourly, daily
- Historical backfill from /historical/markets/{ticker}/candlesticks (per-ticker)
- Live collection from /markets/candlesticks (batch, up to 50 tickers)
- Splice: historical replaces live via conditional upsert

Uses framework: RateLimiter, RunLogger, ProgressTracker, with_retry.

Usage:
    python -m collectors.kalshi.candles backfill [--resolution 60] [--resume] [--force]
    python -m collectors.kalshi.candles collect [--resolution 60]
    python -m collectors.kalshi.candles sync
    python -m collectors.kalshi.candles status
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
from data.ingestion.run_logger import RunLogger, get_last_run

from trading.kalshi_client import KalshiClient

# --- Configuration ---

SOURCE = "kalshi"
DEFAULT_QPS = 25.0          # Kalshi Advanced Tier = 30; leave headroom
DB_BATCH_SIZE = 5000        # rows per DB insert
BATCH_TICKERS = 50          # tickers per batch candle API call

# Resolution → API period_interval
RESOLUTION_MAP = {
    "minute": 1,
    "hourly": 60,
    "daily": 1440,
}
RESOLUTION_DATASET = {
    1: "kalshi_candles_minute",
    60: "kalshi_candles_hourly",
    1440: "kalshi_candles_daily",
}

# Backfill chunking: how many seconds per API call by resolution
CHUNK_SECONDS = {
    1: 86400,           # 1 day = 1,440 minute candles
    60: 7 * 86400,      # 7 days = 168 hourly candles
    1440: 90 * 86400,   # 90 days = 90 daily candles
}
MAX_LOOKBACK = {
    1: 14 * 86400,      # 14 days for minute (most markets live < 14 days)
    60: 365 * 86400,    # 1 year for hourly
    1440: 4 * 365 * 86400,  # 4 years for daily
}


# --- API layer ---

def kalshi_get(client: KalshiClient, path: str, params: dict | None = None) -> requests.Response:
    """Authenticated Kalshi GET returning raw Response for retry handling."""
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

def _dollars_to_cents(val) -> int | None:
    """Convert API dollar value (string or number) to integer cents.

    Returns None if the value is missing/empty.
    '0.85' -> 85, '0.03' -> 3, None -> None
    """
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None


def _parse_candle(ticker: str, c: dict, resolution: int, origin: str) -> tuple | None:
    """Map an API candle dict to a DB row tuple.

    Handles both historical API format (no _dollars/_fp suffix) and
    live/batch API format (with _dollars/_fp suffix).
    Returns None if the candle has no timestamp.
    """
    ts = c.get("end_period_ts", 0)
    if not ts:
        return None
    period_end = datetime.fromtimestamp(ts, tz=timezone.utc)

    bid = c.get("yes_bid") or {}
    ask = c.get("yes_ask") or {}
    price = c.get("price") or {}

    def ohlc(obj):
        """Extract open/high/low/close from a bid/ask/price sub-object.

        Historical API uses: open, high, low, close (plain)
        Batch/live API uses: open_dollars, high_dollars, low_dollars, close_dollars
        """
        return (
            _dollars_to_cents(obj.get("open_dollars", obj.get("open"))),
            _dollars_to_cents(obj.get("close_dollars", obj.get("close"))),
            _dollars_to_cents(obj.get("high_dollars", obj.get("high"))),
            _dollars_to_cents(obj.get("low_dollars", obj.get("low"))),
        )

    bid_o, bid_c, bid_h, bid_l = ohlc(bid)
    ask_o, ask_c, ask_h, ask_l = ohlc(ask)
    price_o, price_c, price_h, price_l = ohlc(price)

    # Volume and OI: historical uses volume/open_interest, live uses _fp suffix
    vol_raw = c.get("volume_fp", c.get("volume"))
    oi_raw = c.get("open_interest_fp", c.get("open_interest"))
    volume = int(float(vol_raw)) if vol_raw is not None else None
    oi = int(float(oi_raw)) if oi_raw is not None else None

    return (
        ticker, period_end, resolution, origin,
        bid_o, bid_c, bid_h, bid_l,
        ask_o, ask_c, ask_h, ask_l,
        price_o, price_c, price_h, price_l,
        volume, oi,
    )


# --- DB operations ---

_INSERT_COLS = """(ticker, period_end, resolution, origin,
     yes_bid_open, yes_bid_close, yes_bid_high, yes_bid_low,
     yes_ask_open, yes_ask_close, yes_ask_high, yes_ask_low,
     price_open, price_close, price_high, price_low,
     volume, open_interest)"""


def _flush_candles_historical(cur, buffer: list[tuple]) -> int:
    """Insert historical candles with splice-aware upsert.

    Historical has precedence: replaces live rows, never overwrites historical.
    Deduplicates by (ticker, period_end, resolution) — last row wins.
    """
    if not buffer:
        return 0
    # Deduplicate: API can return duplicate timestamps within a single response.
    # Key is (ticker, period_end, resolution) = columns 0, 1, 2.
    seen = {}
    for row in buffer:
        seen[(row[0], row[1], row[2])] = row
    buffer = list(seen.values())
    execute_values(cur, f"""
        INSERT INTO prediction_markets.kalshi_candles {_INSERT_COLS}
        VALUES %s
        ON CONFLICT (ticker, period_end, resolution) DO UPDATE SET
            origin = EXCLUDED.origin,
            yes_bid_open = EXCLUDED.yes_bid_open,
            yes_bid_close = EXCLUDED.yes_bid_close,
            yes_bid_high = EXCLUDED.yes_bid_high,
            yes_bid_low = EXCLUDED.yes_bid_low,
            yes_ask_open = EXCLUDED.yes_ask_open,
            yes_ask_close = EXCLUDED.yes_ask_close,
            yes_ask_high = EXCLUDED.yes_ask_high,
            yes_ask_low = EXCLUDED.yes_ask_low,
            price_open = EXCLUDED.price_open,
            price_close = EXCLUDED.price_close,
            price_high = EXCLUDED.price_high,
            price_low = EXCLUDED.price_low,
            volume = EXCLUDED.volume,
            open_interest = EXCLUDED.open_interest,
            recorded_at = now()
        WHERE prediction_markets.kalshi_candles.origin = 'live'
    """, buffer, page_size=1000)
    return cur.rowcount


def _flush_candles_live(cur, buffer: list[tuple]) -> int:
    """Insert live candles. Never overwrites existing rows (any origin)."""
    if not buffer:
        return 0
    execute_values(cur, f"""
        INSERT INTO prediction_markets.kalshi_candles {_INSERT_COLS}
        VALUES %s
        ON CONFLICT (ticker, period_end, resolution) DO NOTHING
    """, buffer, page_size=1000)
    return cur.rowcount


# --- Backfill: historical candles per-ticker ---

def backfill(conn, client: KalshiClient, resolution: int = 60,
             qps: float = DEFAULT_QPS, resume: bool = False,
             force: bool = False):
    """Download historical candles for all pre-cutoff settled markets.

    Iterates through settled tickers, downloading candles at the given
    resolution via the per-ticker historical endpoint. Resumable: on
    --resume, skips tickers that already have historical data.
    --force: re-download even if data exists (deletes per-ticker first).
    """
    dataset_id = RESOLUTION_DATASET[resolution]
    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(dataset_id, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "backfill", "resolution": resolution})

    cur = conn.cursor()

    try:
        # Get cutoff
        cutoff_resp = api_get(client, limiter, "/historical/cutoff")
        cutoff_ts = cutoff_resp.get("market_settled_ts", "")
        cutoff_dt = datetime.fromisoformat(cutoff_ts.replace("Z", "+00:00"))
        print(f"Market cutoff: {cutoff_ts}")

        # Get all pre-cutoff settled markets
        print("Loading settled market list...")
        cur.execute("""
            SELECT sm.ticker, sm.event_ticker, sm.settled_at
            FROM prediction_markets.kalshi_settled_markets sm
            WHERE sm.result IN ('yes', 'no')
              AND sm.settled_at IS NOT NULL
              AND sm.settled_at < %s
            ORDER BY sm.settled_at DESC
        """, (cutoff_dt,))

        markets = []
        for ticker, event_ticker, settled_at in cur:
            if settled_at is None:
                continue
            if settled_at.tzinfo is None:
                settled_at = settled_at.replace(tzinfo=timezone.utc)
            markets.append((ticker, settled_at))
        print(f"  {len(markets):,} settled markets before cutoff")

        # Resume: skip tickers already downloaded (unless --force)
        if resume and not force:
            cur.execute("""
                SELECT DISTINCT ticker FROM prediction_markets.kalshi_candles
                WHERE resolution = %s AND origin = 'historical'
            """, (resolution,))
            done_tickers = {r[0] for r in cur}
            before = len(markets)
            markets = [(t, sdt) for t, sdt in markets if t not in done_tickers]
            print(f"  Resuming: {before - len(markets):,} already done, {len(markets):,} remaining")
        if force:
            print(f"  Force mode: will delete and re-download existing data per-ticker")

        total_tickers = 0
        total_candles = 0
        total_inserted = 0
        total_empty = 0
        total_errors = 0
        start_wall = time.time()
        chunk_secs = CHUNK_SECONDS[resolution]
        max_lookback = MAX_LOOKBACK[resolution]

        for i, (ticker, settled_at) in enumerate(markets):
            end_unix = int(settled_at.timestamp())
            earliest_ts = end_unix - max_lookback
            ticker_candles = 0
            chunk_end = end_unix
            batch_buffer = []

            # Force mode: delete existing data for this ticker before re-downloading
            if force:
                cur.execute("""
                    DELETE FROM prediction_markets.kalshi_candles
                    WHERE ticker = %s AND resolution = %s AND origin = 'historical'
                """, (ticker, resolution))
                conn.commit()

            while chunk_end > earliest_ts:
                chunk_start = max(chunk_end - chunk_secs, earliest_ts)

                try:
                    data = api_get(client, limiter,
                                   f"/historical/markets/{ticker}/candlesticks",
                                   {"period_interval": resolution,
                                    "start_ts": chunk_start,
                                    "end_ts": chunk_end})
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        if ticker_candles == 0:
                            total_empty += 1
                    else:
                        total_errors += 1
                        if total_errors <= 10:
                            print(f"  Error on {ticker}: {e}")
                    break
                except Exception as e:
                    total_errors += 1
                    if total_errors <= 10:
                        print(f"  Error on {ticker}: {e}")
                    break

                candles = data.get("candlesticks", [])
                for c in candles:
                    row = _parse_candle(ticker, c, resolution, "historical")
                    if row:
                        batch_buffer.append(row)
                        ticker_candles += 1

                if len(batch_buffer) >= DB_BATCH_SIZE:
                    inserted = _flush_candles_historical(cur, batch_buffer)
                    conn.commit()
                    total_inserted += inserted
                    batch_buffer = []

                # Extend backward if data exists at boundary
                if candles:
                    chunk_end = chunk_start
                else:
                    break

            # Flush remainder for this ticker
            if batch_buffer:
                inserted = _flush_candles_historical(cur, batch_buffer)
                conn.commit()
                total_inserted += inserted
                batch_buffer = []

            total_candles += ticker_candles
            if ticker_candles > 0:
                total_tickers += 1
            elif not any(True for _ in []):  # counted above
                pass

            # Progress reporting
            if (i + 1) % 5000 == 0:
                elapsed = time.time() - start_wall
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta_h = (len(markets) - i - 1) / rate / 3600 if rate > 0 else 0
                print(f"  {i + 1:,}/{len(markets):,} ({(i+1)/len(markets):.1%}): "
                      f"{total_tickers:,} with data, {total_candles:,} candles, "
                      f"{total_empty:,} empty, {total_errors:,} errors, "
                      f"{rate:.1f}/s, ETA {eta_h:.1f}h")
                logger.record_progress(
                    rows_fetched=total_candles,
                    rows_inserted=total_inserted,
                )
                logger.set_metadata({
                    "mode": "backfill",
                    "resolution": resolution,
                    "tickers_processed": i + 1,
                    "tickers_total": len(markets),
                    "tickers_with_data": total_tickers,
                })

        # Post-backfill ANALYZE
        print("Running ANALYZE on kalshi_candles...")
        old_autocommit = conn.autocommit
        conn.autocommit = True
        cur.execute("ANALYZE prediction_markets.kalshi_candles")
        conn.autocommit = old_autocommit
        print("ANALYZE complete")

        elapsed = time.time() - start_wall
        logger.record_progress(rows_fetched=total_candles, rows_inserted=total_inserted)
        logger.finish("completed")
        print(f"\nBackfill complete: {total_tickers:,} tickers with data, "
              f"{total_candles:,} candles, {total_inserted:,} inserted, "
              f"{total_empty:,} empty, {total_errors:,} errors "
              f"in {elapsed / 3600:.1f}h")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


# --- Collect: live candles via batch endpoint ---

def collect(conn, client: KalshiClient, resolution: int = 60,
            qps: float = DEFAULT_QPS):
    """Download recent candles for all active markets using the batch endpoint.

    Uses GET /markets/candlesticks with up to 50 tickers per call.
    Collects candles from the last 2 periods backward.
    """
    dataset_id = RESOLUTION_DATASET[resolution]
    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(dataset_id, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "collect", "resolution": resolution})

    cur = conn.cursor()

    try:
        # Get active market tickers from the live API (DB status is stale)
        print("Fetching active markets from API...")
        active_tickers = []
        cursor = None
        while True:
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = api_get(client, limiter, "/events", params)
            for event in data.get("events", []):
                for m in event.get("markets", []):
                    if m.get("status") in ("open", "active"):
                        vol = float(m.get("volume_fp", m.get("volume", "0")))
                        oi = float(m.get("open_interest_fp", m.get("open_interest", "0")))
                        if vol > 0 or oi > 0:
                            active_tickers.append(m["ticker"])
            cursor = data.get("cursor")
            if not cursor or not data.get("events"):
                break
        print(f"Active markets with activity: {len(active_tickers):,}")

        if not active_tickers:
            print("No active markets found.")
            logger.finish("completed")
            return

        # Time window: last 2 periods of this resolution
        now_ts = int(time.time())
        period_secs = resolution * 60
        start_ts = now_ts - (2 * period_secs)

        total_fetched = 0
        total_inserted = 0
        batches = 0
        start_wall = time.time()

        # Process in batches of BATCH_TICKERS
        for batch_start in range(0, len(active_tickers), BATCH_TICKERS):
            batch = active_tickers[batch_start:batch_start + BATCH_TICKERS]
            tickers_csv = ",".join(batch)

            try:
                data = api_get(client, limiter, "/markets/candlesticks", {
                    "market_tickers": tickers_csv,
                    "period_interval": resolution,
                    "start_ts": start_ts,
                    "end_ts": now_ts,
                })
            except Exception as e:
                print(f"  Batch error at offset {batch_start}: {e}")
                continue

            # Response: {"markets": [{"market_ticker": "...", "candlesticks": [...]}]}
            batch_buffer = []
            for market_data in data.get("markets", []):
                ticker = market_data.get("market_ticker", "")
                for c in market_data.get("candlesticks", []):
                    row = _parse_candle(ticker, c, resolution, "live")
                    if row:
                        batch_buffer.append(row)
                        total_fetched += 1

            if batch_buffer:
                inserted = _flush_candles_live(cur, batch_buffer)
                conn.commit()
                total_inserted += inserted

            batches += 1
            if batches % 50 == 0:
                elapsed = time.time() - start_wall
                print(f"  Batch {batches}: {total_fetched:,} candles, "
                      f"{total_inserted:,} new, {elapsed:.0f}s")

        elapsed = time.time() - start_wall
        logger.record_progress(rows_fetched=total_fetched, rows_inserted=total_inserted)
        logger.finish("completed")
        print(f"\nCollect complete: {total_fetched:,} fetched, {total_inserted:,} new "
              f"in {elapsed:.0f}s ({batches} batches)")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


# --- Sync: cron entry point ---

def sync(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """Scheduled sync: collect hourly and daily candles, update health cache.

    Designed to run from a systemd timer. Collects recent candles for
    active markets at hourly and daily resolution.
    """
    print(f"{'=' * 60}")
    print(f"kalshi_candles sync — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 60}")

    # Collect hourly candles
    print("\n--- Hourly candles ---")
    collect(conn, client, resolution=60, qps=qps)

    # Collect daily candles
    print("\n--- Daily candles ---")
    collect(conn, client, resolution=1440, qps=qps)

    # Update health cache
    print("\n--- Health cache ---")
    from data.health.check import update_cache
    updated = update_cache(conn)
    print(f"Updated health cache for {updated} dataset(s)")

    print(f"\n{'=' * 60}")
    print("Sync complete")


# --- Status ---

def status(conn):
    """Show candle data status and health."""
    cur = conn.cursor()

    # Per-resolution stats (using index-backed queries)
    print("Kalshi candles by resolution:")
    for res_name, res_val in RESOLUTION_MAP.items():
        cur.execute("""
            SELECT DISTINCT origin FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
        """, (res_val,))
        origins = [r[0] for r in cur]

        cur.execute("""
            SELECT min(period_end), max(period_end)
            FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
        """, (res_val,))
        row = cur.fetchone()
        min_t, max_t = row if row else (None, None)

        if min_t:
            print(f"\n  {res_name} (resolution={res_val}):")
            print(f"    Origins: {', '.join(origins)}")
            print(f"    Time range: {min_t} to {max_t}")
        else:
            print(f"\n  {res_name} (resolution={res_val}): no data")

    # Approximate total row count from health cache or pg_class
    cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'kalshi_candles'")
    approx = cur.fetchone()
    if approx:
        print(f"\n  Approx total rows: {approx[0]:,}")

    # Health cache info
    for ds_id in RESOLUTION_DATASET.values():
        cur.execute("""
            SELECT row_count, max_freshness, last_computed
            FROM prediction_markets.dataset_health_cache
            WHERE dataset_id = %s
        """, (ds_id,))
        row = cur.fetchone()
        if row:
            print(f"\n  {ds_id} health cache:")
            print(f"    Rows: {row[0]:,}, Freshness: {row[1]}, Cached: {row[2]}")

    # Last ingestion runs
    for ds_id in RESOLUTION_DATASET.values():
        last = get_last_run(ds_id, conn)
        if last:
            print(f"\n  {ds_id} last run:")
            print(f"    Status: {last.status}, Started: {last.started_at}")
            print(f"    Fetched: {last.rows_fetched:,}, Inserted: {last.rows_inserted:,}")
            if last.error_message:
                print(f"    Error: {last.error_message[:200]}")

    # Health status
    from data.health.check import check_one
    for ds_id in RESOLUTION_DATASET.values():
        health = check_one(ds_id, conn)
        if health:
            print(f"\n  {ds_id} health: {health.health_status}")

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
        description="Kalshi candles ingestion — backfill + live collection",
    )
    parser.add_argument(
        "command",
        choices=["backfill", "collect", "sync", "status"],
        help="backfill: historical candles | collect: live candles | sync: scheduled cron | status: show progress",
    )
    parser.add_argument("--resolution", type=str, default="hourly",
                        choices=list(RESOLUTION_MAP.keys()),
                        help="Candle resolution (default: hourly)")
    parser.add_argument("--resume", action="store_true", help="Resume from last position")
    parser.add_argument("--force", action="store_true",
                        help="Force re-download (delete existing per-ticker, then re-fetch)")
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS,
                        help=f"API queries per second (default: {DEFAULT_QPS})")
    args = parser.parse_args()

    resolution = RESOLUTION_MAP[args.resolution]
    conn = get_conn()

    try:
        if args.command == "status":
            status(conn)
        else:
            client = KalshiClient()
            if args.command == "backfill":
                backfill(conn, client, resolution=resolution,
                         qps=args.qps, resume=args.resume,
                         force=args.force)
            elif args.command == "collect":
                collect(conn, client, resolution=resolution, qps=args.qps)
            elif args.command == "sync":
                sync(conn, client, qps=args.qps)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
