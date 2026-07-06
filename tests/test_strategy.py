"""Tests for trading/strategy.py — pure strategy logic."""

import math

from strategy import (
    TradingParams,
    detect_chain,
    edge_per_day,
    identify_tails,
    maker_fee,
    net_edge,
    optimal_quantity,
    parse_strike_value,
    rank_and_select_pairs,
    taker_fee,
)
from conftest import make_pair


# ─── maker_fee ────────────────────────────────────────────────────

class TestMakerFee:
    def test_known_value_at_90(self):
        # ceil(0.0175 * 1 * 0.9 * 0.1 * 100) = ceil(0.1575) = 1
        assert maker_fee(90, 1) == 1

    def test_known_value_at_50(self):
        # ceil(0.0175 * 1 * 0.5 * 0.5 * 100) = ceil(0.4375) = 1
        assert maker_fee(50, 1) == 1

    def test_multiple_contracts_at_90(self):
        # ceil(0.0175 * 5 * 0.9 * 0.1 * 100) = ceil(0.7875) = 1
        assert maker_fee(90, 5) == 1

    def test_multiple_contracts_at_50(self):
        # ceil(0.0175 * 5 * 0.5 * 0.5 * 100) = ceil(2.1875) = 3
        assert maker_fee(50, 5) == 3

    def test_large_quantity_at_90(self):
        # ceil(0.0175 * 20 * 0.9 * 0.1 * 100) = ceil(3.15) = 4
        assert maker_fee(90, 20) == 4

    def test_at_extreme_tail(self):
        # ceil(0.0175 * 1 * 0.97 * 0.03 * 100) = ceil(0.050925) = 1
        assert maker_fee(97, 1) == 1

    def test_at_95(self):
        # ceil(0.0175 * 1 * 0.95 * 0.05 * 100) = ceil(0.083125) = 1
        assert maker_fee(95, 1) == 1

    def test_ceil_boundary_just_above_integer(self):
        # Find a case where raw is just above an integer
        # ceil(0.0175 * 8 * 0.5 * 0.5 * 100) = ceil(3.5) = 4
        assert maker_fee(50, 8) == 4

    def test_symmetric(self):
        # Fee at 90¢ should equal fee at 10¢ (same P*(1-P))
        assert maker_fee(90, 5) == maker_fee(10, 5)

    def test_zero_price(self):
        # At 0¢, P*(1-P) = 0, fee = ceil(0) = 0
        assert maker_fee(0, 1) == 0

    def test_hundred_price(self):
        # At 100¢, P*(1-P) = 0, fee = ceil(0) = 0
        assert maker_fee(100, 1) == 0

    def test_custom_rate(self):
        # ceil(0.07 * 1 * 0.9 * 0.1 * 100) = ceil(0.63) = 1
        assert maker_fee(90, 1, rate=0.07) == 1


class TestTakerFee:
    def test_taker_higher_than_maker(self):
        # Taker rate (0.07) is 4x maker rate (0.0175)
        assert taker_fee(50, 5) >= maker_fee(50, 5)

    def test_known_value(self):
        # ceil(0.07 * 5 * 0.5 * 0.5 * 100) = ceil(8.75) = 9
        assert taker_fee(50, 5) == 9


# ─── optimal_quantity ──────────────────────────────────────────────

class TestOptimalQuantity:
    def test_returns_within_bounds(self):
        q = optimal_quantity(90, max_q=8, min_q=1)
        assert 1 <= q <= 8

    def test_minimizes_fee_per_contract(self):
        q = optimal_quantity(90, max_q=8)
        # Verify this is actually optimal
        p = 0.9
        pp = p * (1 - p)
        best_fpc = math.ceil(0.0175 * q * pp * 100) / q
        for other_q in range(1, 9):
            other_fpc = math.ceil(0.0175 * other_q * pp * 100) / other_q
            assert best_fpc <= other_fpc + 1e-10

    def test_at_midrange_price(self):
        q = optimal_quantity(50, max_q=20)
        assert 1 <= q <= 20

    def test_at_extreme_tail(self):
        q = optimal_quantity(97, max_q=8)
        assert 1 <= q <= 8

    def test_min_q_respected(self):
        q = optimal_quantity(90, max_q=8, min_q=3)
        assert q >= 3

    def test_custom_fee_rate(self):
        q = optimal_quantity(90, max_q=8, fee_rate=0.07)
        assert 1 <= q <= 8


# ─── detect_chain ──────────────────────────────────────────────────

