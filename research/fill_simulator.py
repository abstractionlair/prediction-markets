"""Event-driven fill simulator with latent queue state.

Simulates whether resting limit orders would fill by walking the historical
trade tape forward from order placement. Vectorized over multiple candidate
prices for the same market and time window.

Two modes:
- Backtest: Q_ahead drawn from a prior (unknown historical book state)
- Live: Q_ahead observed from API orderbook snapshot

The simulator is the ground truth for the fill model. The fill model's
probability predictions must be calibrated against this simulator's
fill rates (consistency requirement from fill-model-requirements.md).

Usage:
    from research.fill_simulator import FillSimulator, SimOrder

    # Single order
    order = SimOrder(side='no', price_cents=93, quantity=8, q_ahead=50.0)
    sim = FillSimulator(trades)  # trades for one ticker
    result = sim.run(order, t0, t_end)

    # Vectorized: multiple prices, same market
    results = sim.run_batch(
        side='no',
        prices=[90, 91, 92, 93, 94],
        quantities=[4, 4, 4, 4, 4],
        q_aheads=[100, 80, 50, 30, 10],
        t0=t0, t_end=t_end,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

import numpy as np


class Trade(NamedTuple):
    """A single trade from the tape."""
    timestamp: datetime
    quantity: int
    yes_price_cents: int
    taker_side: str  # 'yes' or 'no'


@dataclass
class FillEvent:
    """A fill that occurred during simulation."""
    timestamp: datetime
    contracts: int


@dataclass
class SimResult:
    """Result of simulating one order."""
    side: str
    price_cents: int
    quantity: int
    q_ahead_initial: float
    fills: list[FillEvent] = field(default_factory=list)

    @property
    def contracts_filled(self) -> int:
        return sum(f.contracts for f in self.fills)

    @property
    def fully_filled(self) -> bool:
        return self.contracts_filled >= self.quantity

    @property
    def fill_fraction(self) -> float:
        return min(1.0, self.contracts_filled / self.quantity) if self.quantity > 0 else 0.0

    @property
    def time_to_first_fill(self) -> float | None:
        """Seconds from simulation start to first fill, or None."""
        if not self.fills:
            return None
        return self._first_fill_offset

    def set_t0(self, t0: datetime):
        """Set reference time for time_to_first_fill calculation."""
        self._t0 = t0
        if self.fills:
            self._first_fill_offset = (
                self.fills[0].timestamp - t0).total_seconds()


class FillSimulator:
    """Event-driven fill simulator for a single ticker.

    Walks the trade tape forward from t0, depleting queue-ahead and
    then filling the simulated order. Supports vectorized operation
    over multiple candidate prices.

    The simulator makes one assumption about queue mechanics:
    opposing trades at our price level deplete the queue ahead of us
    first (FIFO / price-time priority), then fill our order.

    What it does NOT model (yet):
    - Cancellations ahead of us (would accelerate fills)
    - New orders arriving ahead of us (impossible — we have time priority)
    - Price improvement (trades through our level filling us at our price)
    - Our order's impact on the market
    """

    def __init__(self, trades: list[Trade]):
        """Initialize with trades for a single ticker, sorted by timestamp.

        Args:
            trades: List of Trade namedtuples, sorted ascending by timestamp.
        """
        self._trades = trades

        # Pre-extract numpy arrays for vectorized operations
        if trades:
            self._timestamps = [t.timestamp for t in trades]
            self._quantities = np.array([t.quantity for t in trades], dtype=np.int64)
            self._yes_prices = np.array([t.yes_price_cents for t in trades], dtype=np.int64)
            self._taker_sides = np.array([t.taker_side for t in trades])
        else:
            self._timestamps = []
            self._quantities = np.array([], dtype=np.int64)
            self._yes_prices = np.array([], dtype=np.int64)
            self._taker_sides = np.array([])

    @staticmethod
    def _is_opposing(taker_side: np.ndarray, order_side: str) -> np.ndarray:
        """Boolean mask: which trades have opposing taker side."""
        if order_side == 'yes':
            return taker_side == 'no'
        else:
            return taker_side == 'yes'

    @staticmethod
    def _hits_price(yes_prices: np.ndarray, order_side: str,
                    limit_yes_price_cents: int) -> np.ndarray:
        """Boolean mask: trades at-or-through our level, from the counterfactual.

        All prices are in yes_price terms (as recorded in the trade tape).

        For YES buy at yes_price L (we're a YES bid): NO takers sell YES and
        hit the highest bid first. Flow sweeps from high yes_price DOWN.
        A trade at yes_price<=L represents contra flow that reaches our level
        in the counterfactual where we were at L.

        For NO buy at yes_price L (we're a YES ask at L, i.e., NO bid at 100-L):
        YES takers buy YES and hit the lowest ask first. Flow sweeps from low
        yes_price UP. A trade at yes_price>=L represents contra flow that
        reaches our level.
        """
        if order_side == 'yes':
            return yes_prices <= limit_yes_price_cents
        return yes_prices >= limit_yes_price_cents

    @staticmethod
    def _sweeps_through(yes_prices: np.ndarray, order_side: str,
                        limit_yes_price_cents: int) -> np.ndarray:
        """Boolean mask: trades that strictly passed our level (queue cleared).

        A trade strictly past our level means the taker cleared our level and
        continued further. Under price-time priority, our queue must have been
        fully consumed. When this happens, reset our simulated queue to 0
        before using the trade's remaining quantity to fill us.
        """
        if order_side == 'yes':
            # YES bidder: sweep continues DOWN past our price
            return yes_prices < limit_yes_price_cents
        # NO bidder (YES ask): sweep continues UP past our price
        return yes_prices > limit_yes_price_cents

    def run(self, side: str, price_cents: int, quantity: int,
            q_ahead: float, t0: datetime, t_end: datetime) -> SimResult:
        """Simulate a single order.

        Args:
            side: 'yes' or 'no'
            price_cents: limit price in cents
            quantity: number of contracts
            q_ahead: estimated contracts ahead of us in the queue
            t0: order placement time
            t_end: simulation end (settlement time)

        Returns:
            SimResult with fill events.
        """
        result = SimResult(
            side=side, price_cents=price_cents,
            quantity=quantity, q_ahead_initial=q_ahead,
        )

        remaining = quantity
        queue = q_ahead

        for trade in self._trades:
            if trade.timestamp < t0:
                continue
            if trade.timestamp > t_end:
                break
            if remaining <= 0:
                break

            # Check if this trade is opposing and would reach our level.
            # For YES buy at L: opposing = no-taker; reaches us if yes_price <= L.
            # For NO buy at L:  opposing = yes-taker; reaches us if yes_price >= L.
            if side == 'yes':
                opposing = trade.taker_side == 'no'
                hits = trade.yes_price_cents <= price_cents
                sweeps_through = trade.yes_price_cents < price_cents
            else:
                opposing = trade.taker_side == 'yes'
                hits = trade.yes_price_cents >= price_cents
                sweeps_through = trade.yes_price_cents > price_cents

            if not (opposing and hits):
                continue

            # If the trade strictly passed our level, price-time priority
            # implies our queue was fully cleared before this trade.
            if sweeps_through:
                queue = 0

            # Deplete queue ahead first, then fill us
            v = trade.quantity
            if queue > 0:
                take_from_queue = min(queue, v)
                queue -= take_from_queue
                v -= take_from_queue

            if v > 0 and remaining > 0:
                fill_qty = min(remaining, v)
                remaining -= fill_qty
                result.fills.append(FillEvent(
                    timestamp=trade.timestamp,
                    contracts=fill_qty,
                ))

        result.set_t0(t0)
        return result

    def run_batch(self, side: str, prices: list[int],
                  quantities: list[int], q_aheads: list[float],
                  t0: datetime, t_end: datetime) -> list[SimResult]:
        """Simulate multiple orders on the same market, vectorized.

        All orders share the same side, time window, and trade tape.
        Vectorized over prices — one pass through the tape updates
        all candidates simultaneously.

        Args:
            side: 'yes' or 'no' (same for all candidates)
            prices: limit prices in cents for each candidate
            quantities: contract counts for each candidate
            q_aheads: queue-ahead estimates for each candidate
            t0: order placement time
            t_end: simulation end

        Returns:
            List of SimResult, one per candidate.
        """
        n = len(prices)
        assert len(quantities) == n and len(q_aheads) == n

        # State arrays
        remaining = np.array(quantities, dtype=np.float64)
        queues = np.array(q_aheads, dtype=np.float64)
        prices_arr = np.array(prices, dtype=np.int64)

        # Results
        results = [
            SimResult(side=side, price_cents=prices[i],
                      quantity=quantities[i], q_ahead_initial=q_aheads[i])
            for i in range(n)
        ]

        # Filter trades to time window first
        if not self._trades:
            for r in results:
                r.set_t0(t0)
            return results

        # Find start/end indices via binary search
        start_idx = 0
        for i, t in enumerate(self._timestamps):
            if t >= t0:
                start_idx = i
                break
        else:
            start_idx = len(self._timestamps)

        # Pre-compute opposing mask (same for all prices since same side)
        opposing = self._is_opposing(self._taker_sides, side)

        for idx in range(start_idx, len(self._trades)):
            ts = self._timestamps[idx]
            if ts > t_end:
                break

            # Skip non-opposing trades
            if not opposing[idx]:
                continue

            trade_qty = int(self._quantities[idx])
            trade_price = int(self._yes_prices[idx])

            # Which candidates does this trade reach? (in counterfactual)
            # YES buy at L: reaches if trade price <= L (sweeps from high down)
            # NO buy at L:  reaches if trade price >= L (sweeps from low up)
            if side == 'yes':
                hits = trade_price <= prices_arr
                sweeps = trade_price < prices_arr
            else:
                hits = trade_price >= prices_arr
                sweeps = trade_price > prices_arr

            # Process each hit candidate
            # (Can't fully vectorize fills because each candidate has
            # independent queue/remaining state and we need fill events)
            for i in range(n):
                if not hits[i] or remaining[i] <= 0:
                    continue

                # Queue cleared if trade passed strictly past our level
                if sweeps[i]:
                    queues[i] = 0

                v = trade_qty

                # Deplete queue
                if queues[i] > 0:
                    take = min(queues[i], v)
                    queues[i] -= take
                    v -= take

                # Fill us
                if v > 0:
                    fill_qty = min(int(remaining[i]), v)
                    remaining[i] -= fill_qty
                    results[i].fills.append(FillEvent(
                        timestamp=ts, contracts=fill_qty,
                    ))

            # Early exit if all candidates filled
            if np.all(remaining <= 0):
                break

        for r in results:
            r.set_t0(t0)
        return results


# --- Convenience for loading trades from DB ---


def load_trades(conn, ticker: str, t0: datetime | None = None,
                t_end: datetime | None = None) -> list[Trade]:
    """Load trades for a ticker from the database.

    Args:
        conn: Database connection.
        ticker: Market ticker.
        t0: Optional start time filter.
        t_end: Optional end time filter.

    Returns:
        List of Trade namedtuples, sorted by timestamp.
    """
    cur = conn.cursor()
    query = """
        SELECT created_time, count, yes_price, taker_side
        FROM prediction_markets.kalshi_trades
        WHERE ticker = %s
    """
    params = [ticker]
    if t0:
        query += " AND created_time >= %s"
        params.append(t0)
    if t_end:
        query += " AND created_time <= %s"
        params.append(t_end)
    query += " ORDER BY created_time"

    cur.execute(query, params)
    trades = []
    for created_time, count, yes_price, taker_side in cur:
        # yes_price may be stored as dollars (float) or cents (int)
        price_cents = int(round(float(yes_price) * 100)) if float(yes_price) < 2 else int(yes_price)
        trades.append(Trade(
            timestamp=created_time,
            quantity=int(count),
            yes_price_cents=price_cents,
            taker_side=taker_side,
        ))
    cur.close()
    return trades
