#!/usr/bin/env python3
"""
Download historical candlestick data for settled Kalshi events.
Stores results in SQLite for model training.
"""

import os
import sys
import sqlite3
import requests
import time
import json
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import base64
import argparse

# Load credentials
KEY_ID = open(os.path.expanduser("~/.config/kalshi/key_id")).read().strip()
with open(os.path.expanduser("~/.config/kalshi/private_key.pem"), "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign_request(method, path, timestamp_str):
    msg = f"{timestamp_str}{method}{path}".encode()
    signature = PRIVATE_KEY.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()

def api_get(path, retries=3):
    for attempt in range(retries):
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sig = sign_request("GET", path, timestamp)
            headers = {
                "KALSHI-ACCESS-KEY": KEY_ID,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": sig,
            }
            r = requests.get(f"https://api.elections.kalshi.com{path}", headers=headers, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:  # Rate limited
                time.sleep(5)
                continue
            else:
                print(f"API error {r.status_code}: {r.text[:200]}")
                return None
        except Exception as e:
            print(f"Request failed: {e}")
            time.sleep(1)
    return None

def init_db(db_path):
    """Initialize the settled markets database."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settled_events (
        event_ticker TEXT PRIMARY KEY,
        title TEXT,
        category TEXT,
        settled_at TEXT,
        num_markets INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settled_markets (
        ticker TEXT PRIMARY KEY,
        event_ticker TEXT,
        title TEXT,
        result TEXT,  -- 'yes', 'no', or NULL
        volume INTEGER,
        settled_at TEXT,
        FOREIGN KEY (event_ticker) REFERENCES settled_events(event_ticker)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS historical_candles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TEXT NOT NULL,
        open_price REAL,
        close_price REAL,
        high_price REAL,
        low_price REAL,
        volume INTEGER,
        UNIQUE(ticker, period_end)
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_candles_ticker ON historical_candles(ticker)")

    conn.commit()
    return conn

def get_all_settled_events(limit=None):
    """Fetch all settled events from Kalshi API."""
    events = []
    cursor = None

    while True:
        path = "/trade-api/v2/events?status=settled&limit=100"
        if cursor:
            path += f"&cursor={cursor}"

        data = api_get(path)
        if not data:
            break

        batch = data.get('events', [])
        events.extend(batch)

        if limit and len(events) >= limit:
            events = events[:limit]
            break

        cursor = data.get('cursor')
        if not cursor:
            break

        time.sleep(0.3)  # Rate limiting

    return events

def get_event_markets(event_ticker):
    """Get all markets for a specific event."""
    data = api_get(f"/trade-api/v2/events/{event_ticker}")
    if data:
        return data.get('markets', [])
    return []

def get_market_candles(ticker, period=60):
    """Get historical candlesticks for a market (period in minutes)."""
    path = f"/trade-api/v2/markets/{ticker}/candlesticks?period_interval={period}"
    data = api_get(path)
    if data:
        return data.get('candlesticks', [])
    return []

def download_settled_data(db_path, categories=None, min_volume=0, max_events=None):
    """Download all settled event data."""
    conn = init_db(db_path)
    cur = conn.cursor()

    print("Fetching settled events...")
    events = get_all_settled_events(limit=max_events)
    print(f"Found {len(events)} settled events")

    if categories:
        events = [e for e in events if e.get('category') in categories]
        print(f"Filtered to {len(events)} events in categories: {categories}")

    total_markets = 0
    total_candles = 0

    for i, event in enumerate(events):
        ticker = event['event_ticker']

        # Check if already processed
        cur.execute("SELECT 1 FROM settled_events WHERE event_ticker = ?", (ticker,))
        if cur.fetchone():
            continue

        print(f"[{i+1}/{len(events)}] Processing {ticker}...")

        markets = get_event_markets(ticker)

        # Filter by volume
        high_vol_markets = [m for m in markets if m.get('volume', 0) >= min_volume * 100]

        if not high_vol_markets:
            continue

        # Store event
        cur.execute("""
        INSERT OR REPLACE INTO settled_events (event_ticker, title, category, num_markets)
        VALUES (?, ?, ?, ?)
        """, (ticker, event.get('title'), event.get('category'), len(markets)))

        # Process each market
        for market in high_vol_markets:
            mticker = market['ticker']

            cur.execute("""
            INSERT OR REPLACE INTO settled_markets (ticker, event_ticker, title, result, volume)
            VALUES (?, ?, ?, ?, ?)
            """, (mticker, ticker, market.get('title'), market.get('result'), market.get('volume')))

            # Get candlesticks
            candles = get_market_candles(mticker)

            for c in candles:
                try:
                    cur.execute("""
                    INSERT OR IGNORE INTO historical_candles
                    (ticker, period_end, open_price, close_price, high_price, low_price, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        mticker,
                        c.get('end_period_ts') or c.get('period_end'),
                        c.get('open', {}).get('yes_bid'),
                        c.get('close', {}).get('yes_bid'),
                        c.get('high', {}).get('yes_bid'),
                        c.get('low', {}).get('yes_bid'),
                        c.get('volume')
                    ))
                    total_candles += 1
                except Exception as e:
                    pass  # Skip duplicates

            total_markets += 1
            time.sleep(0.2)  # Rate limiting

        conn.commit()
        time.sleep(0.3)

    print(f"\nDownload complete!")
    print(f"  Events: {len(events)}")
    print(f"  Markets: {total_markets}")
    print(f"  Candles: {total_candles}")

    # Summary stats
    cur.execute("SELECT COUNT(DISTINCT event_ticker) FROM settled_events")
    print(f"\nDatabase now has:")
    print(f"  {cur.fetchone()[0]} settled events")
    cur.execute("SELECT COUNT(*) FROM settled_markets")
    print(f"  {cur.fetchone()[0]} settled markets")
    cur.execute("SELECT COUNT(*) FROM historical_candles")
    print(f"  {cur.fetchone()[0]} historical candles")

    conn.close()

def main():
    parser = argparse.ArgumentParser(description='Download Kalshi settled market data')
    parser.add_argument('--db', default='data/kalshi_settled.db', help='Database path')
    parser.add_argument('--categories', nargs='+', help='Filter by categories')
    parser.add_argument('--min-volume', type=int, default=100, help='Minimum volume in dollars')
    parser.add_argument('--max-events', type=int, help='Max events to process')

    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) or '.', exist_ok=True)
    download_settled_data(args.db, args.categories, args.min_volume, args.max_events)

if __name__ == '__main__':
    main()
