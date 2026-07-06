"""Tests for trading/flb_strategy.py — strategy logic with synthetic data.

No API calls, no DB connections. Uses a fake EdgeLookup that returns
configured values.
"""

from datetime import datetime, timezone, timedelta

from flb_strategy import FLBStrategy
from strategy import TradingParams


# ─── Fake EdgeLookup ──────────────────────────────────────────────

class FakeEdgeLookup:
    """Minimal EdgeLookup for testing. Returns configured edge/classification."""

    def __init__(self, edges=None, classifications=None):
        # edges: {series: edge_value} — returns same edge for all hours
        self._edges = edges or {}
        # classifications: {series: (gp, topic)}
        self._classifications = classifications or {}

    def get_edge(self, series: str, hours_to_settlement: float,
                 observed_price_cents=None, side=None):
        return self._edges.get(series)

    def get_classification(self, series: str):
        return self._classifications.get(series)


# ─── Synthetic event/market builders ──────────────────────────────

def _make_market(ticker, yes_bid=0.89, yes_ask=0.92, status="active",
                 close_time=None):
    """Build a market dict matching Kalshi API format."""
    if close_time is None:
        close_time = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    return {
        "ticker": ticker,
        "status": status,
        "yes_bid_dollars": str(yes_bid),
        "yes_ask_dollars": str(yes_ask),
        "expected_expiration_time": close_time,
        "title": f"Market {ticker}",
    }


def _make_event(event_ticker, markets, market_structure=None):
    """Build an event dict matching Kalshi API format."""
    e = {
        "event_ticker": event_ticker,
        "markets": markets,
    }
    if market_structure is not None:
        e["market_structure"] = market_structure
    return e


def _make_chain_event(series="KXBTCD", event_suffix="26MAR2717",
                      strikes=None, yes_bid=0.89, yes_ask=0.92,
                      market_structure="monotone_threshold"):
    """Build a chain event with numeric strike tickers."""
    if strikes is None:
        strikes = [60000, 62000, 64000, 66000]
    event_ticker = f"{series}-{event_suffix}"
    markets = []
    for s in strikes:
        ticker = f"{series}-{event_suffix}-T{s}"
        markets.append(_make_market(ticker, yes_bid=yes_bid, yes_ask=yes_ask))
    return _make_event(event_ticker, markets, market_structure=market_structure)


def _default_lookup(series="KXBTCD", edge=0.025, gp="continuous_underlyer",
                    topic="financial"):
    return FakeEdgeLookup(
        edges={series: edge},
        classifications={series: (gp, topic)},
    )


# ─── Tests ────────────────────────────────────────────────────────

