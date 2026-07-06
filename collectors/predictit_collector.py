#!/usr/bin/env python3
"""
PredictIt Data Collector

Collects market data from PredictIt's public API.
No authentication required. Uses PostgreSQL for storage.

Usage:
    python predictit_collector.py --interval 300
    python predictit_collector.py --status
"""

import argparse
import os
import time
import signal
from datetime import datetime
from pathlib import Path

import psycopg2
import requests

API_URL = "https://www.predictit.org/api/marketdata/all"

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


def signal_handler(signum, frame):
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


def fetch_all_markets() -> dict:
    """Fetch all markets from PredictIt API."""
    resp = requests.get(API_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def collect_snapshot(conn) -> tuple[int, int]:
    """Collect snapshot of all markets."""
    data = fetch_all_markets()
    markets = data.get('markets', [])

    cursor = conn.cursor()
    timestamp = datetime.now().isoformat()
    contracts_collected = 0
    markets_collected = 0

    for market in markets:
        market_id = market.get('id')
        if not market_id:
            continue

        # Upsert market
        cursor.execute("""
            INSERT INTO predictit_markets (market_id, name, short_name, url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (market_id) DO UPDATE SET
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                url = EXCLUDED.url
        """, (market_id, market.get('name'), market.get('shortName'), market.get('url')))
        markets_collected += 1

        # Process contracts
        for contract in market.get('contracts', []):
            contract_id = contract.get('id')
            if not contract_id:
                continue

            # Upsert contract
            cursor.execute("""
                INSERT INTO predictit_contracts (contract_id, market_id, name, short_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (contract_id) DO UPDATE SET
                    market_id = EXCLUDED.market_id,
                    name = EXCLUDED.name,
                    short_name = EXCLUDED.short_name
            """, (contract_id, market_id, contract.get('name'), contract.get('shortName')))

            # Extract prices (PredictIt uses 0-1 scale)
            last_trade = contract.get('lastTradePrice')
            buy_yes = contract.get('bestBuyYesCost')
            sell_yes = contract.get('bestSellYesCost')
            buy_no = contract.get('bestBuyNoCost')
            sell_no = contract.get('bestSellNoCost')
            last_close = contract.get('lastClosePrice')

            # Calculate spread (buy_yes is what you pay, sell_yes is what you get)
            spread_bps = None
            if buy_yes and sell_yes and buy_yes > 0:
                mid = (buy_yes + sell_yes) / 2
                spread_bps = (buy_yes - sell_yes) / mid * 10000 if mid > 0 else None

            cursor.execute("""
                INSERT INTO predictit_snapshots (contract_id, timestamp, last_trade_price, best_buy_yes,
                                                 best_sell_yes, best_buy_no, best_sell_no, last_close_price, spread_bps)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (contract_id, timestamp, last_trade, buy_yes, sell_yes, buy_no, sell_no, last_close, spread_bps))
            contracts_collected += 1

    conn.commit()
    return markets_collected, contracts_collected


def run_collector(interval: int):
    """Main collection loop."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    conn = get_connection()

    # Initial fetch to show stats
    markets, contracts = collect_snapshot(conn)
    print(f"[{datetime.now().isoformat()}] PredictIt: {markets} markets, {contracts} contracts")
    print(f"[{datetime.now().isoformat()}] Starting collection with {interval}s interval")

    while RUNNING:
        conn = ensure_connection(conn)
        start = time.time()

        try:
            markets, contracts = collect_snapshot(conn)
            print(f"[{datetime.now().isoformat()}] Collected {contracts} contract snapshots")
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Error: {e}")

        elapsed = time.time() - start
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

    cursor.execute("SELECT COUNT(*) FROM predictit_markets")
    markets = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM predictit_contracts")
    contracts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM predictit_snapshots")
    snapshots = cursor.fetchone()[0]

    cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM predictit_snapshots")
    start, end = cursor.fetchone()

    # Sample some markets
    cursor.execute("SELECT name FROM predictit_markets LIMIT 5")
    sample_markets = [row[0][:50] for row in cursor.fetchall()]

    print(f"PredictIt Collector Status:")
    print(f"  Markets: {markets}")
    print(f"  Contracts: {contracts}")
    print(f"  Snapshots: {snapshots:,}")
    print(f"  Time range: {start} to {end}")
    print(f"  Sample markets:")
    for m in sample_markets:
        print(f"    - {m}")

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
