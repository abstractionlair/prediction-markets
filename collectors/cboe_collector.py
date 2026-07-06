#!/usr/bin/env python3
"""
CBOE SPX Options Chain Collector

Fetches and stores daily SPX and SPXW options chain data from CBOE delayed
quotes for comparing prediction market prices against options-implied
risk-neutral distributions.

Data source (public, no API key needed):
  https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json

The _SPX endpoint returns BOTH SPX (monthly) and SPXW (weekly/0DTE) options
in a single response — SPXW symbols are distinguished by their option root.
The separate _SPXW.json endpoint returns 403 (not publicly accessible), but
is attempted as a fallback in case CBOE re-enables it.

CBOE prohibits automated mass scraping. This collector is designed for a
single daily fetch (or a few times per day) — not continuous polling.

Usage:
    python cboe_collector.py                   # Fetch SPX chain (includes SPXW)
    python cboe_collector.py --symbol SPX      # Fetch SPX only
    python cboe_collector.py --symbol SPXW     # Try SPXW endpoint (may 403)
    python cboe_collector.py --status          # Show collection statistics
    python cboe_collector.py --list-expiries   # Show expiration dates in latest snapshot
"""

import argparse
import json
import os
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

import time

import psycopg2

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options"
SYMBOLS = {
    "SPX": "_SPX",    # Monthly SPX options
    "SPXW": "_SPXW",  # Weekly/0DTE SPX options
}

REQUEST_TIMEOUT = 60  # seconds


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


# ---------------------------------------------------------------------------
# CBOE data fetching and parsing
# ---------------------------------------------------------------------------

def fetch_cboe_chain(symbol_key: str, verbose: bool = False,
                     max_retries: int = 3) -> dict:
    """Fetch the full options chain from CBOE delayed quotes.

    Args:
        symbol_key: 'SPX' or 'SPXW'
        max_retries: Number of attempts with exponential backoff (default: 3)

    Returns:
        Raw JSON response as a dict.
    """
    cboe_symbol = SYMBOLS[symbol_key]
    url = f"{CBOE_BASE}/{cboe_symbol}.json"

    if verbose:
        print(f"  GET {url}")

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            if verbose:
                spot = data.get("data", {}).get("current_price", "?")
                n_opts = len(data.get("data", {}).get("options", []))
                ts = data.get("timestamp", "?")
                print(f"  -> Spot={spot}, {n_opts} options, timestamp={ts}")

            return data
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + (time.time() % 1)
                if verbose:
                    print(f"  Retry {attempt + 1}/{max_retries} after {wait_time:.1f}s: {e}")
                time.sleep(wait_time)
            else:
                raise
    raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries")


