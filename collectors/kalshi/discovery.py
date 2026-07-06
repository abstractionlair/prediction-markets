#!/usr/bin/env python3
"""Kalshi discovery — event and market metadata refresh.

Implements the data system spec for kalshi_events and kalshi_markets:
- Metadata tables (overwritten, not appended) — shared "what's active" source
- Paginate GET /events?status=open&with_nested_markets=true
- Upsert events and markets; derive market_structure per event
- volume/open_interest from _fp fields (integer fields zeroed in nested responses)
- Timer-based: runs every 30 min via systemd timer

Other collectors (snapshots, candles) should query kalshi_markets
for active market discovery instead of doing inline API calls.

Uses framework: RateLimiter, RunLogger, with_retry.

Usage:
    python -m collectors.kalshi.discovery collect
    python -m collectors.kalshi.discovery sync
    python -m collectors.kalshi.discovery status
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

EVENTS_DATASET_ID = "kalshi_events"
MARKETS_DATASET_ID = "kalshi_markets"
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

def derive_market_structure(markets: list[dict]) -> str:
    """Derive event's market_structure from its markets' strike_types.

    Categories:
    - standalone: single market or no strike pattern
    - exhaustive_partition: 'between' strikes or mutually_exclusive multi-market
    - monotone_threshold: directional threshold strikes (greater, less, etc.)
    """
    active = [m for m in markets if m.get("status") in ("open", "active")]
    if len(active) <= 1:
        return "standalone"

    strike_types = {m.get("strike_type") for m in active if m.get("strike_type")}
    has_between = "between" in strike_types
    has_threshold = bool(strike_types & {"greater", "greater_or_equal", "less", "less_or_equal"})

    if has_between:
        return "exhaustive_partition"
    if has_threshold:
        return "monotone_threshold"
    return "standalone"


def derive_market_structure_with_mutex(markets: list[dict],
                                       mutually_exclusive: bool) -> str:
    """Like derive_market_structure but also checks mutually_exclusive flag."""
    active = [m for m in markets if m.get("status") in ("open", "active")]
    if len(active) <= 1:
        return "standalone"

    strike_types = {m.get("strike_type") for m in active if m.get("strike_type")}
    has_between = "between" in strike_types
    has_threshold = bool(strike_types & {"greater", "greater_or_equal", "less", "less_or_equal"})

    if has_between:
        return "exhaustive_partition"
    if mutually_exclusive:
        return "exhaustive_partition"
    if has_threshold:
        return "monotone_threshold"
    return "standalone"


# --- Upsert helpers ---

def _upsert_event(cur, event: dict, market_structure: str, now: datetime):
    """Upsert a single event row."""
    cur.execute("""
        INSERT INTO prediction_markets.kalshi_events
            (event_ticker, title, category, series_ticker, sub_title,
             strike_period, mutually_exclusive, market_structure,
             recorded_at, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'live')
        ON CONFLICT (event_ticker) DO UPDATE SET
            title = EXCLUDED.title,
            category = EXCLUDED.category,
            series_ticker = EXCLUDED.series_ticker,
            sub_title = EXCLUDED.sub_title,
            mutually_exclusive = EXCLUDED.mutually_exclusive,
            market_structure = EXCLUDED.market_structure,
            recorded_at = EXCLUDED.recorded_at,
            origin = EXCLUDED.origin,
            superseded_at = NULL
    """, (
        event.get("event_ticker"),
        (event.get("title") or "")[:200],
        event.get("category"),
        event.get("series_ticker"),
        event.get("sub_title"),
        event.get("strike_period"),
        event.get("mutually_exclusive"),
        market_structure,
        now,
    ))


def _upsert_market(cur, m: dict, now: datetime):
    """Upsert a single market row."""
    # Parse _fp fields for volume/OI (integer fields are zeroed in nested responses)
    volume = int(float(m.get("volume_fp", m.get("volume", "0") or "0")))
    open_interest = int(float(m.get("open_interest_fp",
                                     m.get("open_interest", "0") or "0")))

    # Parse timestamps
    close_time = m.get("close_time")
    expected_exp = m.get("expected_expiration_time")
    open_time = m.get("open_time")
    created_time = m.get("created_time")

    cur.execute("""
        INSERT INTO prediction_markets.kalshi_markets
            (ticker, event_ticker, title, status, close_time,
             volume, open_interest, rules_primary, rules_secondary,
             strike_type, floor_strike, market_type, expected_expiration_time,
             open_time, created_time, can_close_early, result,
             yes_sub_title, no_sub_title,
             recorded_at, origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, 'live')
        ON CONFLICT (ticker) DO UPDATE SET
            event_ticker = EXCLUDED.event_ticker,
            title = EXCLUDED.title,
            status = EXCLUDED.status,
            close_time = EXCLUDED.close_time,
            volume = EXCLUDED.volume,
            open_interest = EXCLUDED.open_interest,
            rules_primary = EXCLUDED.rules_primary,
            rules_secondary = EXCLUDED.rules_secondary,
            strike_type = EXCLUDED.strike_type,
            floor_strike = EXCLUDED.floor_strike,
            market_type = EXCLUDED.market_type,
            expected_expiration_time = EXCLUDED.expected_expiration_time,
            open_time = EXCLUDED.open_time,
            created_time = EXCLUDED.created_time,
            can_close_early = EXCLUDED.can_close_early,
            result = EXCLUDED.result,
            yes_sub_title = EXCLUDED.yes_sub_title,
            no_sub_title = EXCLUDED.no_sub_title,
            recorded_at = EXCLUDED.recorded_at,
            origin = EXCLUDED.origin,
            superseded_at = NULL
    """, (
        m.get("ticker"),
        m.get("event_ticker"),
        (m.get("title") or "")[:200],
        m.get("status"),
        close_time,
        volume,
        open_interest,
        m.get("rules_primary"),
        m.get("rules_secondary"),
        m.get("strike_type"),
        m.get("floor_strike"),
        m.get("market_type"),
        expected_exp,
        open_time,
        created_time,
        m.get("can_close_early"),
        m.get("result") or None,
        m.get("yes_sub_title"),
        m.get("no_sub_title"),
        now,
    ))


# --- Collection ---

def collect(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """One discovery cycle: paginate events API, upsert events + markets."""
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
                "status": "open",
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

                # Derive market_structure from constituent markets
                structure = derive_market_structure_with_mutex(
                    markets, event.get("mutually_exclusive", False))

                _upsert_event(cur, event, structure, now)
                events_count += 1

                for m in markets:
                    if m.get("status") not in ("open", "active"):
                        continue
                    _upsert_market(cur, m, now)
                    markets_count += 1

            # Commit per page
            conn.commit()

            if pages % 10 == 0:
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

        # Mark live rows not seen in this cycle as superseded.
        # Rows upserted in this cycle have recorded_at = now.
        # Rows with recorded_at < now were not in the API response → no longer active.
        # Legacy rows are left alone (they're historical preservation).
        cur.execute("""
            UPDATE prediction_markets.kalshi_events
            SET superseded_at = %s
            WHERE origin = 'live' AND superseded_at IS NULL AND recorded_at < %s
        """, (now, now))
        events_superseded = cur.rowcount

        cur.execute("""
            UPDATE prediction_markets.kalshi_markets
            SET superseded_at = %s
            WHERE origin = 'live' AND superseded_at IS NULL AND recorded_at < %s
        """, (now, now))
        markets_superseded = cur.rowcount
        conn.commit()

        elapsed = time.time() - start_wall
        # Final delta
        logger.record_progress(
            rows_fetched=events_count - last_reported_events,
            rows_inserted=markets_count - last_reported_markets,
        )
        logger.finish("completed")

        print(f"\nDiscovery complete: {events_count:,} events, "
              f"{markets_count:,} markets in {pages} pages ({elapsed:.1f}s)")
        if events_superseded or markets_superseded:
            print(f"Superseded: {events_superseded} events, "
                  f"{markets_superseded} markets (no longer in API)")

    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
    finally:
        cur.close()


def sync(conn, client: KalshiClient, qps: float = DEFAULT_QPS):
    """One-shot sync: collect + health cache update."""
    print(f"{'=' * 60}")
    print(f"kalshi discovery sync — {datetime.now(timezone.utc).isoformat()}")
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
    """Show discovery data status and health."""
    cur = conn.cursor()

    # Events summary
    cur.execute("""
        SELECT origin, count(*) FROM prediction_markets.kalshi_events
        GROUP BY origin ORDER BY origin
    """)
    print("kalshi_events by origin:")
    for origin, count in cur:
        print(f"  {origin}: {count:,}")

    cur.execute("""
        SELECT min(recorded_at), max(recorded_at)
        FROM prediction_markets.kalshi_events
    """)
    min_t, max_t = cur.fetchone()
    print(f"  Recorded range: {min_t} to {max_t}")

    cur.execute("""
        SELECT count(*) FILTER (WHERE superseded_at IS NULL) AS current,
               count(*) FILTER (WHERE superseded_at IS NOT NULL) AS superseded
        FROM prediction_markets.kalshi_events WHERE origin = 'live'
    """)
    curr, sup = cur.fetchone()
    print(f"  Live: {curr:,} current, {sup:,} superseded")

    cur.execute("""
        SELECT category, count(*) FROM prediction_markets.kalshi_events
        GROUP BY category ORDER BY count(*) DESC
    """)
    print("\n  By category:")
    for cat, count in cur:
        print(f"    {cat}: {count:,}")

    # Markets summary
    print()
    cur.execute("""
        SELECT origin, count(*) FROM prediction_markets.kalshi_markets
        GROUP BY origin ORDER BY origin
    """)
    print("kalshi_markets by origin:")
    for origin, count in cur:
        print(f"  {origin}: {count:,}")

    cur.execute("""
        SELECT status, count(*) FROM prediction_markets.kalshi_markets
        GROUP BY status ORDER BY count(*) DESC
    """)
    print("\n  By status:")
    for st, count in cur:
        print(f"    {st}: {count:,}")

    cur.execute("""
        SELECT count(*) FILTER (WHERE origin = 'live' AND superseded_at IS NULL) AS current,
               count(*) FILTER (WHERE origin = 'live' AND superseded_at IS NOT NULL) AS superseded,
               count(*) FILTER (WHERE open_interest > 0 AND origin = 'live' AND superseded_at IS NULL) AS with_oi
        FROM prediction_markets.kalshi_markets
    """)
    current, superseded, with_oi = cur.fetchone()
    print(f"\n  Live markets: {current:,} current, {superseded:,} superseded, {with_oi:,} with OI > 0")

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
        description="Kalshi discovery — event and market metadata refresh",
    )
    parser.add_argument(
        "command",
        choices=["collect", "sync", "status"],
        help="collect: one refresh cycle | sync: collect + health | status: show stats",
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