class TestFLBStrategyScan:
    def test_basic_chain_produces_pairs(self):
        """A chain event with YES and NO tails produces a paired trade."""
        # Strike at 60000: YES bid=89, ask=92 → YES mid=90 (in tail)
        # Strike at 66000: YES bid=3, ask=8 → NO mid=94 (in tail)
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T64000", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXBTCD-26MAR2717-T66000", yes_bid=0.03, yes_ask=0.08),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1
        assert pairs[0].is_chain is True
        assert pairs[0].yes_opp.side == 'yes'
        assert pairs[0].no_opp.side == 'no'

    def test_blocked_series_skipped(self):
        event = _make_chain_event(series="KXPGATOP5")
        lookup = FakeEdgeLookup(
            edges={"KXPGATOP5": 0.05},
            classifications={"KXPGATOP5": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_unclassified_series_skipped(self):
        event = _make_chain_event(series="KXUNKNOWN")
        lookup = FakeEdgeLookup(edges={"KXUNKNOWN": 0.03})  # no classification
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_no_edge_skipped(self):
        event = _make_chain_event()
        lookup = FakeEdgeLookup(
            edges={},  # no edge data
            classifications={"KXBTCD": ("continuous_underlyer", "financial")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_edge_below_min_after_fees_skipped(self):
        """Edge that's positive but below min_edge after fee deduction should be skipped."""
        event = _make_chain_event()
        lookup = FakeEdgeLookup(
            edges={"KXBTCD": 0.003},  # 0.3% raw edge, below 0.5% after fees
            classifications={"KXBTCD": ("continuous_underlyer", "financial")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_zero_edge_skipped(self):
        event = _make_chain_event()
        lookup = FakeEdgeLookup(
            edges={"KXBTCD": 0.0},
            classifications={"KXBTCD": ("continuous_underlyer", "financial")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_max_days_to_settle_enforced(self):
        far_future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92,
                         close_time=far_future),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_expired_market_skipped(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92,
                         close_time=past),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_non_chain_event_not_paired(self):
        """Binary events (name-based tickers) don't produce pairs."""
        event = _make_event("KXNBA-26MAR23", [
            _make_market("KXNBA-26MAR23-LAKERS", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXNBA-26MAR23-CELTICS", yes_bid=0.06, yes_ask=0.11),
        ])
        lookup = FakeEdgeLookup(
            edges={"KXNBA": 0.03},
            classifications={"KXNBA": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_non_chain_allowed_when_unpaired(self):
        """With unpaired=True, single-leg opportunities from any event are returned."""
        event = _make_event("KXNBA-26MAR23", [
            _make_market("KXNBA-26MAR23-LAKERS", yes_bid=0.89, yes_ask=0.92),
        ])
        lookup = FakeEdgeLookup(
            edges={"KXNBA": 0.03},
            classifications={"KXNBA": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event], unpaired=True)
        assert len(pairs) >= 1

    def test_non_chain_both_sides_unpaired(self):
        """Non-chain event with both YES and NO tails should produce unpaired trades."""
        event = _make_event("KXNBA-26MAR23", [
            _make_market("KXNBA-26MAR23-LAKERS", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXNBA-26MAR23-CELTICS", yes_bid=0.04, yes_ask=0.09),
        ])
        lookup = FakeEdgeLookup(
            edges={"KXNBA": 0.03},
            classifications={"KXNBA": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        # Without unpaired, should produce nothing (not a chain)
        pairs = strategy.scan([event])
        assert len(pairs) == 0
        # With unpaired, should produce at least one single-leg trade
        pairs = strategy.scan([event], unpaired=True)
        assert len(pairs) >= 1

    def test_market_structure_overrides_ticker_regex(self):
        """Numeric tickers that look like a chain but market_structure says
        exhaustive_partition → should NOT be paired.

        This is the sports totals bug: KXNBATOTAL-26MAR22-1, -2, -3 have
        numeric suffixes but are an exhaustive partition, not a monotone chain.
        """
        event = _make_event("KXNBATOTAL-26MAR22", [
            _make_market("KXNBATOTAL-26MAR22-200", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXNBATOTAL-26MAR22-210", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXNBATOTAL-26MAR22-220", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXNBATOTAL-26MAR22-230", yes_bid=0.03, yes_ask=0.08),
        ], market_structure="exhaustive_partition")
        lookup = FakeEdgeLookup(
            edges={"KXNBATOTAL": 0.03},
            classifications={"KXNBATOTAL": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0  # NOT paired despite numeric tickers

    def test_market_structure_monotone_enables_pairing(self):
        """Explicit monotone_threshold enables pairing even without regex fallback."""
        event = _make_event("KXTEST-26MAR22", [
            _make_market("KXTEST-26MAR22-A", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXTEST-26MAR22-B", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXTEST-26MAR22-C", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXTEST-26MAR22-D", yes_bid=0.03, yes_ask=0.08),
        ], market_structure="monotone_threshold")
        lookup = FakeEdgeLookup(
            edges={"KXTEST": 0.03},
            classifications={"KXTEST": ("continuous_underlyer", "financial")},
        )
        strategy = FLBStrategy(lookup)
        # Name-based tickers would fail detect_chain, but market_structure overrides
        pairs = strategy.scan([event])
        assert len(pairs) == 1
        assert pairs[0].is_chain is True

    def test_convergent_binary_never_paired_even_with_numeric_tickers(self):
        """convergent_binary events with numeric tickers must not be paired.

        Sports totals (KXNBATOTAL-200, -210, -220) have numeric suffixes
        that detect_chain would match, but they're not monotone safe zones.
        """
        event = _make_chain_event(
            series="KXNBATOTAL", event_suffix="26MAR22",
            strikes=[200, 210, 220, 230],
            market_structure=None,  # no event-level data, would fall back to regex
        )
        lookup = FakeEdgeLookup(
            edges={"KXNBATOTAL": 0.03},
            classifications={"KXNBATOTAL": ("convergent_binary", "entertainment_sports")},
        )
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0  # generating_process filter blocks pairing

    def test_continuous_underlyer_can_be_paired(self):
        """continuous_underlyer with numeric tickers should pair normally."""
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T64000", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXBTCD-26MAR2717-T66000", yes_bid=0.03, yes_ask=0.08),
        ], market_structure="monotone_threshold")
        lookup = _default_lookup(gp="continuous_underlyer")
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1
        assert pairs[0].is_chain is True

    def test_scheduled_release_can_be_paired(self):
        """scheduled_release with monotone_threshold should pair."""
        event = _make_event("KXCPI-26MAR26", [
            _make_market("KXCPI-26MAR26-T30", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXCPI-26MAR26-T35", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXCPI-26MAR26-T40", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXCPI-26MAR26-T45", yes_bid=0.03, yes_ask=0.08),
        ], market_structure="monotone_threshold")
        lookup = _default_lookup(series="KXCPI", gp="scheduled_release",
                                  topic="economic_data")
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1

    def test_no_market_structure_falls_back_to_regex(self):
        """Without market_structure field, detect_chain regex is used."""
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T64000", yes_bid=0.20, yes_ask=0.25),
            _make_market("KXBTCD-26MAR2717-T66000", yes_bid=0.03, yes_ask=0.08),
        ])  # no market_structure
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1  # regex detects numeric tickers

    def test_traded_tickers_skipped(self):
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event], traded_tickers={"KXBTCD-26MAR2717-T60000"})
        assert len(pairs) == 0

    def test_inactive_market_skipped(self):
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92,
                         status="closed"),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_spread_too_wide_skipped(self):
        # Spread = 20¢ > MAX_SPREAD (10¢)
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.80, yes_ask=1.00),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.50, yes_ask=0.55),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_midrange_price_not_tail(self):
        # YES mid = 50¢, not in 85-97 tail zone
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.48, yes_ask=0.52),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.48, yes_ask=0.52),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 0

    def test_max_qualifying_events(self):
        """Only top N events by edge/day are returned."""
        events = []
        lookup_edges = {}
        lookup_classes = {}
        for i in range(30):
            series = f"KXTEST{i}"
            events.append(_make_event(f"{series}-EVT", [
                _make_market(f"{series}-EVT-T100", yes_bid=0.89, yes_ask=0.92),
                _make_market(f"{series}-EVT-T200", yes_bid=0.03, yes_ask=0.08),
            ]))
            lookup_edges[series] = 0.03 - i * 0.0005
            lookup_classes[series] = ("continuous_underlyer", "financial")

        lookup = FakeEdgeLookup(edges=lookup_edges, classifications=lookup_classes)
        params = TradingParams(max_qualifying_events=5)
        strategy = FLBStrategy(lookup, params)
        pairs = strategy.scan(events)
        event_count = len({p.event_ticker for p in pairs})
        assert event_count == 5

    def test_inverted_strikes_not_paired(self):
        """If YES tail is at a higher strike than NO tail, no safe zone exists."""
        # YES at T70000 (high strike, YES=90¢) + NO at T60000 (low strike, YES=5¢ → NO=95¢)
        # This is inverted: YES strike > NO strike → no safe zone
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T70000", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXBTCD-26MAR2717-T65000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.03, yes_ask=0.08),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        # Should not produce a paired trade (inverted strikes)
        assert all(not p.is_chain for p in pairs)

    def test_correct_strike_ordering_paired(self):
        """YES at lower strike + NO at higher strike = valid safe zone."""
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.89, yes_ask=0.92),
            _make_market("KXBTCD-26MAR2717-T65000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T70000", yes_bid=0.03, yes_ask=0.08),
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1
        assert pairs[0].is_chain

    def test_selects_best_pair_by_edge_not_spread(self):
        """When multiple valid pairs exist, pick the one with highest combined edge."""
        # Give different edges to different series to force different net edges.
        # Use two separate events since all markets in one event share a series.
        # Instead: use markets at very different prices so net edge differs.
        # YES at mid=86 (lower net edge, more fee impact) vs mid=96 (higher)
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.85, yes_ask=0.88),  # YES mid=86, wider spread
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.95, yes_ask=0.97),  # YES mid=96, tight spread
            _make_market("KXBTCD-26MAR2717-T65000", yes_bid=0.50, yes_ask=0.55),
            _make_market("KXBTCD-26MAR2717-T70000", yes_bid=0.03, yes_ask=0.08),  # NO mid=94
        ])
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        pairs = strategy.scan([event])
        assert len(pairs) == 1
        p = pairs[0]
        # T62000 (YES mid=96) should be selected: lower fees at 96¢ → higher net edge
        assert "T62000" in p.yes_opp.ticker

    def test_custom_params(self):
        """Custom TradingParams override defaults."""
        event = _make_event("KXBTCD-26MAR2717", [
            _make_market("KXBTCD-26MAR2717-T60000", yes_bid=0.80, yes_ask=0.82),
            _make_market("KXBTCD-26MAR2717-T62000", yes_bid=0.05, yes_ask=0.08),
        ])
        lookup = _default_lookup()
        # Default params: min_tail=85, so YES mid=81 wouldn't qualify
        strategy_default = FLBStrategy(lookup)
        assert len(strategy_default.scan([event])) == 0

        # Custom params: min_tail=80, so YES mid=81 qualifies
        params = TradingParams(min_tail=80, max_tail=99)
        strategy_custom = FLBStrategy(lookup, params)
        assert len(strategy_custom.scan([event])) >= 1


class TestFLBStrategySizeOrder:
    def test_size_order(self):
        from conftest import make_opportunity
        lookup = _default_lookup()
        strategy = FLBStrategy(lookup)
        opp = make_opportunity(bid_price=90)
        q = strategy.size_order(opp)
        assert 1 <= q <= 8


class TestFLBStrategyHelpers:
    def test_parse_days_to_settle_iso(self):
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = "2026-03-24T12:00:00Z"
        days = FLBStrategy._parse_days_to_settle(future, now)
        assert abs(days - 1.0) < 0.01

    def test_parse_days_to_settle_none(self):
        now = datetime.now(timezone.utc)
        assert FLBStrategy._parse_days_to_settle(None, now) is None
        assert FLBStrategy._parse_days_to_settle("", now) is None

    def test_parse_prices(self):
        market = {"yes_bid_dollars": "0.89", "yes_ask_dollars": "0.92"}
        bid, ask = FLBStrategy._parse_prices(market)
        assert bid == 89
        assert ask == 92

    def test_parse_prices_invalid(self):
        market = {"yes_bid_dollars": "bad", "yes_ask_dollars": "0.92"}
        bid, ask = FLBStrategy._parse_prices(market)
        assert bid is None

    def test_parse_prices_zero(self):
        market = {"yes_bid_dollars": "0", "yes_ask_dollars": "0.92"}
        bid, ask = FLBStrategy._parse_prices(market)
        assert bid is None
