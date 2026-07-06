#!/usr/bin/env python3
"""
Polymarket Data Collector

Collects ALL non-sports markets with pagination.
Collects orderbook snapshots at configurable intervals.

Usage:
    python polymarket_collector.py --interval 300
    python polymarket_collector.py --status
"""

import argparse
import json
import os
import time
import signal
from datetime import datetime
from pathlib import Path

import psycopg2
import requests

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

RUNNING = True


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


def signal_handler(_signum, _frame):
    global RUNNING
    print(f"\n[{datetime.now().isoformat()}] Shutting down...")
    RUNNING = False


def get_connection():
    """Get a PostgreSQL connection with search_path set."""
    conn = psycopg2.connect(get_pg_dsn())
    with conn.cursor() as cur:
        cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


def ensure_connection(conn):
    """Test connection and reconnect if needed."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        print(f"[{datetime.now().isoformat()}] DB connection lost, reconnecting...")
        try:
            conn.close()
        except Exception:
            pass
        return get_connection()


def discover_markets(conn) -> list[tuple[str, str]]:
    """Find all non-sports markets using pagination.

    Fetches markets from the API, upserts them into the DB, and returns
    only a list of (market_id, token_id) tuples to minimize memory usage.
    """
    print(f"[{datetime.now().isoformat()}] Discovering all markets with pagination...")

    # Sports keywords to exclude
    sports_keywords = ['nfl', 'nba', 'mlb', 'nhl', 'soccer', 'football', 'basketball',
                       'tennis', 'ufc', 'boxing', 'golf', 'f1', 'nascar', 'formula 1',
                       'premier league', 'champions league', 'world cup', 'super bowl',
                       'playoffs', 'stanley cup', 'world series']

    market_keys = []  # (market_id, token_id) tuples
    top_markets = []  # Keep only top 5 for logging
    total_fetched = 0
    sports_excluded = 0
    no_clob = 0
    offset = 0
    page_size = 500

    db_cursor = conn.cursor()
    consecutive_errors = 0
    while True:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "limit": page_size, "offset": offset},
                timeout=30
            )

            if resp.status_code == 429:
                wait_time = (2 ** min(consecutive_errors, 5)) + (time.time() % 1)
                print(f"  Rate limited at offset {offset}, waiting {wait_time:.1f}s")
                time.sleep(wait_time)
                consecutive_errors += 1
                continue

            resp.raise_for_status()
            page = resp.json()
            consecutive_errors = 0  # Reset on success

            if not page:
                break

            total_fetched += len(page)

            # Filter and upsert each market in this page, then discard the raw data
            for m in page:
                question = m.get('question', '')

                # Skip sports
                if any(kw in question.lower() for kw in sports_keywords):
                    sports_excluded += 1
                    continue

                if not m.get('active'):
                    continue

                # Need CLOB token for orderbook
                clob_ids = m.get('clobTokenIds')
                if not clob_ids:
                    no_clob += 1
                    continue
                try:
                    token_ids = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                    if not token_ids:
                        no_clob += 1
                        continue
                except (json.JSONDecodeError, TypeError, ValueError):
                    no_clob += 1
                    continue

                market_id = m.get('conditionId')
                token_id = token_ids[0]
                end_date = m.get('endDate')
                volume = float(m.get('volume', 0) or 0)
                liquidity = float(m.get('liquidity', 0) or 0)

                # Upsert to DB immediately
                db_cursor.execute("""
                    INSERT INTO polymarket_markets (market_id, token_id, question, outcome, end_date, volume, liquidity, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (market_id) DO UPDATE SET
                        token_id = EXCLUDED.token_id,
                        question = EXCLUDED.question,
                        outcome = EXCLUDED.outcome,
                        end_date = EXCLUDED.end_date,
                        volume = EXCLUDED.volume,
                        liquidity = EXCLUDED.liquidity,
                        is_active = TRUE
                """, (market_id, token_id, question, 'Yes', end_date, volume, liquidity))

                market_keys.append((market_id, token_id))

                # Track top 5 by volume for logging
                if len(top_markets) < 5:
                    top_markets.append((volume, question))
                    top_markets.sort(key=lambda x: x[0], reverse=True)
                elif volume > top_markets[-1][0]:
                    top_markets[-1] = (volume, question)
                    top_markets.sort(key=lambda x: x[0], reverse=True)

            print(f"  Fetched {len(page)} markets (offset={offset}, total so far={total_fetched})")

            if len(page) < page_size:
                break

            offset += page_size
            conn.commit()  # Periodic commit during discovery
            time.sleep(0.1)  # Rate limiting

        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 5:
                print("  Too many consecutive errors, stopping discovery")
                break
            time.sleep(2 ** consecutive_errors)

    conn.commit()

    print(f"[{datetime.now().isoformat()}] After filtering: {len(market_keys)} markets")
    print(f"  Excluded: {sports_excluded} sports, {no_clob} without CLOB")

    # Log top 5 by volume
    for vol, question in top_markets:
        print(f"  Top: ${vol/1e6:.1f}M - {question[:50]}")

    return market_keys


def get_orderbook(token_id: str, max_retries: int = 3) -> dict:
    """Fetch orderbook for a token with exponential backoff."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)

            if resp.status_code == 429:
                wait_time = (2 ** attempt) + (time.time() % 1)
                time.sleep(wait_time)
                continue

            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return {}
    return {}


def build_volume_cache(conn) -> dict[str, float]:
    """Build a cache of {market_id: volume} from polymarket_markets.

    Called once after discover_markets() and refreshed every 30 minutes,
    avoiding per-market SELECTs during each snapshot cycle.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT market_id, volume FROM polymarket_markets")
    cache = {}
    for row in cursor.fetchall():
        cache[row[0]] = row[1]
    return cache


def collect_snapshot(conn, market_id: str, token_id: str,
                     volume_cache: dict[str, float] | None = None) -> bool:
    """Collect single market snapshot. Takes only market_id and token_id strings."""
    book = get_orderbook(token_id)
    if not book:
        return False

    bids = book.get('bids', [])
    asks = book.get('asks', [])

    # API returns bids ascending, asks descending — best prices are last
    best_bid = float(bids[-1]['price']) if bids else None
    best_ask = float(asks[-1]['price']) if asks else None

    mid_price = None
    spread_bps = None
    if best_bid and best_ask:
        mid_price = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid_price * 10000 if mid_price > 0 else None

    bid_depth = sum(float(b.get('size', 0)) for b in bids)
    ask_depth = sum(float(a.get('size', 0)) for a in asks)

    # Look up volume from in-memory cache (populated at discovery time)
    if volume_cache and market_id in volume_cache:
        volume = volume_cache[market_id]
    else:
        volume = None

    db_cursor = conn.cursor()
    db_cursor.execute("""
        INSERT INTO polymarket_snapshots (market_id, timestamp, best_bid, best_ask, mid_price,
                               spread_bps, bid_depth, ask_depth, volume_24h)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (market_id, datetime.now().isoformat(), best_bid, best_ask,
          mid_price, spread_bps, bid_depth, ask_depth, volume))
    return True


