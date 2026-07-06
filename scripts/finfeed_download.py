#!/usr/bin/env python3
"""
Download all available OHLCV data from FinFeedAPI.

Stores in SQLite at data/finfeed/ohlcv.db

Usage:
    python scripts/finfeed_download.py                    # Download all exchanges
    python scripts/finfeed_download.py --exchange KALSHI  # Download specific exchange
    python scripts/finfeed_download.py --status           # Show download status
"""

import os
import argparse
import sqlite3
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
import time

load_dotenv(os.path.expanduser("~/.env"))

API_KEY = os.getenv("FINFEED_API_KEY")
BASE_URL = "https://api.prediction-markets.finfeedapi.com"
DB_PATH = Path(__file__).parent.parent / "data" / "finfeed" / "ohlcv.db"

EXCHANGES = ["POLYMARKET", "KALSHI", "MYRIAD", "MANIFOLD"]

# Known data availability (based on testing)
DATA_START = {
    "POLYMARKET": datetime(2025, 9, 1, tzinfo=timezone.utc),
    "KALSHI": datetime(2025, 9, 1, tzinfo=timezone.utc),
    "MYRIAD": datetime(2025, 9, 1, tzinfo=timezone.utc),
    "MANIFOLD": datetime(2025, 9, 1, tzinfo=timezone.utc),
}