class TestDetectChain:
    def test_btc_daily_is_chain(self):
        tickers = [
            "KXBTCD-26MAR2717-T60399.99",
            "KXBTCD-26MAR2717-T62399.99",
            "KXBTCD-26MAR2717-T64399.99",
            "KXBTCD-26MAR2717-T66399.99",
        ]
        assert detect_chain(tickers) is True

    def test_march_madness_not_chain(self):
        tickers = [
            "KXNCAAMB1HWINNER-26MAR21-ARIZ",
            "KXNCAAMB1HWINNER-26MAR21-USU",
            "KXNCAAMB1HWINNER-26MAR21-TRAN",
            "KXNCAAMB1HWINNER-26MAR21-DUKE",
        ]
        assert detect_chain(tickers) is False

    def test_tennis_not_chain(self):
        tickers = [
            "KXATPWINNER-26MAR21-CILIC",
            "KXATPWINNER-26MAR21-ZVEREV",
        ]
        assert detect_chain(tickers) is False

    def test_jobless_claims_is_chain(self):
        tickers = [
            "KXJOBLESSCLAIMS-26MAR26-215000",
            "KXJOBLESSCLAIMS-26MAR26-220000",
            "KXJOBLESSCLAIMS-26MAR26-225000",
        ]
        assert detect_chain(tickers) is True

    def test_mixed_mostly_numeric(self):
        # 3 of 4 are numeric = chain
        tickers = [
            "KXETH-26MAR-T3000",
            "KXETH-26MAR-T3500",
            "KXETH-26MAR-T4000",
            "KXETH-26MAR-WINNER",
        ]
        assert detect_chain(tickers) is True

    def test_mixed_mostly_names(self):
        # 1 of 4 is numeric = not chain
        tickers = [
            "KXNBA-26MAR-LAKERS",
            "KXNBA-26MAR-CELTICS",
            "KXNBA-26MAR-WARRIORS",
            "KXNBA-26MAR-T100",
        ]
        assert detect_chain(tickers) is False

    def test_empty_tickers(self):
        assert detect_chain([]) is False

    def test_single_numeric_ticker(self):
        # Single ticker is never a chain (need 2+ for monotone ordering)
        assert detect_chain(["KXBTC-26MAR-T50000"]) is False

    def test_single_name_ticker(self):
        assert detect_chain(["KXNBA-26MAR-LAKERS"]) is False

    def test_two_numeric_tickers_is_chain(self):
        assert detect_chain(["KXBTC-T50000", "KXBTC-T55000"]) is True


# ─── identify_tails ───────────────────────────────────────────────

class TestIdentifyTails:
    def test_yes_tail(self):
        # YES mid = 90, spread = 3 → in tail
        tails = identify_tails(yes_bid=89, yes_ask=92)
        sides = [t.side for t in tails]
        assert 'yes' in sides
        yes_tail = [t for t in tails if t.side == 'yes'][0]
        assert yes_tail.mid == 90
        assert yes_tail.spread == 3

    def test_no_tail(self):
        # YES bid=3, ask=8 → YES mid=5 (not in tail)
        # NO bid=92, ask=97 → NO mid=(92+97)//2=94 (in tail)
        tails = identify_tails(yes_bid=3, yes_ask=8)
        sides = [t.side for t in tails]
        assert 'no' in sides
        assert 'yes' not in sides
        no_tail = [t for t in tails if t.side == 'no'][0]
        assert no_tail.mid == 94

    def test_both_tails_impossible(self):
        # Can't have both YES and NO in 85-97 simultaneously
        # YES=90 → NO=10 (not in tail)
        tails = identify_tails(yes_bid=89, yes_ask=92)
        assert len(tails) == 1

    def test_midrange_no_tail(self):
        # YES mid = 50, NO mid = 50 → neither in tail
        tails = identify_tails(yes_bid=48, yes_ask=52)
        assert len(tails) == 0

    def test_spread_too_wide(self):
        # YES mid = 90 but spread = 12 > MAX_SPREAD (10)
        tails = identify_tails(yes_bid=84, yes_ask=96)
        assert len(tails) == 0

    def test_at_boundary_85(self):
        # YES mid exactly 85 → in tail
        tails = identify_tails(yes_bid=84, yes_ask=86)
        sides = [t.side for t in tails]
        assert 'yes' in sides

    def test_at_boundary_97(self):
        # YES mid exactly 97 → in tail
        tails = identify_tails(yes_bid=96, yes_ask=98)
        sides = [t.side for t in tails]
        assert 'yes' in sides

    def test_below_min_tail(self):
        # YES mid = 84 → not in tail
        tails = identify_tails(yes_bid=83, yes_ask=85)
        yes_tails = [t for t in tails if t.side == 'yes']
        assert len(yes_tails) == 0

    def test_above_max_tail(self):
        # YES mid = 98 → not in tail
        tails = identify_tails(yes_bid=97, yes_ask=99)
        yes_tails = [t for t in tails if t.side == 'yes']
        assert len(yes_tails) == 0

    def test_zero_prices(self):
        assert identify_tails(yes_bid=0, yes_ask=0) == []

    def test_crossed_quotes(self):
        # yes_ask < yes_bid is invalid
        assert identify_tails(yes_bid=92, yes_ask=89) == []

    def test_negative_prices(self):
        assert identify_tails(yes_bid=-5, yes_ask=90) == []

    def test_custom_params(self):
        params = TradingParams(min_tail=80, max_tail=99, max_spread=20)
        tails = identify_tails(yes_bid=78, yes_ask=82, params=params)
        assert len(tails) == 1