def filter_active_market_keys(conn, market_keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Filter to only markets that haven't ended yet, using end_date from DB."""
    if not market_keys:
        return []

    now = datetime.now().isoformat()
    cursor = conn.cursor()

    # Bulk fetch all end_dates in one query
    market_ids = [mk[0] for mk in market_keys]
    cursor.execute(
        "SELECT market_id, end_date FROM polymarket_markets WHERE market_id = ANY(%s)",
        (market_ids,)
    )
    end_dates = {row[0]: row[1] for row in cursor.fetchall()}

    active = []
    ended = 0
    for market_id, token_id in market_keys:
        end_date = end_dates.get(market_id)
        if end_date and end_date < now:
            ended += 1
            continue
        active.append((market_id, token_id))
    if ended > 0:
        print(f"  Filtered out {ended} ended markets, {len(active)} active remaining")
    return active


def update_market_status(conn, _active_market_ids: set[str]):
    """Mark markets as inactive if they're ended or no longer in API."""
    cursor = conn.cursor()
    # Mark ended markets as inactive
    cursor.execute("""
        UPDATE polymarket_markets SET is_active = FALSE
        WHERE is_active = TRUE AND end_date IS NOT NULL AND end_date < NOW()::text
    """)
    ended_count = cursor.rowcount
    if ended_count > 0:
        print(f"  Marked {ended_count} ended markets as inactive")
    conn.commit()


def run_collector(interval: int):
    """Main collection loop."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    conn = get_connection()

    # discover_markets now upserts to DB and returns only (market_id, token_id) tuples
    market_keys = discover_markets(conn)

    # Filter to active markets only
    market_keys = filter_active_market_keys(conn, market_keys)
    update_market_status(conn, {mk[0] for mk in market_keys})

    # Build volume cache to avoid per-market SELECTs during snapshot collection
    volume_cache = build_volume_cache(conn)
    print(f"[{datetime.now().isoformat()}] Cached volume for {len(volume_cache)} markets")

    print(f"[{datetime.now().isoformat()}] Starting collection: {len(market_keys)} active markets, {interval}s interval")

    refresh_interval_sec = 1800  # 30 minutes
    refresh_counter = 0
    while RUNNING:
        conn = ensure_connection(conn)
        start = time.time()
        success = 0

        for i, (market_id, token_id) in enumerate(market_keys):
            if not RUNNING:
                break
            if collect_snapshot(conn, market_id, token_id, volume_cache):
                success += 1
            # Commit every 50 snapshots to keep transactions short.
            # Avoids idle-in-transaction for minutes (blocks VACUUM, causes bloat).
            if (i + 1) % 50 == 0:
                conn.commit()
            time.sleep(0.05)  # Rate limiting

        conn.commit()
        elapsed = time.time() - start
        print(f"[{datetime.now().isoformat()}] Collected {success}/{len(market_keys)} snapshots in {elapsed:.1f}s")

        # Refresh market list every 30 minutes
        refresh_counter += 1
        if refresh_counter >= refresh_interval_sec // interval:
            market_keys = discover_markets(conn)
            # Filter and update status
            market_keys = filter_active_market_keys(conn, market_keys)
            update_market_status(conn, {mk[0] for mk in market_keys})
            volume_cache = build_volume_cache(conn)
            print(f"[{datetime.now().isoformat()}] Refreshed: {len(market_keys)} active markets, "
                  f"volume cache: {len(volume_cache)} entries")
            refresh_counter = 0

        # Sleep until next interval
        sleep_time = max(0, interval - elapsed)

        for _ in range(int(sleep_time)):
            if not RUNNING:
                break
            time.sleep(1)

    conn.close()
    print(f"[{datetime.now().isoformat()}] Collector stopped")


def show_status():
    """Show collection statistics."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(DISTINCT market_id), COUNT(*) FROM polymarket_snapshots")
    row = cursor.fetchone()
    markets_with_data, snapshots = row if row else (0, 0)

    cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM polymarket_snapshots")
    row = cursor.fetchone()
    start, end = row if row else (None, None)

    cursor.execute("SELECT COUNT(*) FROM polymarket_markets")
    row = cursor.fetchone()
    total_markets = row[0] if row else 0

    cursor.execute("SELECT COUNT(*) FROM polymarket_markets WHERE end_date IS NOT NULL AND end_date < NOW()::text")
    row = cursor.fetchone()
    ended_markets = row[0] if row else 0

    cursor.execute("SELECT COUNT(*) FROM polymarket_markets WHERE end_date IS NULL OR end_date >= NOW()::text")
    row = cursor.fetchone()
    active_markets = row[0] if row else 0

    print("Polymarket Collector Status:")
    print(f"  Total markets in DB: {total_markets}")
    print(f"    Active (end_date >= now): {active_markets}")
    print(f"    Ended (end_date < now): {ended_markets}")
    print(f"  Markets with snapshots: {markets_with_data}")
    print(f"  Total snapshots: {snapshots:,}")
    print(f"  Time range: {start} to {end}")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_collector(args.interval)
