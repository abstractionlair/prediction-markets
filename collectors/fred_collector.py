#!/usr/bin/env python3
"""
FRED Benchmark Data Collector

Fetches and stores Federal Reserve Economic Data (FRED) time series for all
Kalshi market categories that have free FRED benchmarks. Supports incremental
updates and can be run as a cron job for daily refreshes.

Requires FRED_API_KEY in ~/.env or as environment variable.

Usage:
    python fred_collector.py                          # Fetch all configured series
    python fred_collector.py --series SP500 DGS10     # Fetch specific series
    python fred_collector.py --status                 # Show collection statistics
    python fred_collector.py --update                 # Incremental update (only new data)
    python fred_collector.py --list                   # List all configured series
"""

import argparse
import os
import sys
import time
from datetime import datetime
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

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

# All FRED series relevant to Kalshi market categories
# Format: (series_id, description, category)
CONFIGURED_SERIES = [
    # Equity Indices
    ("SP500", "S&P 500 Index", "equities"),

    # Crude Oil
    ("DCOILWTICO", "WTI Crude Oil Spot Price", "energy"),

    # Treasury Yields
    ("DGS10", "10-Year Treasury Constant Maturity Rate", "rates"),
    ("DGS2", "2-Year Treasury Constant Maturity Rate", "rates"),
    ("DGS30", "30-Year Treasury Constant Maturity Rate", "rates"),
    ("DGS5", "5-Year Treasury Constant Maturity Rate", "rates"),
    ("DTB3", "3-Month Treasury Bill Secondary Market Rate", "rates"),

    # Yield Curve Spreads
    ("T10Y2Y", "10-Year minus 2-Year Treasury Spread", "rates"),
    ("T10Y3M", "10-Year minus 3-Month Treasury Spread", "rates"),

    # CPI / Inflation
    ("CPIAUCSL", "CPI for All Urban Consumers: All Items", "inflation"),
    ("CPILFESL", "CPI for All Urban Consumers: All Items Less Food and Energy (Core CPI)", "inflation"),
    ("PCEPILFE", "PCE Excluding Food and Energy (Core PCE)", "inflation"),

    # Breakeven Inflation
    ("T10YIE", "10-Year Breakeven Inflation Rate", "inflation"),
    ("T5YIE", "5-Year Breakeven Inflation Rate", "inflation"),

    # Employment / Labor
    ("PAYEMS", "All Employees, Total Nonfarm (Thousands)", "employment"),
    ("UNRATE", "Unemployment Rate", "employment"),
    ("ICSA", "Initial Claims, Seasonally Adjusted", "employment"),
    ("ADPMNUSNERSA", "ADP National Employment Report", "employment"),

    # GDP
    ("A191RL1Q225SBEA", "Real GDP Growth Rate (Quarterly, Annualized)", "gdp"),
    ("GDPNOW", "Atlanta Fed GDPNow Estimate", "gdp"),

    # Gas Prices
    ("GASREGW", "US Regular All Formulations Gas Price (Weekly)", "energy"),

    # Natural Gas
    ("DHHNGSP", "Henry Hub Natural Gas Spot Price", "energy"),

    # Foreign Exchange
    ("DEXUSEU", "US Dollar / Euro Exchange Rate", "fx"),
    ("DEXJPUS", "Japanese Yen / US Dollar Exchange Rate", "fx"),
    ("DEXBZUS", "Brazilian Real / US Dollar Exchange Rate", "fx"),

    # Precious Metals
    ("GOLDPMGBD228NLBM", "Gold Fixing Price (London PM Fix)", "metals"),

    # Federal Reserve
    ("DFF", "Federal Funds Effective Rate", "fed"),
    ("WALCL", "Federal Reserve Total Assets (Balance Sheet)", "fed"),

    # Housing
    ("MORTGAGE30US", "30-Year Fixed Rate Mortgage Average", "housing"),
    ("HOUST", "Housing Starts: Total (Thousands)", "housing"),
    ("PERMIT", "New Privately-Owned Housing Units Authorized (Thousands)", "housing"),
    ("EXHOSLUSM495S", "Existing Home Sales (Thousands)", "housing"),
    ("HSN1F", "New One Family Houses Sold (Thousands)", "housing"),

    # Other
    ("USREC", "NBER Recession Indicator", "other"),
]