def parse_option_symbol(symbol: str) -> dict | None:
    """Parse a CBOE option symbol into components.

    Handles both formats:
      SPX260320C06650000  -> 2026-03-20, call, 6650.0
      SPXW260313P05500000 -> 2026-03-13, put, 5500.0
    """
    m = re.match(r'^(SPXW?)(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return None

    root = m.group(1)
    date_str = m.group(2)
    opt_type_char = m.group(3)
    strike = int(m.group(4)) / 1000.0

    try:
        expiry = datetime.strptime(f"20{date_str}", "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None

    return {
        "root": root,
        "expiry": expiry,
        "option_type": "call" if opt_type_char == "C" else "put",
        "strike": strike,
    }


def safe_float(val, default=None):
    """Convert a value to float, returning default if missing or invalid."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=None):
    """Convert a value to int, returning default if missing or invalid."""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def store_snapshot(conn, symbol_key: str, raw_data: dict,
                   verbose: bool = False) -> int | None:
    """Parse and store a CBOE options chain snapshot.

    Stores:
      1. Raw JSON in cboe_snapshots table (for reproducibility)
      2. Parsed option records in cboe_options table (for querying)

    Returns:
        snapshot_id of the inserted snapshot.
    """
    cursor = conn.cursor()

    fetched_at = datetime.now(timezone.utc).isoformat()
    spot_price = safe_float(raw_data.get("data", {}).get("current_price"))
    raw_json = json.dumps(raw_data)
    compressed = zlib.compress(raw_json.encode("utf-8"), level=6)

    # Insert snapshot with zlib-compressed JSON (BYTEA), returning the id
    cursor.execute("""
        INSERT INTO cboe_snapshots (symbol, fetched_at, spot_price, data_json)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (symbol_key, fetched_at, spot_price, psycopg2.Binary(compressed)))
    snapshot_id = cursor.fetchone()[0]

    # Parse and insert individual options
    raw_options = raw_data.get("data", {}).get("options", [])
    inserted = 0
    parse_failures = 0

    for opt in raw_options:
        option_symbol = opt.get("option", "")
        parsed = parse_option_symbol(option_symbol)
        if not parsed:
            parse_failures += 1
            continue

        cursor.execute("""
            INSERT INTO cboe_options (snapshot_id, option_symbol, expiry, strike,
                                 option_type, bid, ask, last_price, volume,
                                 open_interest, iv, delta, gamma, theta, vega)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            snapshot_id,
            option_symbol,
            parsed["expiry"],
            parsed["strike"],
            parsed["option_type"],
            safe_float(opt.get("bid")),
            safe_float(opt.get("ask")),
            safe_float(opt.get("last_trade_price")),
            safe_int(opt.get("volume")),
            safe_int(opt.get("open_interest")),
            safe_float(opt.get("iv")),
            safe_float(opt.get("delta")),
            safe_float(opt.get("gamma")),
            safe_float(opt.get("theta")),
            safe_float(opt.get("vega")),
        ))
        inserted += 1

    conn.commit()

    if verbose:
        print(f"  Stored snapshot #{snapshot_id}: {inserted} options parsed, "
              f"{parse_failures} parse failures")

    return snapshot_id


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(symbols: list[str], verbose: bool = False):
    """Fetch and store options chain data for the given symbols."""
    conn = get_connection()

    print(f"[{datetime.now().isoformat()}] CBOE Options Chain Collector")
    print(f"  Symbols: {', '.join(symbols)}")
    print()

    for symbol_key in symbols:
        print(f"  Fetching {symbol_key}...")
        try:
            raw_data = fetch_cboe_chain(symbol_key, verbose)
            spot = safe_float(raw_data.get("data", {}).get("current_price"))
            n_opts = len(raw_data.get("data", {}).get("options", []))
            ts = raw_data.get("timestamp", "?")

            snapshot_id = store_snapshot(conn, symbol_key, raw_data, verbose)

            print(f"    Spot: {spot}")
            print(f"    Options: {n_opts}")
            print(f"    CBOE timestamp: {ts}")
            print(f"    Snapshot ID: {snapshot_id}")
            print()

        except requests.RequestException as e:
            print(f"    ERROR: {e}")
            print()

    conn.close()
    print(f"[{datetime.now().isoformat()}] Done.")


# ---------------------------------------------------------------------------
# Status and queries
# ---------------------------------------------------------------------------

def show_status():
    """Show collection statistics."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cursor = conn.cursor()

    print(f"CBOE Options Chain Collector Status")
    print()

    # Overall stats
    cursor.execute("SELECT COUNT(*) FROM cboe_snapshots")
    row = cursor.fetchone()
    n_snapshots = row[0] if row else 0

    cursor.execute("SELECT COUNT(*) FROM cboe_options")
    row = cursor.fetchone()
    n_options = row[0] if row else 0

    print(f"  Total snapshots: {n_snapshots:,}")
    print(f"  Total option records: {n_options:,}")
    print()

    # Per-symbol breakdown
    cursor.execute("""
        SELECT symbol,
               COUNT(*) as n_snapshots,
               MIN(fetched_at) as first_fetch,
               MAX(fetched_at) as last_fetch,
               AVG(spot_price) as avg_spot
        FROM cboe_snapshots
        GROUP BY symbol
        ORDER BY symbol
    """)

    for row in cursor.fetchall():
        symbol, n, first, last, avg_spot = row
        print(f"  [{symbol}]")
        print(f"    Snapshots: {n}")
        print(f"    First fetch: {first}")
        print(f"    Last fetch:  {last}")
        if avg_spot:
            print(f"    Avg spot:    {avg_spot:.2f}")
        print()

    # Latest snapshot details
    for symbol in ["SPX", "SPXW"]:
        cursor.execute("""
            SELECT id, fetched_at, spot_price
            FROM cboe_snapshots
            WHERE symbol = %s
            ORDER BY fetched_at DESC
            LIMIT 1
        """, (symbol,))
        row = cursor.fetchone()
        if row:
            sid, fetched, spot = row
            cursor.execute("""
                SELECT COUNT(*),
                       COUNT(DISTINCT expiry),
                       MIN(expiry),
                       MAX(expiry)
                FROM cboe_options
                WHERE snapshot_id = %s
            """, (sid,))
            row_data = cursor.fetchone() or (0, 0, None, None)
            n_opts, n_exp, min_exp, max_exp = row_data
            print(f"  Latest {symbol} snapshot (#{sid}, {fetched}):")
            print(f"    Spot: {spot}")
            print(f"    Options: {n_opts:,}")
            print(f"    Expiries: {n_exp} ({min_exp} to {max_exp})")
            print()

    conn.close()


def list_expiries():
    """List available expiration dates from the latest snapshots."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cursor = conn.cursor()

    for symbol in ["SPX", "SPXW"]:
        cursor.execute("""
            SELECT id, fetched_at, spot_price
            FROM cboe_snapshots
            WHERE symbol = %s
            ORDER BY fetched_at DESC
            LIMIT 1
        """, (symbol,))
        row = cursor.fetchone()
        if not row:
            print(f"  No {symbol} snapshots found.")
            continue

        sid, fetched, spot = row
        print(f"  {symbol} (snapshot #{sid}, fetched {fetched}, spot={spot}):")
        print()

        cursor.execute("""
            SELECT expiry,
                   COUNT(*) as n_options,
                   COUNT(CASE WHEN option_type = 'call' THEN 1 END) as n_calls,
                   COUNT(CASE WHEN option_type = 'put' THEN 1 END) as n_puts,
                   MIN(strike) as min_strike,
                   MAX(strike) as max_strike,
                   SUM(COALESCE(volume, 0)) as total_vol,
                   SUM(COALESCE(open_interest, 0)) as total_oi
            FROM cboe_options
            WHERE snapshot_id = %s
            GROUP BY expiry
            ORDER BY expiry
        """, (sid,))

        rows = cursor.fetchall()
        if not rows:
            print(f"    No options parsed.")
            continue

        print(f"    {'Expiry':>12s} | {'Options':>7s} | {'Calls':>5s} | {'Puts':>5s} | "
              f"{'Min Strike':>10s} | {'Max Strike':>10s} | {'Volume':>10s} | {'OI':>10s}")
        print(f"    {'-'*12} | {'-'*7} | {'-'*5} | {'-'*5} | "
              f"{'-'*10} | {'-'*10} | {'-'*10} | {'-'*10}")

        for r in rows:
            expiry, n, nc, np_, min_k, max_k, vol, oi = r
            print(f"    {expiry:>12s} | {n:>7,} | {nc:>5,} | {np_:>5,} | "
                  f"{min_k:>10.0f} | {max_k:>10.0f} | {vol:>10,} | {oi:>10,}")

        print(f"\n    Total: {len(rows)} expiration dates, "
              f"{sum(r[1] for r in rows):,} options")
        print()

    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CBOE SPX Options Chain Collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                        Fetch both SPX and SPXW chains
  %(prog)s --symbol SPX           Fetch SPX only
  %(prog)s --symbol SPXW          Fetch SPXW (weekly/0DTE) only
  %(prog)s --status               Show collection statistics
  %(prog)s --list-expiries        Show available expiration dates
  %(prog)s -v                     Verbose output
        """,
    )
    parser.add_argument("--symbol", choices=["SPX", "SPXW"],
                        help="Fetch only this symbol (default: both)")
    parser.add_argument("--status", action="store_true",
                        help="Show collection statistics")
    parser.add_argument("--list-expiries", action="store_true",
                        help="Show available expiration dates in latest snapshot")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose output")

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.list_expiries:
        list_expiries()
    else:
        symbols = [args.symbol] if args.symbol else ["SPX", "SPXW"]
        collect(symbols, args.verbose)
