"""
Kalshi API client for the FLB tail trading system.

Handles authentication (RSA-PSS), REST API calls, and WebSocket connections.
Supports both production and demo environments.
"""

import base64
import os
import time

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Endpoints
PROD_BASE = "https://api.elections.kalshi.com"
DEMO_BASE = "https://demo-api.kalshi.co"
PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"
API_PREFIX = "/trade-api/v2"


CONFIG_DIR = os.path.expanduser("~/.config/kalshi")


class KalshiClient:
    """Authenticated Kalshi REST API client."""

    def __init__(self, key_id: str = None, private_key_path: str = None,
                 demo: bool = False):
        """Initialize client.

        If key_id/private_key_path are not provided, loads from
        ~/.config/kalshi/key_id and ~/.config/kalshi/private_key.pem
        (same location as the kalshi_collector).
        """
        if key_id is None:
            key_id = open(os.path.join(CONFIG_DIR, "key_id")).read().strip()
        if private_key_path is None:
            private_key_path = os.path.join(CONFIG_DIR, "private_key.pem")

        self.key_id = key_id
        self.private_key = self._load_key(private_key_path)
        self.base_url = DEMO_BASE if demo else PROD_BASE
        self.ws_url = DEMO_WS if demo else PROD_WS
        self.demo = demo
        self.session = requests.Session()

    @staticmethod
    def _load_key(path: str):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend())

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS signature over timestamp + method + path."""
        msg = (timestamp_ms + method + path).encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        # Strip query params for signing
        sign_path = path.split("?")[0]
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return self.base_url + path

    def get(self, path: str, params: dict = None) -> dict:
        full_path = API_PREFIX + path
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                full_path += "?" + qs
        r = self.session.get(
            self._url(full_path),
            headers=self._headers("GET", full_path),
        )
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        full_path = API_PREFIX + path
        r = self.session.post(
            self._url(full_path),
            headers=self._headers("POST", full_path),
            json=body,
        )
        r.raise_for_status()
        return r.json()

    def delete(self, path: str) -> dict:
        full_path = API_PREFIX + path
        r = self.session.delete(
            self._url(full_path),
            headers=self._headers("DELETE", full_path),
        )
        r.raise_for_status()
        return r.json()

    # ── Convenience methods ──────────────────────────────────────────

    def get_balance(self) -> dict:
        return self.get("/portfolio/balance")

    def get_positions(self, limit=200) -> list:
        """Get all open positions, paginating if needed."""
        positions = []
        cursor = None
        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/portfolio/positions", params)
            positions.extend(resp.get("market_positions", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return positions

    def get_orders(self, status: str = "resting", limit=200) -> list:
        """Get orders by status, paginating if needed."""
        orders = []
        cursor = None
        while True:
            params = {"limit": limit, "status": status}
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/portfolio/orders", params)
            orders.extend(resp.get("orders", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return orders

    def get_events(self, status: str = "open", series_ticker: str = None,
                   with_nested_markets: bool = True, limit=100) -> list:
        """Get events, optionally filtered by series."""
        events = []
        cursor = None
        while True:
            params = {
                "limit": limit,
                "status": status,
                "with_nested_markets": str(with_nested_markets).lower(),
            }
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            resp = self.get("/events", params)
            events.extend(resp.get("events", []))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return events

    def get_market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        """Get orderbook for a market.

        Returns parsed bids as integer cents for convenience:
        {
            'yes': [[price_cents, quantity], ...],  # descending by price
            'no': [[price_cents, quantity], ...],
        }
        """
        raw = self.get(f"/markets/{ticker}/orderbook")
        ob_fp = raw.get("orderbook_fp", raw.get("orderbook", {}))
        yes_raw = ob_fp.get("yes_dollars", ob_fp.get("yes", []))
        no_raw = ob_fp.get("no_dollars", ob_fp.get("no", []))

        def parse_bids(bids):
            result = []
            for entry in bids:
                if isinstance(entry, list) and len(entry) == 2:
                    price = entry[0]
                    qty = entry[1]
                    # Handle both string ("0.9300") and int (93) formats
                    if isinstance(price, str):
                        price_cents = int(round(float(price) * 100))
                    else:
                        price_cents = int(price)
                    if isinstance(qty, str):
                        quantity = int(round(float(qty)))
                    else:
                        quantity = int(qty)
                    result.append([price_cents, quantity])
            # API returns ascending order. Reverse so best bid is first.
            result.reverse()
            return result

        return {
            'yes': parse_bids(yes_raw),
            'no': parse_bids(no_raw),
        }

    def create_order(self, ticker: str, side: str, action: str = "buy",
                     count: int = 1, yes_price: int = None,
                     no_price: int = None, post_only: bool = True,
                     client_order_id: str = None) -> dict:
        """Place a limit order.

        Args:
            ticker: Market ticker
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            count: Number of contracts
            yes_price: Price in cents (1-99) for YES side
            no_price: Price in cents (1-99) for NO side
            post_only: If True, order will only rest (maker). Rejects if it
                       would immediately match (prevents accidental taking).
            client_order_id: Optional custom ID for tracking
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "post_only": post_only,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self.post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self.delete(f"/portfolio/orders/{order_id}")

    def get_queue_positions(self, tickers: list[str] = None,
                            event_ticker: str = None) -> dict:
        """Get queue positions for resting orders on given markets.

        Requires either market_tickers or event_ticker.
        Returns dict mapping order_id -> queue_position (contracts ahead as float),
        or empty dict if no resting orders.

        API response format:
            {"queue_positions": [{"order_id": "...", "market_ticker": "...",
                                  "queue_position_fp": "10.00"}, ...]}
        """
        params = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        elif tickers:
            params["market_tickers"] = ",".join(tickers)
        else:
            raise ValueError("Must provide tickers or event_ticker")
        resp = self.get("/portfolio/orders/queue_positions", params)
        raw = resp.get("queue_positions") or []
        return {
            item["order_id"]: float(item["queue_position_fp"])
            for item in raw
            if "order_id" in item and "queue_position_fp" in item
        }

    def ws_auth_headers(self) -> dict:
        """Headers for WebSocket authentication."""
        ts = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, "GET", path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