# ─── rank_and_select_pairs ─────────────────────────────────────────

class TestRankAndSelectPairs:
    def test_empty_input(self):
        assert rank_and_select_pairs([]) == []

    def test_ranks_by_edge_per_day(self):
        p1 = make_pair(event_ticker="EVT1", edge=0.03, days_to_settle=1.0)
        p2 = make_pair(event_ticker="EVT2", edge=0.01, days_to_settle=1.0)
        result = rank_and_select_pairs([p2, p1], max_events=2)
        # Both qualify; result is shuffled but both should be present
        events = {p.event_ticker for p in result}
        assert events == {"EVT1", "EVT2"}

    def test_max_events_cap(self):
        pairs = []
        for i in range(10):
            pairs.append(make_pair(
                event_ticker=f"EVT{i}",
                yes_ticker=f"T{i}-YES",
                no_ticker=f"T{i}-NO",
                edge=0.03 - i * 0.002,
                days_to_settle=1.0,
            ))
        result = rank_and_select_pairs(pairs, max_events=3)
        events = {p.event_ticker for p in result}
        assert len(events) == 3

    def test_top_events_selected(self):
        # Edge descending: EVT0 (0.03), EVT1 (0.028), ..., EVT9 (0.012)
        pairs = []
        for i in range(10):
            pairs.append(make_pair(
                event_ticker=f"EVT{i}",
                yes_ticker=f"T{i}-YES",
                no_ticker=f"T{i}-NO",
                edge=0.03 - i * 0.002,
                days_to_settle=1.0,
            ))
        result = rank_and_select_pairs(pairs, max_events=3)
        events = {p.event_ticker for p in result}
        # Top 3 by edge/day should be EVT0, EVT1, EVT2
        assert events == {"EVT0", "EVT1", "EVT2"}

    def test_single_pair(self):
        p = make_pair()
        result = rank_and_select_pairs([p], max_events=25)
        assert len(result) == 1


# ─── net_edge ──────────────────────────────────────────────────────

class TestNetEdge:
    def test_positive_net_edge(self):
        # 2% edge at 90¢, 8 contracts: fee = ceil(0.0175*8*0.9*0.1*100) = 2¢
        # fee/contract = 0.25¢, net = 0.02 - 0.0025 = 0.0175
        ne = net_edge(0.02, 90, 8)
        assert abs(ne - 0.0175) < 1e-10

    def test_fee_kills_small_edge(self):
        # 0.1% edge at 90¢, 1 contract: fee = 1¢
        # fee/contract = 1¢, net = 0.001 - 0.01 = -0.009
        ne = net_edge(0.001, 90, 1)
        assert ne < 0

    def test_larger_order_better_net(self):
        # Same edge, more contracts → better fee amortization
        ne_1 = net_edge(0.02, 90, 1)
        ne_8 = net_edge(0.02, 90, 8)
        assert ne_8 > ne_1

    def test_tail_prices_low_fees(self):
        # At 97¢, P*(1-P) is very small → fees near zero
        ne = net_edge(0.02, 97, 8)
        assert ne > 0.018  # almost all edge preserved

    def test_midrange_high_fees(self):
        # At 50¢, fees are highest
        ne_50 = net_edge(0.02, 50, 8)
        ne_90 = net_edge(0.02, 90, 8)
        assert ne_50 < ne_90

    def test_zero_edge(self):
        ne = net_edge(0.0, 90, 8)
        assert ne < 0  # fees make it negative


# ─── edge_per_day ──────────────────────────────────────────────────

class TestEdgePerDay:
    def test_normal(self):
        assert edge_per_day(0.03, 3.0) == 0.01

    def test_floor_prevents_infinity(self):
        # 1/48 ≈ 0.0208 days ≈ 30 minutes
        result = edge_per_day(0.03, 0.001)
        assert result == 0.03 / (1/48)

    def test_zero_edge(self):
        assert edge_per_day(0.0, 1.0) == 0.0


# ─── parse_strike_value ─────────────────────────────────────────────

class TestParseStrikeValue:
    def test_t_prefix(self):
        assert parse_strike_value("KXBTCD-26MAR2717-T60399.99") == 60399.99

    def test_bare_numeric(self):
        assert parse_strike_value("KXJOBLESSCLAIMS-26MAR26-215000") == 215000

    def test_no_strike(self):
        assert parse_strike_value("KXNBA-26MAR23-LAKERS") is None

    def test_negative_strike(self):
        assert parse_strike_value("KXTEST-T-50") == -50.0


# ─── TradingParams ────────────────────────────────────────────────

class TestTradingParams:
    def test_defaults(self):
        p = TradingParams()
        assert p.min_tail == 85
        assert p.max_tail == 97
        assert p.max_spread == 10
        assert p.maker_fee_rate == 0.0175

    def test_max_spread_dollars(self):
        p = TradingParams(max_spread=10)
        assert p.max_spread_dollars == 0.10

    def test_custom_params(self):
        p = TradingParams(min_tail=80, max_tail=99)
        assert p.min_tail == 80
        assert p.max_tail == 99
