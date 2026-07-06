#!/usr/bin/env python3
"""Test FinFeedAPI to understand what data is available."""

import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env"))

API_KEY = os.getenv("FINFEED_API_KEY")
BASE_URL = "https://api.prediction-markets.finfeedapi.com"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


def test_endpoint(name: str, path: str, params: dict = None):
    """Test an endpoint and print results."""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"GET {path}")
    if params:
        print(f"Params: {params}")
    print("-" * 60)

    try:
        resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params)
        print(f"Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                print(f"Returned list with {len(data)} items")
                if data:
                    print(f"First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'not a dict'}")
                    print(f"First item sample: {data[0]}")
            elif isinstance(data, dict):
                print(f"Keys: {list(data.keys())}")
                # Print first few key-value pairs
                for k, v in list(data.items())[:5]:
                    if isinstance(v, list):
                        print(f"  {k}: list[{len(v)}]")
                    else:
                        print(f"  {k}: {v}")
        else:
            print(f"Error: {resp.text[:500]}")
    except Exception as e:
        print(f"Exception: {e}")


def main():
    print("FinFeedAPI Prediction Markets Test")
    print(f"API Key present: {bool(API_KEY)}")
    print(f"API Key prefix: {API_KEY[:8]}..." if API_KEY else "NO KEY")

    # Test 1: List exchanges
    test_endpoint("List Exchanges", "/v1/exchanges")

    # Test 2: List OHLCV periods
    test_endpoint("OHLCV Periods", "/v1/ohlcv/periods")

    # Test 3: List markets on Polymarket (history = includes resolved)
    test_endpoint(
        "Polymarket Markets History",
        "/v1/markets/POLYMARKET/history",
        {"limit": 5}
    )

    # Test 4: List active markets on Kalshi
    test_endpoint(
        "Kalshi Active Markets",
        "/v1/markets/KALSHI/active",
        {"limit": 5}
    )

    # Test 5: Get OHLCV with period
    test_endpoint(
        "Polymarket OHLCV (daily)",
        "/v1/ohlcv/POLYMARKET/history",
        {"period_id": "1DAY", "limit": 5}
    )

    # Test 6: Get more markets to find resolved ones
    test_endpoint(
        "Polymarket Markets (100)",
        "/v1/markets/POLYMARKET/history",
        {"limit": 100}
    )

    # Test 7: Count markets by checking pagination
    test_endpoint(
        "Polymarket Markets offset 1000",
        "/v1/markets/POLYMARKET/history",
        {"limit": 5, "offset": 1000}
    )

    # Test 8: List all exchanges
    test_endpoint(
        "All Exchanges Details",
        "/v1/exchanges"
    )


def check_all_exchanges():
    """Check market counts across all exchanges."""
    print("\n" + "="*60)
    print("Checking all exchanges")
    print("-"*60)

    # Get exchanges
    resp = requests.get(f"{BASE_URL}/v1/exchanges", headers=headers)
    exchanges = resp.json()

    for ex in exchanges:
        ex_id = ex['exchange_id']
        print(f"\n{ex_id}:")

        # Try history endpoint
        resp = requests.get(
            f"{BASE_URL}/v1/markets/{ex_id}/history",
            headers=headers,
            params={"limit": 500}
        )
        if resp.status_code == 200:
            data = resp.json()
            status_counts = {}
            for m in data:
                status = m.get("status", "unknown") if isinstance(m, dict) else "id_only"
                status_counts[status] = status_counts.get(status, 0) + 1
            print(f"  History endpoint: {len(data)} markets")
            for s, c in sorted(status_counts.items()):
                print(f"    {s}: {c}")
        else:
            print(f"  History endpoint: {resp.status_code}")

        # Try active endpoint
        resp = requests.get(
            f"{BASE_URL}/v1/markets/{ex_id}/active",
            headers=headers,
            params={"limit": 10}
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Active endpoint: {len(data)} returned")


def test_market_ohlcv():
    """Test OHLCV for a specific market."""
    print("\n" + "="*60)
    print("Testing market-specific OHLCV")
    print("-"*60)

    # Get a market ID
    resp = requests.get(
        f"{BASE_URL}/v1/markets/POLYMARKET/active",
        headers=headers,
        params={"limit": 1}
    )
    if resp.status_code == 200:
        market_ids = resp.json()
        if market_ids:
            market_id = market_ids[0] if isinstance(market_ids[0], str) else market_ids[0].get('market_id')
            print(f"Testing with market: {market_id[:50]}...")

            # Try OHLCV history
            resp = requests.get(
                f"{BASE_URL}/v1/ohlcv/POLYMARKET/{market_id}/history",
                headers=headers,
                params={"period_id": "1DAY", "limit": 5}
            )
            print(f"OHLCV status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"Returned: {len(data) if isinstance(data, list) else 'dict'}")
                if isinstance(data, list) and data:
                    print(f"Keys: {list(data[0].keys())}")
                    print(f"Sample: {data[0]}")
            else:
                print(f"Error: {resp.text[:300]}")


if __name__ == "__main__":
    main()
    check_all_exchanges()
    test_market_ohlcv()
