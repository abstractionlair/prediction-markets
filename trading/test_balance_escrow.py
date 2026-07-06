#!/usr/bin/env python3
"""
Test whether Kalshi's balance API subtracts resting order escrow.

Places a small resting order far from market (won't fill), checks
balance before/after, then cancels. Costs nothing.

Usage:
    python test_balance_escrow.py [--ticker TICKER] [--demo]
"""

import argparse
import sys
import time
import uuid

from kalshi_client import KalshiClient


def main():
    parser = argparse.ArgumentParser(description="Test Kalshi balance escrow behavior")
    parser.add_argument("--ticker", help="Market ticker to test with")
    parser.add_argument("--demo", action="store_true", help="Use demo environment")
    parser.add_argument("--contracts", type=int, default=1, help="Contracts to order (default: 1)")
    parser.add_argument("--price", type=int, default=3, help="YES price in cents (default: 3, far OTM)")
    args = parser.parse_args()

    client = KalshiClient(demo=args.demo)
    env = "DEMO" if args.demo else "PRODUCTION"
    print(f"Environment: {env}")
    print()

    # Step 1: Check initial balance
    bal1 = client.get_balance()
    balance_before = bal1["balance"]
    print(f"Step 1 — Initial balance: {balance_before}¢ (${balance_before/100:.2f})")

    # Check existing resting orders
    resting = client.get_orders(status="resting")
    if resting:
        total_escrow = sum(
            (o.get("yes_price", 0) or o.get("no_price", 0)) * o.get("remaining_count", 0)
            for o in resting
        )
        print(f"         Existing resting orders: {len(resting)}, naive escrow sum: {total_escrow}¢")
    else:
        print(f"         No existing resting orders")
    print()

    # Find a ticker if not provided
    ticker = args.ticker
    if not ticker:
        print("No ticker provided. Fetching an active market...")
        try:
            events = client.get_events(status="open", with_nested_markets=True, limit=1)
            if events and events[0].get("markets"):
                ticker = events[0]["markets"][0]["ticker"]
            else:
                print("ERROR: No active markets found")
                sys.exit(1)
        except Exception as e:
            print(f"ERROR fetching markets: {e}")
            print("Provide a ticker with --ticker")
            sys.exit(1)

    print(f"Using ticker: {ticker}")
    price = args.price
    contracts = args.contracts
    expected_cost = price * contracts
    print(f"Order: BUY {contracts} YES @ {price}¢ = {expected_cost}¢ escrow")
    print()

    # Step 2: Place a resting order far from market
    print("Step 2 — Placing resting order...")
    order_id = None
    try:
        resp = client.create_order(
            ticker=ticker,
            side="yes",
            action="buy",
            count=contracts,
            yes_price=price,
            post_only=True,
            client_order_id=str(uuid.uuid4()),
        )
        order = resp.get("order", resp)
        order_id = order.get("order_id")
        status = order.get("status")
        print(f"         Order placed: {order_id} (status: {status})")
    except Exception as e:
        print(f"ERROR placing order: {e}")
        sys.exit(1)

    # Brief pause for settlement
    time.sleep(1)

    # Step 3: Check balance after placing order
    bal2 = client.get_balance()
    balance_after = bal2["balance"]
    delta = balance_before - balance_after
    print()
    print(f"Step 3 — Balance after order: {balance_after}¢ (${balance_after/100:.2f})")
    print(f"         Delta: {delta}¢ (expected {expected_cost}¢ if API subtracts escrow)")
    print()

    # Step 4: Cancel the order
    print("Step 4 — Cancelling order...")
    try:
        client.cancel_order(order_id)
        print(f"         Order cancelled")
    except Exception as e:
        print(f"ERROR cancelling: {e}")
        print(f"         ORDER {order_id} MAY STILL BE RESTING — cancel manually!")
        sys.exit(1)

    time.sleep(1)

    # Step 5: Check balance after cancel
    bal3 = client.get_balance()
    balance_restored = bal3["balance"]
    print()
    print(f"Step 5 — Balance after cancel: {balance_restored}¢ (${balance_restored/100:.2f})")
    print()

    # Verdict
    print("=" * 60)
    print("RESULTS:")
    print(f"  Before order:  {balance_before}¢")
    print(f"  After order:   {balance_after}¢  (delta: {delta}¢)")
    print(f"  After cancel:  {balance_restored}¢")
    print()

    if delta == expected_cost:
        print("CONCLUSION: Balance API DOES subtract resting order escrow.")
        print("  → available_cents should be balance_cents directly")
        print("  → capital_in_orders subtraction in trader.py is DOUBLE COUNTING")
    elif delta == 0:
        print("CONCLUSION: Balance API does NOT subtract resting order escrow.")
        print("  → available_cents = balance_cents - capital_in_orders is CORRECT")
    else:
        print(f"CONCLUSION: Unexpected delta ({delta}¢ vs expected {expected_cost}¢).")
        print("  → Possible partial subtraction or other mechanics at play")
        print("  → Investigate further")

    if balance_restored != balance_before:
        print()
        print(f"WARNING: Balance not fully restored after cancel!")
        print(f"  Difference: {balance_before - balance_restored}¢")


if __name__ == "__main__":
    main()
