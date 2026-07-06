"""Sanity tests for the fill model against real market data.

These tests verify the fill model produces sensible results — not just
that the code runs, but that the outputs match intuition about how
limit orders behave in real markets.

Uses real hourly candle data from the database.
Requires DB access — skip if unavailable.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'collectors'))

from fill_model import FillModel, CandleData, TimestampedCandle

# ─── Database fixture ─────────────────────────────────────────────

def _get_conn():
    try:
        import psycopg2
        dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
        if not dsn:
            return None
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute("SET search_path TO prediction_markets, public")
        conn.commit()
        return conn
    except Exception:
        return None


def _load_candles(conn, ticker):
    """Load all hourly candles for a ticker as TimestampedCandle list."""
    cur = conn.cursor()
    cur.execute("""
        SELECT period_end,
               (yes_bid_high * 100)::int, (yes_ask_low * 100)::int,
               COALESCE(volume, 0)
        FROM kalshi_hourly_candles
        WHERE ticker = %s AND yes_bid_high > 0 AND yes_ask_low > 0
        ORDER BY period_end
    """, (ticker,))
    return [
        TimestampedCandle(
            period_end=pe,
            candle=CandleData(yes_bid_high=bh, yes_ask_low=al, volume=vol),
        )
        for pe, bh, al, vol in cur
    ]


def _load_tail_candles(conn, n=200):
    """Load a sample of candles in the tail zone (mid 85-97c) with volume."""
    cur = conn.cursor()
    cur.execute("""
        SELECT period_end, ticker,
               (yes_bid_high * 100)::int, (yes_ask_low * 100)::int,
               (yes_bid_close * 100)::int, (yes_ask_close * 100)::int,
               COALESCE(volume, 0)
        FROM kalshi_hourly_candles
        WHERE yes_bid_close > 0 AND yes_ask_close > 0
          AND (yes_bid_close + yes_ask_close) / 2 BETWEEN 0.85 AND 0.97
          AND volume > 0
        ORDER BY RANDOM()
        LIMIT %s
    """, (n,))
    return cur.fetchall()


# Skip all tests if no DB
conn = _get_conn()
if conn is None:
    pytestmark = pytest.mark.skip("No database connection")
else:
    pytestmark = pytest.mark.db


# ─── Sanity: extreme prices ──────────────────────────────────────

class TestExtremePrices:
    """Orders at extreme prices should have obvious fill behavior."""

    def test_buy_yes_at_100_always_fills(self):
        """Offering $1.00 for YES — maximum possible price. Must always fill."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('yes', 100, 1, candles)
        # At 100c, yes_ask_low is always <= 100 (asks can't exceed $1)
        assert result.fill_complete, "Bid at $1.00 should always fill"

    def test_buy_yes_at_1_rarely_fills(self):
        """Offering 1c for YES in a 90c market — should almost never fill."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('yes', 1, 1, candles)
        assert result.contracts_filled == 0, "Bid at 1c in a 90c market should never fill"

    def test_buy_no_at_100_always_fills(self):
        """Offering $1.00 for NO — must always fill."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('no', 100, 1, candles)
        # NO ask low = 100 - yes_bid_high. For bid_high to make NO ask > 100,
        # bid_high would need to be < 0, which is impossible.
        assert result.fill_complete, "Bid at $1.00 for NO should always fill"

    def test_million_contracts_at_zero_never_fills(self):
        """Offering 0c for 1M contracts — obviously never fills."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=1.0)
        result = fm.simulate_order('yes', 0, 1000000, candles)
        assert result.contracts_filled == 0


# ─── Sanity: price levels ────────────────────────────────────────

class TestPriceLevels:
    """Fill rates should be monotonically increasing with bid price."""

    def test_higher_bid_fills_more(self):
        """Bidding higher should fill at least as much as bidding lower."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        fills_at_80 = fm.simulate_order('yes', 80, 8, candles).contracts_filled
        fills_at_85 = fm.simulate_order('yes', 85, 8, candles).contracts_filled
        fills_at_90 = fm.simulate_order('yes', 90, 8, candles).contracts_filled
        fills_at_95 = fm.simulate_order('yes', 95, 8, candles).contracts_filled

        assert fills_at_80 <= fills_at_85 <= fills_at_90 <= fills_at_95

    def test_bid_at_ask_fills_quickly(self):
        """Bidding at the ask (crossing the spread) should fill immediately."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        # Bid at 99c when market is around 88c — well above any ask
        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('yes', 99, 1, candles)
        assert result.fill_complete
        assert result.fill_events == 1, "Bid well above market should fill in first period"


# ─── Sanity: order size ──────────────────────────────────────────

class TestOrderSize:
    """Larger orders should take longer to fill."""

    def test_large_order_fills_slower(self):
        """50 contracts should take more periods than 1 contract."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        small = fm.simulate_order('yes', 90, 1, candles)
        large = fm.simulate_order('yes', 90, 50, candles)

        if small.fill_complete and large.fill_complete:
            assert large.fill_events >= small.fill_events
        elif small.fill_complete:
            # Large didn't complete but small did — consistent
            pass
        # If neither filled, both got 0 periods — fine

    def test_tiny_order_fills_fast(self):
        """1 contract at mid should fill quickly in a liquid market."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('yes', 90, 1, candles)
        assert result.fill_complete, "1 contract at mid in a liquid market should fill"

    def test_huge_order_incomplete(self):
        """1M contracts should not fully fill even in a liquid market."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        fm = FillModel(capture_rate=0.20)
        result = fm.simulate_order('yes', 90, 1000000, candles)
        assert not result.fill_complete, "1M contracts should not fully fill"
        # This market is extremely liquid (~133K vol/hr near settlement)
        # so partial fill can be substantial, but should not be 100%
        assert result.fill_rate < 1.0


