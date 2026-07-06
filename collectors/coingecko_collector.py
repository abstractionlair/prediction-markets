#!/usr/bin/env python3
"""
CoinGecko Crypto Benchmark Data Collector

Fetches and stores daily cryptocurrency price data from CoinGecko's free API
for the major coins traded on Kalshi (BTC, ETH, SOL, DOGE, XRP).

CoinGecko free API: no key needed, 30 calls/min rate limit.

Usage:
    python coingecko_collector.py                       # Fetch all configured coins
    python coingecko_collector.py --coins bitcoin ethereum  # Fetch specific coins
    python coingecko_collector.py --status              # Show collection statistics
    python coingecko_collector.py --update              # Incremental update
    python coingecko_collector.py --days 365            # Fetch last N days (default: max)
    python coingecko_collector.py --list                # List configured coins
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

def _get_api_key() -> str | None:
    """Load CoinGecko demo API key from environment or ~/.env."""
    key = os.environ.get("COINGECKO_API_KEY")
    if key:
        return key
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("COINGECKO_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None

# CoinGecko coin IDs mapped to Kalshi series
# Format: (coingecko_id, symbol, description, kalshi_series)
CONFIGURED_COINS = [
    ("bitcoin", "BTC", "Bitcoin", "KXBTC/KXBTCD"),
    ("ethereum", "ETH", "Ethereum", "KXETH/KXETHD"),
    ("solana", "SOL", "Solana", "KXSOL/KXSOLD"),
    ("dogecoin", "DOGE", "Dogecoin", "KXDOGE/KXDOGED"),
    ("ripple", "XRP", "XRP", "KXXRP/KXXRPD"),
    ("shiba-inu", "SHIB", "Shiba Inu", "KXSHIBA"),
]

# Rate limiting: CoinGecko free API allows ~30 calls/min
RATE_LIMIT_DELAY = 2.5  # seconds between requests (conservative)


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


def get_connection():
    """Get a PostgreSQL connection with search_path set."""
    conn = psycopg2.connect(get_pg_dsn())
    with conn.cursor() as cur:
        cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


def fetch_market_chart(coin_id: str, vs_currency: str = "usd",
                       days: int | str = "max") -> list[dict]:
    """Fetch historical price data from CoinGecko.

    Args:
        coin_id: CoinGecko coin identifier (e.g., 'bitcoin')
        vs_currency: Quote currency (default: 'usd')
        days: Number of days of history, or 'max' for all available

    Returns:
        List of dicts with date, price, market_cap, total_volume
    """
    url = f"{COINGECKO_BASE_URL}/coins/{coin_id}/market_chart"
    params: dict[str, str | int] = {
        "vs_currency": vs_currency,
        "days": days,
        "interval": "daily",
    }
    api_key = _get_api_key()
    if api_key:
        params["x_cg_demo_api_key"] = api_key

    max_retries = 3
    backoff_schedule = [30, 60, 120]  # seconds

    try:
        resp = requests.get(url, params=params, timeout=60)

        for attempt in range(max_retries):
            if resp.status_code != 429:
                break
            wait_time = backoff_schedule[attempt]
            print(f"  Rate limited (attempt {attempt + 1}/{max_retries}). Waiting {wait_time}s...")
            time.sleep(wait_time)
            resp = requests.get(url, params=params, timeout=60)

        if resp.status_code != 200:
            print(f"  WARNING: HTTP {resp.status_code} fetching {coin_id}")
            return []

        data = resp.json()

        prices = data.get("prices", [])
        market_caps = data.get("market_caps", [])
        volumes = data.get("total_volumes", [])

        # Build daily records from timestamp arrays
        # CoinGecko returns [timestamp_ms, value] pairs
        records = []
        for i, (ts_ms, price) in enumerate(prices):
            date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            record = {
                "date": date,
                "price": price,
                "market_cap": market_caps[i][1] if i < len(market_caps) else None,
                "total_volume": volumes[i][1] if i < len(volumes) else None,
            }
            records.append(record)

        # Deduplicate by date (CoinGecko sometimes returns overlapping days)
        seen = set()
        deduped = []
        for r in records:
            if r["date"] not in seen:
                seen.add(r["date"])
                deduped.append(r)

        return deduped

    except requests.RequestException as e:
        print(f"  WARNING: Request error for {coin_id}: {e}")
        return []


def collect_coin(conn, coin_id: str, symbol: str,
                 description: str, kalshi_series: str,
                 days: int | str = "max", incremental: bool = False) -> int:
    """Collect data for a single cryptocurrency.

    Returns number of new observations inserted.
    """
    cursor = conn.cursor()

    # For incremental updates, calculate days since last observation
    if incremental:
        cursor.execute(
            "SELECT MAX(date) FROM coingecko_observations WHERE series_id = %s",
            (coin_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            last_date = datetime.strptime(str(row[0]), "%Y-%m-%d")
            days_since = (datetime.now(timezone.utc) - last_date.replace(tzinfo=timezone.utc)).days + 1
            if days_since <= 1:
                print(f"    Already up to date")
                return 0
            days = min(days_since + 1, 365)  # CoinGecko daily interval max
            print(f"    Incremental: last {days} days (from {row[0]})")

    # Fetch data
    records = fetch_market_chart(coin_id, days=days)

    if not records:
        return 0

    # Upsert series metadata
    cursor.execute("""
        INSERT INTO coingecko_series
        (series_id, description, frequency, units, last_updated, symbol, kalshi_series)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET
            description = EXCLUDED.description,
            frequency = EXCLUDED.frequency,
            units = EXCLUDED.units,
            last_updated = EXCLUDED.last_updated,
            symbol = EXCLUDED.symbol,
            kalshi_series = EXCLUDED.kalshi_series
    """, (
        coin_id,
        description,
        "D",
        "USD",
        datetime.now(timezone.utc).isoformat(),
        symbol,
        kalshi_series,
    ))

    # Upsert observations and market data
    inserted = 0
    for rec in records:
        cursor.execute("""
            INSERT INTO coingecko_observations (series_id, date, value)
            VALUES (%s, %s, %s)
            ON CONFLICT (series_id, date) DO UPDATE SET
                value = EXCLUDED.value
        """, (coin_id, rec["date"], rec["price"]))

        cursor.execute("""
            INSERT INTO coingecko_market_data (series_id, date, price, market_cap, total_volume)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (series_id, date) DO UPDATE SET
                price = EXCLUDED.price,
                market_cap = EXCLUDED.market_cap,
                total_volume = EXCLUDED.total_volume
        """, (coin_id, rec["date"], rec["price"], rec["market_cap"], rec["total_volume"]))

        inserted += 1

    conn.commit()
    return inserted


def collect_all(conn,
                coin_filter: list[str] | None = None,
                days: int | str = "max",
                incremental: bool = False):
    """Collect data for all configured coins (or a filtered subset)."""
    coins_to_fetch = CONFIGURED_COINS
    if coin_filter:
        filter_set = set(c.lower() for c in coin_filter)
        coins_to_fetch = [c for c in CONFIGURED_COINS
                          if c[0] in filter_set or c[1].lower() in filter_set]
        if not coins_to_fetch:
            print(f"WARNING: No matching coins found for: {', '.join(coin_filter)}")
            print("Use --list to see configured coins.")
            return

    total = len(coins_to_fetch)
    print(f"[{datetime.now().isoformat()}] Fetching {total} coins from CoinGecko"
          f"{' (incremental)' if incremental else ''}...")

    total_obs = 0
    errors = 0

    for i, (coin_id, symbol, description, kalshi_series) in enumerate(coins_to_fetch, 1):
        print(f"  [{i}/{total}] {symbol} ({coin_id}): {description}")
        try:
            count = collect_coin(conn, coin_id, symbol, description,
                                 kalshi_series, days, incremental)
            total_obs += count
            print(f"    -> {count:,} observations")
        except Exception as e:
            print(f"    -> ERROR: {e}")
            errors += 1

        if i < total:
            time.sleep(RATE_LIMIT_DELAY)

    print(f"\n[{datetime.now().isoformat()}] Done. {total_obs:,} total observations"
          f" across {total - errors}/{total} coins.")
    if errors:
        print(f"  {errors} coins had errors.")


def show_status():
    """Show collection statistics."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM coingecko_series")
    row = cursor.fetchone()
    series_count = row[0] if row else 0

    cursor.execute("SELECT COUNT(*) FROM coingecko_observations")
    row = cursor.fetchone()
    obs_count = row[0] if row else 0

    cursor.execute("SELECT MIN(date), MAX(date) FROM coingecko_observations")
    row = cursor.fetchone()
    min_date, max_date = row if row else (None, None)

    print(f"CoinGecko Collector Status:")
    print(f"  Coins tracked: {series_count}")
    print(f"  Total observations: {obs_count:,}")
    print(f"  Date range: {min_date} to {max_date}")
    print()

    cursor.execute("""
        SELECT s.series_id, s.symbol, s.description,
               COUNT(o.date) as obs_count,
               MIN(o.date) as first_date,
               MAX(o.date) as last_date,
               (SELECT value FROM coingecko_observations o2
                WHERE o2.series_id = s.series_id
                ORDER BY o2.date DESC LIMIT 1) as latest_price
        FROM coingecko_series s
        LEFT JOIN coingecko_observations o ON s.series_id = o.series_id
        GROUP BY s.series_id, s.symbol, s.description
        ORDER BY obs_count DESC
    """)

    for row in cursor.fetchall():
        _, sym, desc, cnt, first, last, latest = row
        price_str = f"${latest:,.2f}" if latest else "N/A"
        print(f"  {sym:6s} {cnt:>7,} obs  {first or 'N/A':>12} to {last or 'N/A':>12}  "
              f"Latest: {price_str:>12}  {desc}")

    conn.close()


