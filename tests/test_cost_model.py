"""Tests for trading/cost_model.py."""

from cost_model import CostModel, KALSHI_COSTS
from strategy import maker_fee


class TestCostModel:
    def test_maker_fee_matches_canonical(self):
        """CostModel.maker_fee must produce identical results to strategy.maker_fee."""
        cm = CostModel()
        for price in [5, 10, 50, 85, 90, 95, 97]:
            for contracts in [1, 5, 8, 20]:
                assert cm.maker_fee(price, contracts) == maker_fee(price, contracts), \
                    f"Mismatch at price={price}, contracts={contracts}"

    def test_taker_higher_than_maker(self):
        cm = CostModel()
        assert cm.taker_fee(90, 5) >= cm.maker_fee(90, 5)

    def test_entry_cost_maker(self):
        cm = CostModel()
        assert cm.entry_cost(90, 5, is_maker=True) == cm.maker_fee(90, 5)

    def test_entry_cost_taker(self):
        cm = CostModel()
        assert cm.entry_cost(90, 5, is_maker=False) == cm.taker_fee(90, 5)

    def test_custom_rates(self):
        cm = CostModel(maker_fee_rate=0.01, taker_fee_rate=0.05)
        # Custom rate should produce different results than default
        assert cm.maker_fee(50, 10) != KALSHI_COSTS.maker_fee(50, 10)

    def test_frozen(self):
        """CostModel should be immutable."""
        cm = CostModel()
        try:
            cm.maker_fee_rate = 0.99
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_kalshi_costs_singleton(self):
        assert KALSHI_COSTS.maker_fee_rate == 0.0175
        assert KALSHI_COSTS.taker_fee_rate == 0.07
