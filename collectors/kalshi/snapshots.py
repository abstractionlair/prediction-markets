#!/usr/bin/env python3
"""Kalshi snapshots ingestion — continuous orderbook collection.

Implements the data system spec for kalshi_snapshots:
- Live-only (no historical API — missed data is permanently lost)
- Continuous collection: discover open markets, fetch orderbooks
- Discovery uses open_interest_fp from events API (the integer field
  was zeroed by an API change, which broke the old collector)
- ON CONFLICT (ticker, timestamp) DO NOTHING for dedup

Uses framework: RateLimiter, RunLogger, with_retry.

Usage:
    python -m collectors.kalshi.snapshots run [--qps 10]
    python -m collectors.kalshi.snapshots collect [--qps 10]
    python -m collectors.kalshi.snapshots sync [--qps 10]
    python -m collectors.kalshi.snapshots status
"""

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values

# Add project root so imports work when run as script or module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from data.ingestion.rate_limiter import RateLimiter
from data.ingestion.retry import with_retry
from data.ingestion.run_logger import RunLogger, get_last_run

from trading.kalshi_client import KalshiClient

# --- Configuration ---

DATASET_ID = "kalshi_snapshots"
SOURCE = "kalshi"
DEFAULT_QPS = 10.0          # Lower than one-shot collectors; runs continuously
DB_BATCH_SIZE = 500         # Commit every N snapshots
DISCOVER_INTERVAL = 7200    # Re-discover markets every 2 hours (seconds)
ORDERBOOK_BATCH_SIZE = 100  # Max tickers per batch orderbook API call
MAX_TABLE_SIZE_GB = 80      # Pause collection if table exceeds this
DEFAULT_CYCLE_PAUSE = 1800  # Seconds between collection cycles (30 min)
DEPTH_RETENTION_DAYS = 60   # NULL out JSONB depth columns after this many days
# Steady-state math: 48 cycles/day × ~30K tickers × ~600 bytes/row = ~0.75 GB/day
# At 60-day retention: ~45 GB logical (36 GB depth + 9 GB base); the measured
# size also carries dead-space bloat that plain VACUUM never returns to the OS —
# that bloat is what pushed the table past the old 50 GB cap (hit 2026-07-04;
# collection skipped). 80 GB gives ~2x steady-state headroom; pg_repack is the
# real reclaim path.
# Postgres data directory volume: 492 GB (expanded 2026-05-25 after the disk
# incident), 224 GB free as of 2026-07-04.
# Graduated throttle kicks in at 80% cap to provide safety margin

RUNNING = True


def _signal_handler(_signum, _frame):
    global RUNNING
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Shutdown requested...")
    RUNNING = False


# --- API layer ---

def kalshi_get(client: KalshiClient, path: str, params: dict | None = None):
    """Authenticated Kalshi GET returning raw Response for retry handling."""
    import requests
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


def kalshi_get_batch_orderbooks(client: KalshiClient, tickers: list[str]):
    """Fetch orderbooks for up to 100 tickers in one API call.

    Uses repeated query params: ?tickers=T1&tickers=T2&...
    Returns raw Response for retry handling.
    """
    full_path = "/trade-api/v2/markets/orderbooks"
    qs = "&".join(f"tickers={t}" for t in tickers)
    full_path_qs = full_path + "?" + qs
    return client.session.get(
        client.base_url + full_path_qs,
        headers=client._headers("GET", full_path_qs),
        timeout=60,
    )


def api_get_batch_orderbooks(client: KalshiClient, limiter: RateLimiter,
                             tickers: list[str]) -> list[dict]:
    """Rate-limited, retrying batch orderbook fetch.

    Returns list of {ticker, orderbook_fp/orderbook} dicts.
    """
    limiter.acquire()
    resp = with_retry(lambda: kalshi_get_batch_orderbooks(client, tickers))
    resp.raise_for_status()
    return resp.json().get("orderbooks", [])


# --- Market discovery ---

def discover_markets(client: KalshiClient, limiter: RateLimiter
                     ) -> list[tuple[str, int, int]]:
    """Discover all open markets from the events API.

    Uses open_interest_fp (not the integer open_interest field) to identify
    markets with positions. The integer field returns 0 for all markets in
    nested event responses — this broke the old collector.

    Returns: list of (ticker, volume, open_interest) sorted by OI desc.
    """
    markets = []
    cursor = None

    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        data = api_get(client, limiter, "/events", params)

        for event in data.get("events", []):
            for m in event.get("markets", []):
                if m.get("status") not in ("open", "active"):
                    continue
                ticker = m.get("ticker")
                if not ticker:
                    continue
                vol = int(float(m.get("volume_fp", m.get("volume", "0") or "0")))
                oi = int(float(m.get("open_interest_fp",
                                     m.get("open_interest", "0") or "0")))
                if oi > 0:
                    markets.append((ticker, vol, oi))

        cursor = data.get("cursor")
        if not cursor or not data.get("events"):
            break

    # Sort by OI descending — highest priority markets first
    markets.sort(key=lambda x: x[2], reverse=True)
    return markets


