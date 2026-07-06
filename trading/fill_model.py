"""
Limit order fill model.

Models how a resting limit order fills over time given candle data.
Orthogonal component — knows nothing about trading strategies.

Two interfaces:
- check_fill(side, price, remaining, candle) → int
    Per-period: how many contracts fill in one candle? Stateless.
    Used by incremental simulations that drive their own time loop.

- simulate_order(side, price, contracts, candles) → list[Fill]
    Batch: given a sequence of candles from placement to expiry,
    return the timestamped fill schedule. Used for standalone testing
    and analysis.

Both interfaces are public. Implementation details (_price_touched,
_fillable_contracts) are private.

V1: Simple volume-based partial fills. A resting bid can capture up to
capture_rate fraction of each candle's volume, conditional on the price
being touched. Future versions should model:
- Queue position (earlier orders fill first)
- Book depth impact (large orders move the price)
- Time-of-day liquidity patterns
- Adverse selection (fills in losing markets are easier to get)
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CandleData:
    """Minimal candle data needed for fill simulation."""
    yes_bid_high: int   # cents — highest YES bid during the period
    yes_ask_low: int    # cents — lowest YES ask during the period
    volume: int         # contracts traded during the period
    price_high: int = 0  # cents — highest trade price (0 = unavailable)
    price_low: int = 0   # cents — lowest trade price (0 = unavailable)


@dataclass(frozen=True)
class TimestampedCandle:
    """Candle data with its period timestamp, for batch simulation."""
    period_end: datetime
    candle: CandleData


@dataclass(frozen=True)
class Fill:
    """A single fill event: some contracts filled at a point in time."""
    time: datetime
    contracts: int


@dataclass(frozen=True)
class FillResult:
    """Summary result of a batch fill simulation."""
    contracts_requested: int
    contracts_filled: int
    fills: tuple[Fill, ...]     # timestamped fill events (immutable)
    fill_complete: bool

    @property
    def fill_rate(self) -> float:
        if self.contracts_requested == 0:
            return 0.0
        return self.contracts_filled / self.contracts_requested

    @property
    def fill_events(self) -> int:
        """Number of candle periods in which fills occurred (not elapsed time)."""
        return len(self.fills)


class FillModel:
    """Volume-based limit order fill model.

    Args:
        capture_rate: fraction of candle volume a single resting order
            can capture. Default 0.20 (20%). Calibrate from live data
            when available.
        min_fill_per_touch: minimum contracts to fill when price is
            touched, even if volume * capture_rate < 1. Default 1.
    """

    def __init__(self, capture_rate: float = 0.20, min_fill_per_touch: int = 1,
                 require_volume: bool = True):
        if capture_rate < 0:
            raise ValueError(f"capture_rate must be >= 0, got {capture_rate}")
        if min_fill_per_touch < 0:
            raise ValueError(f"min_fill_per_touch must be >= 0, got {min_fill_per_touch}")
        self.capture_rate = capture_rate
        self.min_fill_per_touch = min_fill_per_touch
        self.require_volume = require_volume

    # ── Per-period interface (for incremental simulation) ─────────

    def check_fill(self, side: str, limit_price: int, remaining: int,
                   candle: CandleData) -> int:
        """How many contracts fill in a single candle period?

        Args:
            side: 'yes' or 'no'
            limit_price: resting bid price in cents
            remaining: contracts still unfilled
            candle: this period's market data

        Returns:
            Number of contracts filled (0 if price not touched or
            remaining is 0).
        """
        if remaining <= 0:
            return 0
        if not self._price_touched(side, limit_price, candle):
            return 0
        return self._fillable_contracts(candle.volume, remaining)

    # ── Batch interface (for standalone analysis and testing) ─────

    def simulate_order(self, side: str, limit_price: int, contracts: int,
                       candles: list[TimestampedCandle]) -> FillResult:
        """Simulate a resting order across a sequence of candle periods.

        Args:
            side: 'yes' or 'no'
            limit_price: resting bid price in cents
            contracts: total contracts to fill
            candles: ordered candle data from placement to expiry,
                     with timestamps

        Returns:
            FillResult with timestamped fill events.
        """
        if contracts <= 0:
            return FillResult(0, 0, (), True)

        remaining = contracts
        fills = []

        for tc in candles:
            filled = self.check_fill(side, limit_price, remaining, tc.candle)
            if filled > 0:
                fills.append(Fill(time=tc.period_end, contracts=filled))
                remaining -= filled
                if remaining <= 0:
                    return FillResult(contracts, contracts, tuple(fills), True)

        total_filled = contracts - remaining
        return FillResult(contracts, total_filled, tuple(fills), total_filled >= contracts)

    # ── Private implementation ────────────────────────────────────

    def _price_touched(self, side: str, limit_price: int,
                       candle: CandleData) -> bool:
        """Check if our limit price was touched during this candle.

        A YES buy at limit_price fills if the lowest ask OR the lowest
        trade price reached our limit or below.  Analogously for NO
        (using yes_bid_high / price_high, since NO price = 100 - YES price).
        """
        if side == 'yes':
            if candle.yes_ask_low <= limit_price:
                return True
            if candle.price_low > 0 and candle.price_low <= limit_price:
                return True
            return False
        elif side == 'no':
            no_ask_low = 100 - candle.yes_bid_high
            if no_ask_low <= limit_price:
                return True
            if candle.price_high > 0 and (100 - candle.price_high) <= limit_price:
                return True
            return False
        else:
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    def _fillable_contracts(self, candle_volume: int, remaining: int) -> int:
        """How many contracts can fill in this candle.

        With require_volume=True (default), only fills when actual volume
        traded. With require_volume=False, any price touch fills at least
        min_fill_per_touch — this matches live experience at small order
        sizes where our order IS the volume.
        """
        if self.require_volume and candle_volume <= 0:
            return 0

        if candle_volume <= 0:
            return min(self.min_fill_per_touch, remaining)

        available = max(
            int(candle_volume * self.capture_rate),
            self.min_fill_per_touch,
        )
        return min(available, remaining)


# Default model — calibrate capture_rate from live data when available
DEFAULT_FILL_MODEL = FillModel(capture_rate=0.20)
