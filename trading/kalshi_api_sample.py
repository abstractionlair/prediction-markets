#!/usr/bin/env python3
"""Kalshi API sample: query market data, fetch orderbook, place and cancel an order."""

import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Load credentials from environment variables
key_id = os.environ["KALSHI_API_KEY_ID"]
with open(os.environ["KALSHI_PRIVATE_KEY_PATH"], "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)


def api(method, path, body=None):
    ts = str(int(time.time() * 1000))
    sign_path = ("/trade-api/v2" + path).split("?")[0]
    sig = private_key.sign(
        (ts + method + sign_path).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                     salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256())
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    resp = requests.request(method, BASE_URL + path, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()


# 1. Query market data for today's NYC weather
events = api("GET", "/events?series_ticker=KXHIGHNY&status=open"
             "&with_nested_markets=true")
event = next(e for e in events["events"] if "Mar 22" in e["title"])
print(f"{event['title']} ({len(event['markets'])} markets)")
for m in event["markets"]:
    print(f"  {m['ticker']}  bid={m['yes_bid_dollars']} ask={m['yes_ask_dollars']}")

# 2. Query the orderbook
ticker = event["markets"][0]["ticker"]
ob = api("GET", f"/markets/{ticker}/orderbook")["orderbook_fp"]
print(f"\nOrderbook for {ticker}:")
print(f"  YES bids: {ob['yes_dollars'][-3:]}")
print(f"  NO bids:  {ob['no_dollars'][-3:]}")

# 3. Place and cancel an order (1 contract at $0.01, won't fill)
order = api("POST", "/portfolio/orders", {
    "ticker": ticker, "side": "yes", "action": "buy",
    "count": 1, "type": "limit", "yes_price": 1, "post_only": True,
})["order"]
print(f"\nPlaced order {order['order_id']} (status={order['status']})")

api("DELETE", f"/portfolio/orders/{order['order_id']}")
print("Cancelled.")