# --- Orderbook parsing ---

def _parse_orderbook(data: dict) -> dict:
    """Normalize orderbook API response to standard format.

    The API returns two possible formats:
    - Old: {"orderbook": {"yes": [[price_cents, qty], ...], "no": [...]}}
    - New: {"orderbook_fp": {"yes_dollars": [["0.85", "100.00"], ...],
                             "no_dollars": [...]}}

    Returns: {"yes": [[price_cents, qty_int], ...], "no": [...]}
    """
    # Try new format first (orderbook_fp with dollar strings)
    ob_fp = data.get("orderbook_fp")
    if ob_fp:
        yes_raw = ob_fp.get("yes_dollars") or []
        no_raw = ob_fp.get("no_dollars") or []
        yes = [[round(float(p) * 100), int(float(q))] for p, q in yes_raw]
        no = [[round(float(p) * 100), int(float(q))] for p, q in no_raw]
        return {"yes": yes, "no": no}

    # Fall back to old format (integer cents)
    ob = data.get("orderbook") or {}
    return {"yes": ob.get("yes") or [], "no": ob.get("no") or []}


def _parse_snapshot(ticker: str, orderbook: dict, volume: int,
                    open_interest: int) -> tuple | None:
    """Extract best bid/ask, depth, and full levels from normalized orderbook.

    Orderbook format: {"yes": [[price_cents, qty], ...], "no": [...]}
    - yes levels = YES buy orders (bids). API returns ascending by price.
    - no levels = NO buy orders (buying NO at P = selling YES at 100-P).

    Best YES bid = highest YES bid price. Best NO bid = highest NO bid price.
    Best YES ask = 100 - best NO bid.

    Returns DB row tuple (including JSONB levels), or None if empty.
    """
    yes_levels = orderbook.get("yes") or []
    no_levels = orderbook.get("no") or []

    if not yes_levels and not no_levels:
        return None

    # Best bid = max price across levels (API may return in any order;
    # historically ascending, but max() is defensive either way).
    yes_bid = max((lv[0] for lv in yes_levels), default=None)
    best_no_bid = max((lv[0] for lv in no_levels), default=None)
    yes_ask = (100 - best_no_bid) if best_no_bid is not None else None

    yes_bid_depth = sum(level[1] for level in yes_levels) if yes_levels else 0
    yes_ask_depth = sum(level[1] for level in no_levels) if no_levels else 0

    now = datetime.now(timezone.utc)

    # JSONB: store full level arrays as [[price_cents, qty], ...]
    import json
    yes_json = json.dumps(yes_levels) if yes_levels else None
    no_json = json.dumps(no_levels) if no_levels else None

    return (
        ticker,
        now,
        yes_bid,
        yes_ask,
        yes_bid_depth,
        yes_ask_depth,
        volume,
        open_interest,
        "live",
        yes_json,
        no_json,
    )


# --- Size monitoring ---


def _check_table_size(conn) -> float:
    """Return table size in GB. Used to enforce MAX_TABLE_SIZE_GB."""
    cur = conn.cursor()
    cur.execute("""
        SELECT pg_total_relation_size('prediction_markets.kalshi_snapshots')
               / (1024.0 * 1024 * 1024)
    """)
    size_gb = cur.fetchone()[0]
    cur.close()
    return float(size_gb)


# --- Retention ---


def _cleanup_old_depth(conn) -> int:
    """NULL out JSONB depth columns older than DEPTH_RETENTION_DAYS.

    Keeps the bid/ask/volume/OI row forever — only the depth detail
    is dropped after the retention period. This bounds storage growth
    from the JSONB columns while preserving the basic snapshot data.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE prediction_markets.kalshi_snapshots
        SET yes_levels = NULL, no_levels = NULL
        WHERE yes_levels IS NOT NULL
          AND timestamp < now() - interval '%s days'
    """ % DEPTH_RETENTION_DAYS)
    cleaned = cur.rowcount
    conn.commit()
    cur.close()
    return cleaned


# --- DB operations ---

def _flush_snapshots(cur, buffer: list[tuple]) -> int:
    """Insert snapshots. ON CONFLICT DO NOTHING (dedup on natural key)."""
    if not buffer:
        return 0
    execute_values(cur, """
        INSERT INTO prediction_markets.kalshi_snapshots
            (ticker, timestamp, yes_bid, yes_ask, yes_bid_depth, yes_ask_depth,
             volume, open_interest, origin, yes_levels, no_levels)
        VALUES %s
        ON CONFLICT (ticker, timestamp) DO NOTHING
    """, buffer, page_size=500)
    return cur.rowcount