# Rate limiting: FRED allows 120 requests per minute
RATE_LIMIT_DELAY = 0.6  # seconds between requests


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


def load_api_key() -> str:
    """Load FRED API key from environment or ~/.env file."""
    key = os.environ.get("FRED_API_KEY")
    if key:
        return key

    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("FRED_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")

    print("ERROR: FRED_API_KEY not found. Set it in ~/.env or as environment variable.")
    print("  Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html")
    sys.exit(1)


def fetch_series_info(api_key: str, series_id: str) -> dict | None:
    """Fetch metadata about a FRED series."""
    url = f"{FRED_BASE_URL}/series"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            series_list = data.get("seriess", [])
            if series_list:
                return series_list[0]
        else:
            print(f"  WARNING: Failed to fetch info for {series_id}: HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  WARNING: Request error for {series_id}: {e}")
    return None


def fetch_observations(api_key: str, series_id: str,
                       observation_start: str | None = None) -> list[dict]:
    """Fetch observations for a FRED series.

    Args:
        api_key: FRED API key
        series_id: FRED series identifier
        observation_start: Optional start date (YYYY-MM-DD) for incremental updates
    """
    url = f"{FRED_BASE_URL}/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if observation_start:
        params["observation_start"] = observation_start

    all_observations = []
    offset = 0
    limit = 10000  # FRED max per request

    while True:
        params["offset"] = str(offset)
        params["limit"] = str(limit)

        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  WARNING: HTTP {resp.status_code} fetching {series_id} (offset {offset})")
                break

            data = resp.json()
            observations = data.get("observations", [])

            for obs in observations:
                # FRED uses "." for missing values
                if obs.get("value") not in (".", None, ""):
                    all_observations.append({
                        "date": obs["date"],
                        "value": float(obs["value"]),
                    })

            # Check if there are more pages
            count = int(data.get("count", 0))
            offset += limit
            if offset >= count:
                break

            time.sleep(RATE_LIMIT_DELAY)

        except requests.RequestException as e:
            print(f"  WARNING: Request error for {series_id}: {e}")
            break

    return all_observations


