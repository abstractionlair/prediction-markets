#!/usr/bin/env python3
"""
Backfill historical OHLCV data from FinFeedAPI by iterating day-by-day.

Usage:
    python scripts/finfeed_backfill.py --exchange POLYMARKET --days 30 --test
    python scripts/finfeed_backfill.py --exchange POLYMARKET --days 365
"""

import os
import argparse
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import time

load_dotenv(os.path.expanduser("~/.env"))

API_KEY = os.getenv("FINFEED_API_KEY")
BASE_URL = "https://api.prediction-markets.finfeedapi.com"


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
        params=params
    )

    return {
        "date": date.strftime("%Y-%m-%d"),
        "status": resp.status_code,
        "records": len(resp.json()) if resp.status_code == 200 else 0,
        "data": resp.json() if resp.status_code == 200 else None,
        "error": resp.text if resp.status_code != 200 else None
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill OHLCV from FinFeedAPI")
    parser.add_argument("--exchange", default="POLYMARKET", help="Exchange ID")
    parser.add_argument("--days", type=int, default=7, help="Days to go back")
    parser.add_argument("--test", action="store_true", help="Test mode - just probe dates")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests (seconds)")
    args = parser.parse_args()

    print(f"Exchange: {args.exchange}")
    print(f"Days back: {args.days}")
    print(f"Mode: {'TEST' if args.test else 'FULL'}")
    print()

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    total_records = 0
    days_with_data = 0
    first_empty = None

    for i in range(args.days):
        date = today - timedelta(days=i+1)
        result = fetch_day(args.exchange, date)

        if result["status"] == 200:
            if result["records"] > 0:
                days_with_data += 1
                total_records += result["records"]
                print(f"{result['date']}: {result['records']} records")

                if args.test and result["data"]:
                    # Show sample record
                    sample = result["data"][0]
                    print(f"  Sample: {sample.get('exchange_symbol', 'no symbol')[:40]}")
            else:
                if first_empty is None:
                    first_empty = result["date"]
                print(f"{result['date']}: 0 records")
        elif result["status"] == 429:
            print(f"{result['date']}: RATE LIMITED - stopping")
            break
        else:
            print(f"{result['date']}: HTTP {result['status']}")
            if result["error"]:
                print(f"  Error: {result['error'][:100]}")

        time.sleep(args.delay)

    print()
    print("="*50)
    print(f"Days scanned: {args.days}")
    print(f"Days with data: {days_with_data}")
    print(f"Total records: {total_records}")
    if first_empty:
        print(f"First empty date: {first_empty}")

    if days_with_data > 0:
        print(f"Avg records/day: {total_records / days_with_data:.0f}")

        # Estimate costs
        requests_per_year = 365
        cost_first_1000 = 5  # $5 per 1000
        cost_after = 1  # $1 per 1000

        print()
        print("Cost estimate (1 year daily backfill):")
        print(f"  Requests: {requests_per_year}")
        print(f"  Cost: ~${requests_per_year / 1000 * cost_first_1000:.2f}")


if __name__ == "__main__":
    main()
