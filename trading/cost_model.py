"""
Cost model for prediction market trading.

Formalizes transaction cost assumptions so backtests and live trading
share the same cost structure explicitly. Wraps the canonical fee
functions from strategy.py.
"""

from dataclasses import dataclass

from strategy import maker_fee as _maker_fee, taker_fee as _taker_fee


@dataclass(frozen=True)
class CostModel:
    """Transaction cost assumptions for a trading venue.

    Default values are Kalshi's current fee schedule.
    Frozen to prevent accidental mutation.
    """
    maker_fee_rate: float = 0.0175
    taker_fee_rate: float = 0.07

    def maker_fee(self, price_cents: int, contracts: int = 1) -> int:
        """Maker fee in cents."""
        return _maker_fee(price_cents, contracts, rate=self.maker_fee_rate)

    def taker_fee(self, price_cents: int, contracts: int = 1) -> int:
        """Taker fee in cents."""
        return _taker_fee(price_cents, contracts, rate=self.taker_fee_rate)

    def entry_cost(self, price_cents: int, contracts: int = 1,
                   is_maker: bool = True) -> int:
        """Total fee for entering a position."""
        if is_maker:
            return self.maker_fee(price_cents, contracts)
        return self.taker_fee(price_cents, contracts)


# Default Kalshi cost model
KALSHI_COSTS = CostModel()