# --- Collection modes ---

def collect(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """Run one snapshot cycle: discover markets, collect all orderbooks."""
    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(DATASET_ID, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "collect"})

    cur = conn.cursor()

    try:
        # Check table size before collecting
        size_gb = _check_table_size(conn)
        if size_gb >= MAX_TABLE_SIZE_GB:
            print(f"Table size {size_gb:.1f}GB exceeds limit {MAX_TABLE_SIZE_GB}GB. "
                  f"Skipping collection. Run retention cleanup or raise limit.")
            logger.record_error(f"Table size {size_gb:.1f}GB >= {MAX_TABLE_SIZE_GB}GB")
            logger.finish("skipped")
            cur.close()
            return

        # Discover active markets
        print("Discovering active markets...")
        markets = discover_markets(client, limiter)
        print(f"  {len(markets):,} markets with open interest")

        if not markets:
            print("No markets found.")
            logger.finish("completed")
            cur.close()
            return

        total_fetched = 0
        total_inserted = 0
        total_empty = 0
        total_errors = 0
        batch_buffer = []
        start_wall = time.time()

        # Build lookup for volume/OI by ticker
        market_info = {ticker: (vol, oi) for ticker, vol, oi in markets}
        all_tickers = [ticker for ticker, _, _ in markets]

        # Process in batches of ORDERBOOK_BATCH_SIZE (100 tickers per API call)
        for batch_start in range(0, len(all_tickers), ORDERBOOK_BATCH_SIZE):
            if not RUNNING:
                print("Shutdown requested, stopping cycle.")
                break

            batch_tickers = all_tickers[batch_start:batch_start + ORDERBOOK_BATCH_SIZE]

            try:
                orderbooks = api_get_batch_orderbooks(
                    client, limiter, batch_tickers)
            except Exception as e:
                total_errors += len(batch_tickers)
                print(f"  Batch error at {batch_start}: {e}")
                continue

            for item in orderbooks:
                ticker = item.get("ticker", "")
                if not ticker:
                    continue
                orderbook = _parse_orderbook(item)
                vol, oi = market_info.get(ticker, (0, 0))
                row = _parse_snapshot(ticker, orderbook, vol, oi)

                if row:
                    batch_buffer.append(row)
                    total_fetched += 1
                else:
                    total_empty += 1

            # Flush periodically
            if len(batch_buffer) >= DB_BATCH_SIZE:
                inserted = _flush_snapshots(cur, batch_buffer)
                conn.commit()
                total_inserted += inserted
                batch_buffer = []

            # Progress reporting
            markets_done = batch_start + len(batch_tickers)
            if markets_done % 2000 < ORDERBOOK_BATCH_SIZE or \
               markets_done == len(all_tickers):
                elapsed = time.time() - start_wall
                rate = markets_done / elapsed if elapsed > 0 else 0
                eta_min = ((len(all_tickers) - markets_done) / rate / 60
                           if rate > 0 else 0)
                print(f"  {markets_done:,}/{len(all_tickers):,}: "
                      f"{total_fetched:,} with book, {total_empty:,} empty, "
                      f"{total_errors:,} errors, "
                      f"{rate:.1f}/s, ETA {eta_min:.1f}min")
                logger.record_progress(
                    rows_fetched=total_fetched,
                    rows_inserted=total_inserted,
                )

        # Flush remainder
        if batch_buffer:
            inserted = _flush_snapshots(cur, batch_buffer)
            conn.commit()
            total_inserted += inserted

        elapsed = time.time() - start_wall
        logger.record_progress(rows_fetched=total_fetched,
                               rows_inserted=total_inserted)
        logger.finish("completed")
        print(f"\nCycle complete: {total_fetched:,} snapshots, "
              f"{total_inserted:,} new, {total_empty:,} empty books, "
              f"{total_errors:,} errors in {elapsed / 60:.1f}min")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


