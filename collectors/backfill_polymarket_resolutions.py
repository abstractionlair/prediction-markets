#!/usr/bin/env python3
"""
Backfill resolution data for Polymarket markets.

Fetches closed markets from the Gamma API and updates our DB with
the resolution outcome (derived from outcomePrices).

Usage:
    python backfill_polymarket_resolutions.py --limit 100 --dry-run
    python backfill_polymarket_resolutions.py              # full run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import requests

GAMMA_API = "https://gamma-api.polymarket.com"


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


def resolve_outcome(outcomes: list, outcome_prices: list) -> str | None:
    """Determine winner from outcomePrices.

    outcomePrices like ["0", "1"] means the second outcome won.
    outcomePrices like ["1", "0"] means the first outcome won.
    """
    if not outcomes or not outcome_prices:
        return None
    if len(outcomes) != len(outcome_prices):
        return None

    try:
        prices = [float(p) for p in outcome_prices]
    except (ValueError, TypeError):
        return None

    # Find the outcome with price == 1 (the winner)
    for i, p in enumerate(prices):
        if p == 1.0:
            return outcomes[i]

    # Sometimes prices are like ["0.999", "0.001"] — take the max
    max_idx = max(range(len(prices)), key=lambda i: prices[i])
    if prices[max_idx] > 0.9:
        return outcomes[max_idx]

    return None


def fetch_closed_markets(offset: int = 0, limit: int = 500) -> list:
    """Fetch a page of closed markets from the Gamma API."""
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={
            "closed": "true",
            "limit": limit,
            "offset": offset,
            "order": "closedTime",
            "ascending": "false",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Backfill Polymarket resolutions")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--limit", type=int, default=0, help="Max markets to update (0=unlimited)")
    parser.add_argument("--page-size", type=int, default=500, help="API page size")
    args = parser.parse_args()

    conn = psycopg2.connect(get_pg_dsn())
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")

    # Get all our unresolved market_ids for fast lookup
    cur.execute("""
        SELECT market_id FROM polymarket_markets
        WHERE result IS NULL OR result = ''
    """)
    unresolved = set(r[0] for r in cur.fetchall())
    print(f"Unresolved markets in DB: {len(unresolved)}")

    updated = 0
    skipped_not_ours = 0
    skipped_no_resolution = 0
    errors = 0
    pages_fetched = 0
    consecutive_empty = 0
    offset = 0

    while True:
        if args.limit and updated >= args.limit:
            print(f"Reached --limit {args.limit}")
            break

        try:
            markets = fetch_closed_markets(offset=offset, limit=args.page_size)
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 429:
                wait = 5
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"  API error: {e}")
            errors += 1
            if errors > 10:
                print("Too many errors, stopping")
                break
            time.sleep(2)
            offset += args.page_size
            continue
        except Exception as e:
            print(f"  Request error: {e}")
            errors += 1
            if errors > 10:
                break
            time.sleep(2)
            offset += args.page_size
            continue

        if not markets:
            consecutive_empty += 1
            if consecutive_empty > 3:
                print("No more markets from API")
                break
            offset += args.page_size
            continue

        consecutive_empty = 0
        pages_fetched += 1

        page_updates = 0
        for m in markets:
            condition_id = m.get("conditionId")
            if not condition_id or condition_id not in unresolved:
                skipped_not_ours += 1
                continue

            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []

            outcome_prices = m.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = []

            result = resolve_outcome(outcomes, outcome_prices)
            if not result:
                skipped_no_resolution += 1
                continue

            closed_time = m.get("closedTime", "")

            if not args.dry_run:
                cur.execute("""
                    UPDATE polymarket_markets
                    SET result = %s, resolved_at = %s, is_active = FALSE
                    WHERE market_id = %s
                """, (result, closed_time, condition_id))

            unresolved.discard(condition_id)
            updated += 1
            page_updates += 1

            if args.limit and updated >= args.limit:
                break

        if not args.dry_run and page_updates > 0:
            conn.commit()

        print(f"  Page {pages_fetched} (offset={offset}): {page_updates} updated, "
              f"{len(markets)} fetched. Total updated: {updated}")

        offset += args.page_size

        # Small delay between pages
        time.sleep(0.5)

    if not args.dry_run:
        conn.commit()

    print(f"\nDone.")
    print(f"  Updated: {updated}")
    print(f"  Skipped (not in our DB): {skipped_not_ours}")
    print(f"  Skipped (no resolution): {skipped_no_resolution}")
    print(f"  Errors: {errors}")
    print(f"  Still unresolved in DB: {len(unresolved)}")

    conn.close()


if __name__ == "__main__":
    main()