def collect_series(conn, api_key: str,
                   series_id: str, description: str, category: str,
                   incremental: bool = False) -> int:
    """Collect data for a single FRED series.

    Returns number of new observations inserted.
    """
    cursor = conn.cursor()

    # Determine start date for incremental update
    observation_start = None
    if incremental:
        cursor.execute(
            "SELECT MAX(date) FROM fred_observations WHERE series_id = %s",
            (series_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            observation_start = row[0]
            # Fetch from the last known date to catch any revisions
            print(f"  Incremental from {observation_start}")

    # Fetch series metadata
    info = fetch_series_info(api_key, series_id)
    time.sleep(RATE_LIMIT_DELAY)

    if info:
        cursor.execute("""
            INSERT INTO fred_series (series_id, description, frequency, units, last_updated, category)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (series_id) DO UPDATE SET
                description = EXCLUDED.description,
                frequency = EXCLUDED.frequency,
                units = EXCLUDED.units,
                last_updated = EXCLUDED.last_updated,
                category = EXCLUDED.category
        """, (
            series_id,
            info.get("title", description),
            info.get("frequency_short", ""),
            info.get("units_short", ""),
            info.get("last_updated", ""),
            category,
        ))
    else:
        # Store with provided description even if API call failed
        cursor.execute("""
            INSERT INTO fred_series (series_id, description, category)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (series_id, description, category))

    # Fetch observations
    observations = fetch_observations(api_key, series_id, observation_start)

    if not observations:
        conn.commit()
        return 0

    # Upsert observations
    inserted = 0
    for obs in observations:
        cursor.execute("""
            INSERT INTO fred_observations (series_id, date, value)
            VALUES (%s, %s, %s)
            ON CONFLICT (series_id, date) DO UPDATE SET
                value = EXCLUDED.value
        """, (series_id, obs["date"], obs["value"]))
        inserted += 1

    conn.commit()
    return inserted


def collect_all(conn, api_key: str,
                series_filter: list[str] | None = None,
                incremental: bool = False):
    """Collect data for all configured series (or a filtered subset)."""
    series_to_fetch = CONFIGURED_SERIES
    if series_filter:
        filter_set = set(s.upper() for s in series_filter)
        series_to_fetch = [s for s in CONFIGURED_SERIES if s[0] in filter_set]
        missing = filter_set - {s[0] for s in series_to_fetch}
        if missing:
            print(f"WARNING: Unknown series: {', '.join(sorted(missing))}")

    total = len(series_to_fetch)
    print(f"[{datetime.now().isoformat()}] Fetching {total} FRED series"
          f"{' (incremental)' if incremental else ''}...")

    total_obs = 0
    errors = 0

    for i, (series_id, description, category) in enumerate(series_to_fetch, 1):
        print(f"  [{i}/{total}] {series_id}: {description}")
        try:
            count = collect_series(conn, api_key, series_id, description,
                                   category, incremental)
            total_obs += count
            print(f"    -> {count:,} observations")
        except Exception as e:
            print(f"    -> ERROR: {e}")
            errors += 1

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n[{datetime.now().isoformat()}] Done. {total_obs:,} total observations"
          f" across {total - errors}/{total} series.")
    if errors:
        print(f"  {errors} series had errors.")


def show_status():
    """Show collection statistics."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM fred_series")
    row = cursor.fetchone()
    series_count = row[0] if row else 0

    cursor.execute("SELECT COUNT(*) FROM fred_observations")
    row = cursor.fetchone()
    obs_count = row[0] if row else 0

    cursor.execute("SELECT MIN(date), MAX(date) FROM fred_observations")
    row = cursor.fetchone()
    min_date, max_date = row if row else (None, None)

    print(f"FRED Collector Status:")
    print(f"  Series tracked: {series_count}")
    print(f"  Total observations: {obs_count:,}")
    print(f"  Date range: {min_date} to {max_date}")
    print()

    # Per-series breakdown
    cursor.execute("""
        SELECT s.series_id, s.description, s.frequency, s.category,
               COUNT(o.date) as obs_count,
               MIN(o.date) as first_date,
               MAX(o.date) as last_date
        FROM fred_series s
        LEFT JOIN fred_observations o ON s.series_id = o.series_id
        GROUP BY s.series_id, s.description, s.frequency, s.category
        ORDER BY s.category, s.series_id
    """)

    current_cat = None
    for row in cursor.fetchall():
        sid, desc, freq, cat, cnt, first, last = row
        if cat != current_cat:
            current_cat = cat
            print(f"\n  [{cat or 'uncategorized'}]")
        print(f"    {sid:25s} {cnt:>8,} obs  {first or 'N/A':>12} to {last or 'N/A':>12}  ({freq or '?'})  {desc[:50]}")

    conn.close()


def list_series():
    """List all configured FRED series."""
    print("Configured FRED series:")
    print()
    current_cat = None
    for series_id, description, category in CONFIGURED_SERIES:
        if category != current_cat:
            current_cat = category
            print(f"  [{category}]")
        print(f"    {series_id:25s} {description}")
    print(f"\n  Total: {len(CONFIGURED_SERIES)} series")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FRED Benchmark Data Collector for Kalshi market analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Fetch all configured series (full history)
  %(prog)s --update                 Incremental update (only new data)
  %(prog)s --series SP500 DGS10     Fetch specific series
  %(prog)s --status                 Show collection statistics
  %(prog)s --list                   List all configured series
        """,
    )
    parser.add_argument("--series", nargs="+", metavar="ID",
                        help="Fetch specific FRED series IDs (default: all)")
    parser.add_argument("--update", action="store_true",
                        help="Incremental update (only fetch new observations)")
    parser.add_argument("--status", action="store_true",
                        help="Show collection statistics")
    parser.add_argument("--list", action="store_true",
                        help="List all configured FRED series")

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.list:
        list_series()
    else:
        api_key = load_api_key()
        conn = get_connection()
        try:
            collect_all(conn, api_key, args.series, args.update)
        finally:
            conn.close()