# ─── Sanity: aggregate statistics ────────────────────────────────

class TestAggregateStats:
    """Fill rates across many markets should match intuition."""

    def test_mid_fills_more_than_bid(self):
        """Orders at the mid should fill more often than at the bid."""
        if conn is None:
            pytest.skip("No DB")
        rows = _load_tail_candles(conn, n=200)
        if len(rows) < 50:
            pytest.skip("Insufficient data")

        fm = FillModel(capture_rate=0.20)
        mid_fills = 0
        bid_fills = 0

        for pe, ticker, bh, al, bc, ac, vol in rows:
            candle = CandleData(yes_bid_high=bh, yes_ask_low=al, volume=vol)
            mid = (bc + ac) // 2
            bid = bc  # close bid — conservative

            mid_fill = fm.check_fill('yes', mid, 1, candle)
            bid_fill = fm.check_fill('yes', bid, 1, candle)
            mid_fills += min(mid_fill, 1)
            bid_fills += min(bid_fill, 1)

        assert mid_fills >= bid_fills, (
            f"Mid fills ({mid_fills}) should be >= bid fills ({bid_fills})")

    def test_ask_fills_more_than_mid(self):
        """Orders at the ask should fill more often than at the mid."""
        if conn is None:
            pytest.skip("No DB")
        rows = _load_tail_candles(conn, n=200)
        if len(rows) < 50:
            pytest.skip("Insufficient data")

        fm = FillModel(capture_rate=0.20)
        mid_fills = 0
        ask_fills = 0

        for pe, ticker, bh, al, bc, ac, vol in rows:
            candle = CandleData(yes_bid_high=bh, yes_ask_low=al, volume=vol)
            mid = (bc + ac) // 2
            ask = ac

            mid_fill = fm.check_fill('yes', mid, 1, candle)
            ask_fill = fm.check_fill('yes', ask, 1, candle)
            mid_fills += min(mid_fill, 1)
            ask_fills += min(ask_fill, 1)

        assert ask_fills >= mid_fills, (
            f"Ask fills ({ask_fills}) should be >= mid fills ({mid_fills})")

    def test_fill_rate_in_reasonable_range(self):
        """For 8-contract orders at mid, fill rate should be between 5% and 80%."""
        if conn is None:
            pytest.skip("No DB")
        rows = _load_tail_candles(conn, n=500)
        if len(rows) < 100:
            pytest.skip("Insufficient data")

        fm = FillModel(capture_rate=0.20)
        n_tested = 0
        n_filled = 0

        for pe, ticker, bh, al, bc, ac, vol in rows:
            candle = CandleData(yes_bid_high=bh, yes_ask_low=al, volume=vol)
            mid = (bc + ac) // 2
            fill = fm.check_fill('yes', mid, 8, candle)
            n_tested += 1
            if fill > 0:
                n_filled += 1

        rate = n_filled / n_tested
        assert 0.05 < rate < 0.80, (
            f"Fill rate {rate:.1%} outside reasonable range 5-80%")


# ─── Sanity: capture rate ────────────────────────────────────────

class TestCaptureRateRealism:
    """The capture rate should produce plausible fill volumes."""

    def test_capture_rate_scales_linearly(self):
        """Double the capture rate ≈ double the fills (order large enough to not fully fill)."""
        if conn is None:
            pytest.skip("No DB")
        candles = _load_candles(conn, 'KXNCAAMBGAME-26MAR13OSUMICH-MICH')
        if not candles:
            pytest.skip("No candle data")

        # Order must be large enough that neither rate fully fills it,
        # so we see the rate difference. This market has ~3.5M total volume.
        fm_10 = FillModel(capture_rate=0.10)
        fm_20 = FillModel(capture_rate=0.20)
        r10 = fm_10.simulate_order('yes', 90, 10000000, candles)
        r20 = fm_20.simulate_order('yes', 90, 10000000, candles)

        # Should be roughly 2x, but allow for min_fill_per_touch noise
        if r10.contracts_filled > 100:
            ratio = r20.contracts_filled / r10.contracts_filled
            assert 1.5 < ratio < 2.5, (
                f"Expected ~2x fills at 2x capture rate, got {ratio:.1f}x")