def list_coins():
    """List all configured coins."""
    print("Configured CoinGecko coins:")
    print()
    for coin_id, symbol, description, kalshi in CONFIGURED_COINS:
        print(f"  {symbol:6s} ({coin_id:15s})  {description:15s}  Kalshi: {kalshi}")
    print(f"\n  Total: {len(CONFIGURED_COINS)} coins")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CoinGecko Crypto Benchmark Collector for Kalshi market analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                 Fetch all coins (full history)
  %(prog)s --update                        Incremental update
  %(prog)s --coins bitcoin ethereum        Fetch specific coins
  %(prog)s --days 365                      Fetch last 365 days
  %(prog)s --status                        Show collection statistics
  %(prog)s --list                          List configured coins
        """,
    )
    parser.add_argument("--coins", nargs="+", metavar="ID",
                        help="Fetch specific coins by CoinGecko ID or symbol")
    parser.add_argument("--days", type=int, default=None,
                        help="Number of days of history (default: max available)")
    parser.add_argument("--update", action="store_true",
                        help="Incremental update (only fetch new observations)")
    parser.add_argument("--status", action="store_true",
                        help="Show collection statistics")
    parser.add_argument("--list", action="store_true",
                        help="List all configured coins")

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.list:
        list_coins()
    else:
        conn = get_connection()
        try:
            days = args.days if args.days else "max"
            collect_all(conn, args.coins, days, args.update)
        finally:
            conn.close()