def run(conn, client: KalshiClient, qps: float = DEFAULT_QPS,
        cycle_pause: int = DEFAULT_CYCLE_PAUSE):
    """Continuous collection loop. Designed for systemd Type=simple service.

    Cycles: collect all → pause → repeat.
    Runs depth retention cleanup (every cycle when table large, daily otherwise).
    Graduated throttling: doubles pause when table > 80% cap.
    Handles SIGTERM/SIGINT for graceful shutdown.

    Args:
        cycle_pause: Seconds between cycles. Default 1800 (30 min).
    """
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"{'=' * 60}")
    print(f"kalshi_snapshots continuous — {datetime.now(timezone.utc).isoformat()}")
    print(f"QPS: {qps}, cycle pause: {cycle_pause}s, PID: {os.getpid()}")
    print(f"Max table: {MAX_TABLE_SIZE_GB}GB, depth retention: {DEPTH_RETENTION_DAYS}d")
    print(f"{'=' * 60}")

    cycle = 0
    last_cleanup = None
    while RUNNING:
        cycle += 1
        print(f"\n--- Cycle {cycle} — {datetime.now(timezone.utc).isoformat()} ---")

        try:
            # Reconnect if needed (long-running service)
            try:
                conn.cursor().execute("SELECT 1")
            except Exception:
                print("DB connection lost, reconnecting...")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg2.connect(os.environ["CLAUDE_HUB_PG_DSN"])

            # Check table size for graduated throttle
            size_gb = _check_table_size(conn)
            size_pct = size_gb / MAX_TABLE_SIZE_GB
            if size_pct >= 0.8:
                print(f"  Table {size_gb:.1f}GB ({size_pct:.0%} of cap) — "
                      f"running cleanup before collecting")
                cleaned = _cleanup_old_depth(conn)
                if cleaned > 0:
                    print(f"  Cleaned {cleaned:,} rows of old depth data")
                    size_gb = _check_table_size(conn)
                    size_pct = size_gb / MAX_TABLE_SIZE_GB
                last_cleanup = datetime.now(timezone.utc).date()

            collect(conn, client, qps=qps)

            # Routine depth retention cleanup (daily when table is small)
            today = datetime.now(timezone.utc).date()
            if last_cleanup != today:
                cleaned = _cleanup_old_depth(conn)
                if cleaned > 0:
                    print(f"  Retention cleanup: NULLed depth on {cleaned:,} "
                          f"rows older than {DEPTH_RETENTION_DAYS} days")
                last_cleanup = today

        except Exception as e:
            print(f"Cycle {cycle} failed: {e}")
            for _ in range(30):
                if not RUNNING:
                    break
                time.sleep(1)
            continue

        # Graduated pause: double when table > 80% cap
        effective_pause = cycle_pause
        if size_pct >= 0.8:
            effective_pause = cycle_pause * 2
            print(f"  Throttled: pause {effective_pause}s "
                  f"(table at {size_pct:.0%} of cap)")

        if RUNNING:
            for _ in range(effective_pause):
                if not RUNNING:
                    break
                time.sleep(1)

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Collector stopped after {cycle} cycles")


def sync(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """One-shot sync: single collection cycle + health cache update."""
    print(f"{'=' * 60}")
    print(f"kalshi_snapshots sync — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 60}")

    collect(conn, client, qps=qps)

    # Update health cache
    print(f"\n--- Health cache ---")
    from data.health.check import update_cache
    updated = update_cache(conn)
    print(f"Updated health cache for {updated} dataset(s)")

    print(f"\n{'=' * 60}")
    print("Sync complete")


# --- Status ---

def status(conn):
    """Show snapshot data status and health."""
    cur = conn.cursor()

    # Origin breakdown
    cur.execute("""
        SELECT origin, count(*) FROM prediction_markets.kalshi_snapshots
        GROUP BY origin ORDER BY origin
    """)
    print("Kalshi snapshots by origin:")
    for origin, count in cur:
        print(f"  {origin}: {count:,}")

    # Freshness bounds
    cur.execute("""
        SELECT min(timestamp), max(timestamp)
        FROM prediction_markets.kalshi_snapshots
    """)
    min_t, max_t = cur.fetchone()
    print(f"\nTime range: {min_t} to {max_t}")

    # Health cache
    cur.execute("""
        SELECT row_count, max_freshness, last_computed
        FROM prediction_markets.dataset_health_cache
        WHERE dataset_id = %s
    """, (DATASET_ID,))
    row = cur.fetchone()
    if row:
        print(f"\nHealth cache:")
        print(f"  Rows: {row[0]:,}, Freshness: {row[1]}, Cached: {row[2]}")

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
        description="Kalshi snapshots — continuous orderbook collection",
    )
    parser.add_argument(
        "command",
        choices=["run", "collect", "sync", "status"],
        help="run: continuous loop | collect: one cycle | "
             "sync: one cycle + health | status: show stats",
    )
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS,
                        help=f"API queries per second (default: {DEFAULT_QPS})")
    parser.add_argument("--cycle-pause", type=int, default=DEFAULT_CYCLE_PAUSE,
                        help=f"Seconds between collection cycles "
                             f"(default: {DEFAULT_CYCLE_PAUSE}; "
                             f"auto-doubles when table > 80%% of {MAX_TABLE_SIZE_GB}GB cap)")
    args = parser.parse_args()

    conn = get_conn()

    try:
        if args.command == "status":
            status(conn)
        else:
            client = KalshiClient()
            if args.command == "run":
                run(conn, client, qps=args.qps, cycle_pause=args.cycle_pause)
            elif args.command == "collect":
                collect(conn, client, qps=args.qps)
            elif args.command == "sync":
                sync(conn, client, qps=args.qps)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