def init_db():
    """Initialize SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            exchange TEXT NOT NULL,
            market_id TEXT NOT NULL,
            date TEXT NOT NULL,
            time_period_start TEXT,
            time_period_end TEXT,
            time_open TEXT,
            time_close TEXT,
            price_open REAL,
            price_high REAL,
            price_low REAL,
            price_close REAL,
            volume_traded REAL,
            trades_count INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (exchange, market_id, date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_log (
            exchange TEXT NOT NULL,
            date TEXT NOT NULL,
            records_count INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (exchange, date)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_exchange_date ON ohlcv(exchange, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_market ON ohlcv(market_id)")

    conn.commit()
    return conn


def get_downloaded_dates(conn, exchange: str) -> set:
    """Get set of already downloaded dates for an exchange."""
    cursor = conn.execute(
        "SELECT date FROM download_log WHERE exchange = ?",
        (exchange,)
    )
    return {row[0] for row in cursor.fetchall()}


def fetch_day(exchange: str, date: datetime) -> dict:
    """Fetch OHLCV for one day for an entire exchange."""
    start = date.strftime("%Y-%m-%dT00:00:00Z")
    end = (date + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    headers = {"Authorization": API_KEY}
    params = {
        "period_id": "1DAY",
        "time_start": start,
        "time_end": end
    }

    resp = requests.get(
        f"{BASE_URL}/v1/ohlcv/{exchange}/history",
        headers=headers,
        params=params,
        timeout=30
    )

    return {
        "date": date.strftime("%Y-%m-%d"),
        "status": resp.status_code,
        "records": resp.json() if resp.status_code == 200 else [],
        "error": resp.text if resp.status_code != 200 else None
    }


def store_day(conn, exchange: str, date_str: str, records: list):
    """Store a day's records in the database."""
    if not records:
        # Log that we checked this date but found no data
        conn.execute(
            "INSERT OR REPLACE INTO download_log (exchange, date, records_count) VALUES (?, ?, ?)",
            (exchange, date_str, 0)
        )
        conn.commit()
        return

    rows = []
    for rec in records:
        market_id = rec.get("market_id_exchange", rec.get("market_id", "unknown"))
        rows.append((
            exchange,
            market_id,
            date_str,
            rec.get("time_period_start"),
            rec.get("time_period_end"),
            rec.get("time_open"),
            rec.get("time_close"),
            rec.get("price_open"),
            rec.get("price_high"),
            rec.get("price_low"),
            rec.get("price_close"),
            rec.get("volume_traded"),
            rec.get("trades_count"),
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO ohlcv
        (exchange, market_id, date, time_period_start, time_period_end,
         time_open, time_close, price_open, price_high, price_low,
         price_close, volume_traded, trades_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    conn.execute(
        "INSERT OR REPLACE INTO download_log (exchange, date, records_count) VALUES (?, ?, ?)",
        (exchange, date_str, len(records))
    )

    conn.commit()


def download_exchange(conn, exchange: str, delay: float = 1.0):
    """Download all available data for an exchange."""
    print(f"\n{'='*60}")
    print(f"Downloading: {exchange}")
    print(f"{'='*60}")

    downloaded = get_downloaded_dates(conn, exchange)
    print(f"Already downloaded: {len(downloaded)} days")

    # Generate date range from data start to yesterday
    start_date = DATA_START.get(exchange, datetime(2025, 9, 1, tzinfo=timezone.utc))
    end_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

    # Build list of dates to fetch
    dates_to_fetch = []
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        if date_str not in downloaded:
            dates_to_fetch.append(current)
        current += timedelta(days=1)

    print(f"Dates to fetch: {len(dates_to_fetch)}")

    if not dates_to_fetch:
        print("Nothing to download - all caught up!")
        return

    total_records = 0
    consecutive_empty = 0

    for i, date in enumerate(dates_to_fetch):
        date_str = date.strftime("%Y-%m-%d")

        result = fetch_day(exchange, date)

        if result["status"] == 200:
            records = result["records"]
            store_day(conn, exchange, date_str, records)
            total_records += len(records)

            if len(records) > 0:
                consecutive_empty = 0
                print(f"  {date_str}: {len(records):,} records")
            else:
                consecutive_empty += 1
                print(f"  {date_str}: 0 records")

                # If we hit 5 consecutive empty days, we might be before data start
                if consecutive_empty >= 5:
                    print(f"  (5 consecutive empty days - may be before data availability)")

        elif result["status"] == 429:
            print(f"  {date_str}: RATE LIMITED - waiting 60s")
            time.sleep(60)
            # Retry
            result = fetch_day(exchange, date)
            if result["status"] == 200:
                records = result["records"]
                store_day(conn, exchange, date_str, records)
                total_records += len(records)
                print(f"  {date_str}: {len(records):,} records (retry)")
            else:
                print(f"  {date_str}: Still failing - skipping")

        else:
            print(f"  {date_str}: HTTP {result['status']} - {result['error'][:100] if result['error'] else 'unknown'}")

        # Progress update every 10 days
        if (i + 1) % 10 == 0:
            print(f"  ... {i+1}/{len(dates_to_fetch)} days processed, {total_records:,} total records")

        time.sleep(delay)

    print(f"\nCompleted {exchange}: {total_records:,} total records")


def show_status(conn):
    """Show download status for all exchanges."""
    print("\nDownload Status")
    print("="*60)

    for exchange in EXCHANGES:
        cursor = conn.execute("""
            SELECT
                COUNT(*) as days,
                SUM(records_count) as total_records,
                MIN(date) as first_date,
                MAX(date) as last_date
            FROM download_log
            WHERE exchange = ?
        """, (exchange,))

        row = cursor.fetchone()
        days, total, first, last = row

        if days and days > 0:
            print(f"\n{exchange}:")
            print(f"  Days downloaded: {days}")
            print(f"  Total records: {total:,}" if total else "  Total records: 0")
            print(f"  Date range: {first} to {last}")
        else:
            print(f"\n{exchange}: No data downloaded yet")

    # Overall stats
    cursor = conn.execute("SELECT COUNT(*) FROM ohlcv")
    total_rows = cursor.fetchone()[0]

    cursor = conn.execute("SELECT COUNT(DISTINCT market_id) FROM ohlcv")
    unique_markets = cursor.fetchone()[0]

    print(f"\n{'='*60}")
    print(f"Total OHLCV rows: {total_rows:,}")
    print(f"Unique markets: {unique_markets:,}")
    print(f"Database: {DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Download FinFeedAPI OHLCV data")
    parser.add_argument("--exchange", choices=EXCHANGES, help="Download specific exchange")
    parser.add_argument("--status", action="store_true", help="Show download status")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds)")
    args = parser.parse_args()

    conn = init_db()

    if args.status:
        show_status(conn)
        return

    if args.exchange:
        download_exchange(conn, args.exchange, args.delay)
    else:
        for exchange in EXCHANGES:
            download_exchange(conn, exchange, args.delay)

    show_status(conn)
    conn.close()


if __name__ == "__main__":
    main()
