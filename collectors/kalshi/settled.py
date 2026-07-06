#!/usr/bin/env python3
"""Kalshi settled — download settlement outcomes.

Implements the data system spec for kalshi_settled_events and kalshi_settled_markets:
- Append-only tables: new settlements accumulate over time
- Paginate GET /events?status=settled&with_nested_markets=true
- Upsert events and markets; derive market_structure per event
- volume from _fp fields (integer fields may be zeroed after settlement)
- Weekly schedule via systemd timer

Uses framework: RateLimiter, RunLogger, with_retry.

Usage:
    python -m collectors.kalshi.settled collect
    python -m collectors.kalshi.settled sync
    python -m collectors.kalshi.settled status
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2

# Add project root so imports work when run as script or module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from data.ingestion.rate_limiter import RateLimiter
from data.ingestion.retry import with_retry
from data.ingestion.run_logger import RunLogger, get_last_run

from trading.kalshi_client import KalshiClient

# --- Configuration ---

EVENTS_DATASET_ID = "kalshi_settled_events"
MARKETS_DATASET_ID = "kalshi_settled_markets"
SOURCE = "kalshi"
DEFAULT_QPS = 10.0
API_PAGE_SIZE = 200


# --- API layer ---

def kalshi_get(client: KalshiClient, path: str, params: dict | None = None):
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


# --- Market structure derivation ---

def derive_market_structure(markets: list[dict], mutually_exclusive: bool) -> str:
    """Derive event's market_structure from its markets' strike_types.

    For settled events, uses all markets (no status filter — all are settled).

    Categories:
    - standalone: single market or no strike pattern
    - exhaustive_partition: 'between' strikes or mutually_exclusive multi-market
    - monotone_threshold: directional threshold strikes (greater, less, etc.)
    """
    if len(markets) <= 1:
        return "standalone"

    strike_types = {m.get("strike_type") for m in markets if m.get("strike_type")}

    if "between" in strike_types:
        return "exhaustive_partition"
    if mutually_exclusive:
        return "exhaustive_partition"
    if strike_types & {"greater", "greater_or_equal", "less", "less_or_equal"}:
        return "monotone_threshold"
    return "standalone"


# --- Upsert helpers ---

def _upsert_event(cur, event: dict, markets: list[dict],
                  market_structure: str, now: datetime):
    """Upsert a single settled event row."""
    close_times = [m.get("close_time") for m in markets if m.get("close_time")]
    settled_at = max(close_times) if close_times else None

    cur.execute("""
        INSERT INTO prediction_markets.kalshi_settled_events
            (event_ticker, title, category, settled_at, num_markets,
             market_structure, series_ticker, mutually_exclusive,
             recorded_at, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'live')
        ON CONFLICT (event_ticker) DO UPDATE SET
            title = COALESCE(NULLIF(EXCLUDED.title, ''), kalshi_settled_events.title),
            category = COALESCE(NULLIF(EXCLUDED.category, ''), kalshi_settled_events.category),
            settled_at = COALESCE(EXCLUDED.settled_at, kalshi_settled_events.settled_at),
            num_markets = GREATEST(EXCLUDED.num_markets, kalshi_settled_events.num_markets),
            market_structure = COALESCE(EXCLUDED.market_structure, kalshi_settled_events.market_structure),
            series_ticker = COALESCE(EXCLUDED.series_ticker, kalshi_settled_events.series_ticker),
            mutually_exclusive = COALESCE(EXCLUDED.mutually_exclusive, kalshi_settled_events.mutually_exclusive),
            recorded_at = EXCLUDED.recorded_at,
            origin = EXCLUDED.origin
    """, (
        event.get("event_ticker"),
        (event.get("title") or "")[:500],
        event.get("category"),
        settled_at,
        len(markets),
        market_structure,
        event.get("series_ticker"),
        event.get("mutually_exclusive"),
        now,
    ))


def _upsert_market(cur, m: dict, now: datetime):
    """Upsert a single settled market row."""
    result = m.get("result")
    if result:
        result = result.lower()

    # volume_fp is more reliable than volume (which may be zeroed after settlement)
    volume = 0
    vol_fp = m.get("volume_fp")
    if vol_fp:
        try:
            volume = int(float(vol_fp))
        except (ValueError, TypeError):
            pass
    if volume == 0:
        volume = m.get("volume", 0) or 0

    close_time = m.get("close_time")

    cur.execute("""
        INSERT INTO prediction_markets.kalshi_settled_markets
            (ticker, event_ticker, title, result, volume, settled_at,
             close_time, strike_type, floor_strike,
             recorded_at, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'live')
        ON CONFLICT (ticker) DO UPDATE SET
            result = COALESCE(EXCLUDED.result, kalshi_settled_markets.result),
            volume = CASE WHEN EXCLUDED.volume > 0 THEN EXCLUDED.volume
                         ELSE kalshi_settled_markets.volume END,
            settled_at = COALESCE(EXCLUDED.settled_at, kalshi_settled_markets.settled_at),
            close_time = COALESCE(EXCLUDED.close_time, kalshi_settled_markets.close_time),
            strike_type = COALESCE(EXCLUDED.strike_type, kalshi_settled_markets.strike_type),
            floor_strike = COALESCE(EXCLUDED.floor_strike, kalshi_settled_markets.floor_strike),
            recorded_at = EXCLUDED.recorded_at,
            origin = EXCLUDED.origin
    """, (
        m.get("ticker"),
        m.get("event_ticker"),
        (m.get("title") or "")[:500],
        result,
        volume,
        close_time,  # settled_at = close_time for individual markets
        close_time,
        m.get("strike_type"),
        m.get("floor_strike"),
        now,
    ))


# --- Collection ---

def collect(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """One settled download cycle: paginate settled events API, upsert events + markets."""
    limiter = RateLimiter(SOURCE, qps, conn)
    logger = RunLogger(EVENTS_DATASET_ID, conn)
    run_id = logger.start()
    logger.set_metadata({"mode": "collect"})

    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    try:
        events_count = 0
        markets_count = 0
        last_reported_events = 0
        last_reported_markets = 0
        pages = 0
        cursor = None
        start_wall = time.time()

        while True:
            params = {
                "status": "settled",
                "with_nested_markets": "true",
                "limit": str(API_PAGE_SIZE),
            }
            if cursor:
                params["cursor"] = cursor

            data = api_get(client, limiter, "/events", params)
            events = data.get("events", [])
            pages += 1

            for event in events:
                markets = event.get("markets", [])

                structure = derive_market_structure(
                    markets, event.get("mutually_exclusive", False))

                _upsert_event(cur, event, markets, structure, now)
                events_count += 1

                for m in markets:
                    _upsert_market(cur, m, now)
                    markets_count += 1

            # Commit per page
            conn.commit()

            if pages % 25 == 0:
                elapsed = time.time() - start_wall
                print(f"  Page {pages}: {events_count:,} events, "
                      f"{markets_count:,} markets ({elapsed:.0f}s)")
                # record_progress takes deltas, not cumulative totals
                logger.record_progress(
                    rows_fetched=events_count - last_reported_events,
                    rows_inserted=markets_count - last_reported_markets,
                )
                last_reported_events = events_count
                last_reported_markets = markets_count

            cursor = data.get("cursor")
            if not cursor or not events:
                break

        elapsed = time.time() - start_wall
        # Final delta
        logger.record_progress(
            rows_fetched=events_count - last_reported_events,
            rows_inserted=markets_count - last_reported_markets,
        )
        logger.finish("completed")

        print(f"\nSettled download complete: {events_count:,} events, "
              f"{markets_count:,} markets in {pages} pages ({elapsed:.1f}s)")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


def sync(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """One-shot sync: collect + health cache update."""
    print(f"{'=' * 60}")
    print(f"kalshi settled sync — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 60}")

    collect(conn, client, qps=qps)

    # Update health cache for both datasets
    print(f"\n--- Health cache ---")
    from data.health.check import update_cache
    updated = update_cache(conn)
    print(f"Updated health cache for {updated} dataset(s)")

    print(f"\n{'=' * 60}")
    print("Sync complete")


# --- Status ---

def status(conn):
    """Show settled data status and health."""
    cur = conn.cursor()

    # Events summary
    cur.execute("""
        SELECT origin, count(*) FROM prediction_markets.kalshi_settled_events
        GROUP BY origin ORDER BY origin
    """)
    print("kalshi_settled_events by origin:")
    for origin, count in cur:
        print(f"  {origin}: {count:,}")

    cur.execute("""
        SELECT min(settled_at), max(settled_at)
        FROM prediction_markets.kalshi_settled_events
    """)
    min_t, max_t = cur.fetchone()
    print(f"  Settled range: {min_t} to {max_t}")

    cur.execute("""
        SELECT category, count(*) FROM prediction_markets.kalshi_settled_events
        WHERE category IS NOT NULL AND category != ''
        GROUP BY category ORDER BY count(*) DESC LIMIT 10
    """)
    print("\n  Top categories:")
    for cat, count in cur:
        print(f"    {cat}: {count:,}")

    cur.execute("""
        SELECT market_structure, count(*) FROM prediction_markets.kalshi_settled_events
        GROUP BY market_structure ORDER BY count(*) DESC
    """)
    print("\n  By market_structure:")
    for ms, count in cur:
        print(f"    {ms or 'NULL'}: {count:,}")

    # Markets summary
    print()
    cur.execute("""
        SELECT origin, count(*) FROM prediction_markets.kalshi_settled_markets
        GROUP BY origin ORDER BY origin
    """)
    print("kalshi_settled_markets by origin:")
    for origin, count in cur:
        print(f"  {origin}: {count:,}")

    cur.execute("""
        SELECT result, count(*) FROM prediction_markets.kalshi_settled_markets
        GROUP BY result ORDER BY count(*) DESC
    """)
    print("\n  By result:")
    for r, count in cur:
        print(f"    {r or 'NULL'}: {count:,}")

    cur.execute("""
        SELECT count(*) FILTER (WHERE volume > 0),
               count(*)
        FROM prediction_markets.kalshi_settled_markets
    """)
    with_vol, total = cur.fetchone()
    print(f"\n  Markets with volume > 0: {with_vol:,} / {total:,}")

    # Health cache
    for ds_id in (EVENTS_DATASET_ID, MARKETS_DATASET_ID):
        cur.execute("""
            SELECT row_count, max_freshness, last_computed
            FROM prediction_markets.dataset_health_cache
            WHERE dataset_id = %s
        """, (ds_id,))
        row = cur.fetchone()
        if row:
            print(f"\n{ds_id} health cache:")
            print(f"  Rows: {row[0]:,}, Freshness: {row[1]}, Cached: {row[2]}")

    # Last ingestion run
    last = get_last_run(EVENTS_DATASET_ID, conn)
    if last:
        print(f"\nLast ingestion run:")
        print(f"  Status: {last.status}")
        print(f"  Started: {last.started_at}")
        print(f"  Finished: {last.finished_at}")
        print(f"  Events fetched: {last.rows_fetched:,}")
        print(f"  Markets inserted: {last.rows_inserted:,}")
        if last.error_message:
            print(f"  Error: {last.error_message[:200]}")
    else:
        print("\nNo ingestion runs recorded yet.")

    # Health status
    from data.health.check import check_one
    for ds_id in (EVENTS_DATASET_ID, MARKETS_DATASET_ID):
        health = check_one(ds_id, conn)
        if health:
            print(f"\n{ds_id} health: {health.health_status}")

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
        description="Kalshi settled — download settlement outcomes",
    )
    parser.add_argument(
        "command",
        choices=["collect", "sync", "status"],
        help="collect: download settled data | sync: collect + health | status: show stats",
    )
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS,
                        help=f"API queries per second (default: {DEFAULT_QPS})")
    args = parser.parse_args()

    conn = get_conn()

    try:
        if args.command == "status":
            status(conn)
        else:
            client = KalshiClient()
            if args.command == "collect":
                collect(conn, client, qps=args.qps)
            elif args.command == "sync":
                sync(conn, client, qps=args.qps)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
