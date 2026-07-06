#!/usr/bin/env python3
"""
Extract price + outcome data from the Polymarket subgraph for FLB analysis.

1. Fetch all resolved binary conditions (225K)
2. Fetch MarketData to map token_id -> condition_id
3. For a sample of conditions, get pre-resolution trade prices
4. Run bin-and-count FLB analysis

Usage:
    python extract_polymarket_subgraph.py --sample 5000
    python extract_polymarket_subgraph.py --sample 5000 --output polymarket_flb.csv
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import numpy as np

SUBGRAPH_URL = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
    "polymarket-orderbook-resync/prod/gn"
)


def gql(query: str, retries: int = 3) -> dict:
    """Execute a GraphQL query with retry logic."""
    for attempt in range(retries):
        try:
            resp = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=30)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                msg = data["errors"][0].get("message", "")
                if "timeout" in msg.lower() or "cancel" in msg.lower():
                    time.sleep(2 ** attempt + 1)
                    continue
                raise RuntimeError(f"GraphQL error: {msg[:200]}")
            return data.get("data", {})
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}


def fetch_resolved_conditions() -> list[dict]:
    """Fetch all resolved binary conditions."""
    print("Fetching resolved conditions...")
    all_conditions = []
    last_id = ""
    page = 0

    while True:
        data = gql('''
        {
          conditions(first: 1000, where: {resolutionTimestamp_not: null, id_gt: "%s"}, orderBy: id) {
            id
            resolutionTimestamp
            payouts
            outcomeSlotCount
          }
        }
        ''' % last_id)

        conds = data.get("conditions", [])
        if not conds:
            break

        # Keep only binary with clean resolution
        for c in conds:
            if c["outcomeSlotCount"] != 2:
                continue
            payouts = c.get("payouts")
            if not payouts or len(payouts) != 2:
                continue
            # Clean resolution: one outcome gets ~1, the other ~0
            try:
                p0, p1 = float(payouts[0]), float(payouts[1])
            except (ValueError, TypeError):
                continue
            if not ((p0 > 0.9 and p1 < 0.1) or (p0 < 0.1 and p1 > 0.9)):
                continue
            c["_winner"] = 0 if p0 > 0.5 else 1
            all_conditions.append(c)

        last_id = conds[-1]["id"]
        page += 1
        if page % 10 == 0:
            print(f"  {len(all_conditions)} conditions so far (page {page})...")

        if len(conds) < 1000:
            break
        time.sleep(0.2)

    print(f"Total resolved binary conditions: {len(all_conditions)}")
    return all_conditions


def fetch_market_data_for_conditions(condition_ids: set) -> dict:
    """Fetch MarketData entries and map token_id -> condition info.

    Returns: {token_id: {"condition_id": ..., "price": ...}}
    """
    print("Fetching MarketData (token -> condition mapping)...")
    token_map = {}
    last_id = ""
    page = 0

    while True:
        data = gql('''
        {
          marketDatas(first: 1000, where: {id_gt: "%s"}, orderBy: id) {
            id
            priceOrderbook
            condition { id }
          }
        }
        ''' % last_id)

        mds = data.get("marketDatas", [])
        if not mds:
            break

        for md in mds:
            cid = md["condition"]["id"]
            if cid in condition_ids:
                token_map[md["id"]] = {
                    "condition_id": cid,
                    "price_orderbook": md.get("priceOrderbook"),
                }

        last_id = mds[-1]["id"]
        page += 1
        if page % 50 == 0:
            print(f"  {len(token_map)} tokens mapped (page {page})...")

        if len(mds) < 1000:
            break
        time.sleep(0.1)

    print(f"Total tokens mapped: {len(token_map)}")
    return token_map


def get_pre_resolution_trades(token_id: str, before_ts: int, window_hours: int = 120) -> list[dict]:
    """Get trades for a token in a time window before resolution."""
    after_ts = before_ts - (window_hours * 3600)

    data = gql('''
    {
      orderFilledEvents(
        first: 50,
        where: {
          timestamp_gte: "%d",
          timestamp_lt: "%d",
          makerAssetId: "%s"
        },
        orderBy: timestamp,
        orderDirection: desc
      ) {
        timestamp
        makerAssetId
        takerAssetId
        makerAmountFilled
        takerAmountFilled
      }
    }
    ''' % (after_ts, before_ts, token_id))

    trades = data.get("orderFilledEvents", [])

    # Also try where this token is the taker asset
    data2 = gql('''
    {
      orderFilledEvents(
        first: 50,
        where: {
          timestamp_gte: "%d",
          timestamp_lt: "%d",
          takerAssetId: "%s"
        },
        orderBy: timestamp,
        orderDirection: desc
      ) {
        timestamp
        makerAssetId
        takerAssetId
        makerAmountFilled
        takerAmountFilled
      }
    }
    ''' % (after_ts, before_ts, token_id))

    trades.extend(data2.get("orderFilledEvents", []))
    return trades


def compute_trade_price(trade: dict, token_id: str) -> float | None:
    """Compute price of token from a trade.

    USDC has assetId "0". Price = USDC amount / token amount.
    Both amounts use 6 decimal places.
    """
    maker_id = trade["makerAssetId"]
    taker_id = trade["takerAssetId"]
    maker_amt = int(trade["makerAmountFilled"])
    taker_amt = int(trade["takerAmountFilled"])

    if maker_amt == 0 or taker_amt == 0:
        return None

    if maker_id == "0" and taker_id == token_id:
        # Maker pays USDC for tokens
        return maker_amt / taker_amt
    elif taker_id == "0" and maker_id == token_id:
        # Maker provides tokens, receives USDC
        return taker_amt / maker_amt
    else:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=5000,
                        help="Number of conditions to sample")
    parser.add_argument("--output", default="polymarket_flb_subgraph.csv",
                        help="Output CSV file")
    parser.add_argument("--horizon-hours", type=int, default=72,
                        help="Target hours before resolution for price")
    parser.add_argument("--window-hours", type=int, default=120,
                        help="Window to search for trades before resolution")
    args = parser.parse_args()

    # Step 1: Get all resolved conditions
    conditions = fetch_resolved_conditions()
    if not conditions:
        print("No conditions found")
        return

    # Step 2: Sample
    if args.sample < len(conditions):
        sampled = random.sample(conditions, args.sample)
        print(f"Sampled {len(sampled)} conditions from {len(conditions)}")
    else:
        sampled = conditions
        print(f"Using all {len(sampled)} conditions")

    cond_map = {c["id"]: c for c in sampled}
    cond_ids = set(cond_map.keys())

    # Step 3: Get token -> condition mapping
    # This is slow (paginating all MarketData). For a sample, let's try
    # querying MarketData per condition instead.
    print("Fetching token IDs for sampled conditions...")
    token_to_cond = {}  # token_id -> condition_id
    cond_tokens = defaultdict(list)  # condition_id -> [(token_id, priceOrderbook)]

    batch_size = 20
    cond_list = list(cond_ids)
    for i in range(0, len(cond_list), batch_size):
        batch = cond_list[i:i + batch_size]
        cid_filter = '", "'.join(batch)
        try:
            data = gql('''
            {
              marketDatas(first: 1000, where: {condition_in: ["%s"]}) {
                id
                priceOrderbook
                condition { id }
              }
            }
            ''' % cid_filter)
        except Exception as e:
            print(f"  Batch {i // batch_size} failed: {e}")
            time.sleep(2)
            continue

        for md in data.get("marketDatas", []):
            tid = md["id"]
            cid = md["condition"]["id"]
            post_price = float(md["priceOrderbook"]) if md.get("priceOrderbook") else None
            token_to_cond[tid] = cid
            cond_tokens[cid].append((tid, post_price))

        if (i // batch_size) % 10 == 0:
            print(f"  Batch {i // batch_size}: {len(token_to_cond)} tokens found")
        time.sleep(0.2)

    # Determine which token is the winner using post-resolution prices
    # The winning token has priceOrderbook near 1.0, loser near 0.0
    cond_winner_token = {}  # condition_id -> winning_token_id
    for cid, tokens in cond_tokens.items():
        if len(tokens) != 2:
            continue
        t0_id, t0_price = tokens[0]
        t1_id, t1_price = tokens[1]
        if t0_price is not None and t1_price is not None:
            if t0_price > t1_price:
                cond_winner_token[cid] = t0_id
            else:
                cond_winner_token[cid] = t1_id
        elif t0_price is not None:
            cond_winner_token[cid] = t0_id if t0_price > 0.5 else t1_id
        elif t1_price is not None:
            cond_winner_token[cid] = t1_id if t1_price > 0.5 else t0_id

    print(f"Found tokens for {len(cond_tokens)} conditions, "
          f"winner mapped for {len(cond_winner_token)}")

    # Step 4: For each condition, get pre-resolution trades
    print(f"Fetching pre-resolution trades (target {args.horizon_hours}h before)...")
    results = []
    errors = 0
    no_trades = 0

    for idx, (cid, token_pairs) in enumerate(cond_tokens.items()):
        cond = cond_map.get(cid)
        if not cond:
            continue
        res_ts = int(cond["resolutionTimestamp"])
        target_ts = res_ts - (args.horizon_hours * 3600)
        winner_token = cond_winner_token.get(cid)
        if not winner_token:
            continue

        for token_id, _post_price in token_pairs[:2]:
            try:
                trades = get_pre_resolution_trades(
                    token_id, before_ts=res_ts - 3600 * 24,  # at least 24h before
                    window_hours=args.window_hours
                )
            except Exception as e:
                errors += 1
                continue

            if not trades:
                no_trades += 1
                continue

            # Get the most recent trade in our window
            prices = []
            for t in trades:
                p = compute_trade_price(t, token_id)
                if p and 0.01 < p < 0.99:
                    prices.append((int(t["timestamp"]), p))

            if not prices:
                no_trades += 1
                continue

            # Take the trade closest to target horizon
            prices.sort(key=lambda x: abs(x[0] - target_ts))
            trade_ts, price = prices[0]
            lead_hours = (res_ts - trade_ts) / 3600

            # Outcome: did this token win (pay out 1) or lose (pay out 0)?
            outcome = 1.0 if token_id == winner_token else 0.0

            results.append({
                "condition_id": cid,
                "token_id": token_id,
                "price": price,
                "outcome": outcome,
                "lead_hours": lead_hours,
                "resolution_ts": res_ts,
                "trade_ts": trade_ts,
            })

        if (idx + 1) % 100 == 0:
            print(f"  {idx + 1}/{len(cond_tokens)}: {len(results)} observations, "
                  f"{no_trades} no-trades, {errors} errors")
        time.sleep(0.15)  # Rate limit

    print(f"\nExtraction complete: {len(results)} observations")
    print(f"  No trades found: {no_trades}")
    print(f"  Errors: {errors}")

    if not results:
        print("No data to analyze")
        return

    # Save to CSV
    outpath = Path(__file__).parent.parent / "work" / "empirical" / args.output
    with open(outpath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved to {outpath}")

    # Step 5: FLB bin-and-count analysis
    prices = np.array([r["price"] for r in results])
    outcomes = np.array([r["outcome"] for r in results])

    print(f"\n{'='*65}")
    print(f"POLYMARKET SUBGRAPH FLB ANALYSIS ({len(results)} observations)")
    print(f"{'='*65}")
    print(f"Mean price: {prices.mean():.3f}, Outcome rate: {outcomes.mean():.3f}")
    print()

    print(f"{'Price bin':>12} {'n':>6} {'AvgPrice':>8} {'WinRate':>8} "
          f"{'Profit':>8} {'StdDev':>8} {'t':>7}")
    print("-" * 65)

    for lo, hi, label in [
        (0, 0.10, "0-10%"), (0.10, 0.20, "10-20%"), (0.20, 0.30, "20-30%"),
        (0.30, 0.40, "30-40%"), (0.40, 0.50, "40-50%"), (0.50, 0.60, "50-60%"),
        (0.60, 0.70, "60-70%"), (0.70, 0.80, "70-80%"), (0.80, 0.90, "80-90%"),
        (0.90, 1.0, "90-100%"),
    ]:
        mask = (prices >= lo) & (prices < hi)
        n = int(mask.sum())
        if n < 5:
            continue
        avg_p = float(prices[mask].mean())
        wr = float(outcomes[mask].mean())
        profit = wr - avg_p
        profits = outcomes[mask] - prices[mask]
        std = float(profits.std()) if n > 1 else 0
        t = profit / (std / np.sqrt(n)) if std > 0 and n > 1 else 0
        sig = "***" if abs(t) > 2.58 else "** " if abs(t) > 1.96 else "   "
        print(f"{label:>12} {n:6d} {avg_p:8.3f} {wr:8.3f} "
              f"{profit:+8.3f} {std:8.3f} {t:+7.2f} {sig}")


if __name__ == "__main__":
    main()
