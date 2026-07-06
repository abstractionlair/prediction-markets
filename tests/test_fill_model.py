"""Tests for trading/fill_model.py — limit order fill simulation."""

from datetime import datetime, timezone, timedelta

from fill_model import (
    FillModel, FillResult, Fill, CandleData, TimestampedCandle,
    DEFAULT_FILL_MODEL,
)


def _candle(bid_high=90, ask_low=88, volume=100, price_high=0, price_low=0):
    return CandleData(yes_bid_high=bid_high, yes_ask_low=ask_low, volume=volume,
                      price_high=price_high, price_low=price_low)


def _ts_candle(hours_offset=0, bid_high=90, ask_low=88, volume=100,
               price_high=0, price_low=0):
    t = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hours_offset)
    return TimestampedCandle(period_end=t, candle=_candle(bid_high, ask_low, volume,
                                                          price_high, price_low))


# ─── check_fill: per-period interface ─────────────────────────────

class TestCheckFill:
    def test_yes_fill(self):
        fm = FillModel(capture_rate=0.20)
        # Ask low = 88 <= bid 90 → touched, 100 * 0.20 = 20 available
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=88, volume=100)) == 8

    def test_yes_not_touched(self):
        fm = FillModel(capture_rate=0.20)
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=92, volume=1000)) == 0

    def test_yes_exact_touch(self):
        fm = FillModel(capture_rate=0.20)
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=90, volume=100)) == 8

    def test_no_fill(self):
        fm = FillModel(capture_rate=0.20)
        # NO ask low = 100 - 95 = 5 <= bid 6 → touched
        assert fm.check_fill('no', 6, 8, _candle(bid_high=95, volume=100)) == 8

    def test_no_not_touched(self):
        fm = FillModel(capture_rate=0.20)
        assert fm.check_fill('no', 5, 8, _candle(bid_high=93, volume=100)) == 0

    def test_partial_fill_low_volume(self):
        fm = FillModel(capture_rate=0.20)
        # 10 * 0.20 = 2 available, need 8
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=88, volume=10)) == 2

    def test_no_fill_on_zero_volume(self):
        fm = FillModel(capture_rate=0.20, min_fill_per_touch=1)
        # Zero volume = no actual trading → no fill even if price touched
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=88, volume=0)) == 0

    def test_yes_fill_via_trade_price(self):
        """Ask didn't reach limit, but a trade executed at/below it."""
        fm = FillModel(capture_rate=0.20)
        # ask_low=92 (above limit), but price_low=89 (below limit)
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=92, price_low=89, volume=100)) == 8

    def test_yes_fill_via_trade_exact_touch(self):
        fm = FillModel(capture_rate=0.20)
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=92, price_low=90, volume=100)) == 8

    def test_yes_no_fill_trade_price_above(self):
        fm = FillModel(capture_rate=0.20)
        # Neither ask nor trade reached limit
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=92, price_low=91, volume=100)) == 0

    def test_yes_trade_price_zero_ignored(self):
        """price_low=0 means unavailable, should not trigger fill."""
        fm = FillModel(capture_rate=0.20)
        assert fm.check_fill('yes', 90, 8, _candle(ask_low=92, price_low=0, volume=100)) == 0

    def test_no_fill_via_trade_price(self):
        """YES bid didn't reach high enough, but trade did."""
        fm = FillModel(capture_rate=0.20)
        # NO limit = 6. no_ask_low = 100-93 = 7 (above limit).
        # But price_high=95 → 100-95 = 5 <= 6 → touched.
        assert fm.check_fill('no', 6, 8, _candle(bid_high=93, price_high=95, volume=100)) == 8

    def test_no_no_fill_trade_price_not_high_enough(self):
        fm = FillModel(capture_rate=0.20)
        # NO limit = 6. no_ask_low = 100-93 = 7. price_high=93 → 100-93=7. Neither reaches.
        assert fm.check_fill('no', 6, 8, _candle(bid_high=93, price_high=93, volume=100)) == 0

    def test_invalid_side_raises(self):
        fm = FillModel()
        import pytest
        with pytest.raises(ValueError, match="side must be"):
            fm.check_fill('maybe', 90, 8, _candle())

    def test_negative_params_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            FillModel(capture_rate=-0.5)
        with pytest.raises(ValueError):
            FillModel(min_fill_per_touch=-1)

    def test_zero_remaining(self):
        fm = FillModel()
        assert fm.check_fill('yes', 90, 0, _candle(ask_low=88, volume=100)) == 0

    def test_remaining_caps_fill(self):
        fm = FillModel(capture_rate=0.50)
        # 100 * 0.50 = 50 available, but only 3 remaining
        assert fm.check_fill('yes', 90, 3, _candle(ask_low=88, volume=100)) == 3

    def test_min_fill_caps_at_remaining(self):
        fm = FillModel(capture_rate=0.0, min_fill_per_touch=5)
        # capture_rate gives 0, min_fill = 5, but only 2 remaining
        assert fm.check_fill('yes', 90, 2, _candle(ask_low=88, volume=100)) == 2


# ─── simulate_order: batch interface ──────────────────────────────

class TestSimulateOrder:
    def test_full_fill_one_period(self):
        fm = FillModel(capture_rate=0.50)
        candles = [_ts_candle(0, ask_low=88, volume=100)]
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.fill_complete is True
        assert result.contracts_filled == 8
        assert len(result.fills) == 1
        assert result.fills[0].contracts == 8

    def test_gradual_fill(self):
        fm = FillModel(capture_rate=0.50)
        candles = [
            _ts_candle(0, ask_low=88, volume=4),  # 2 fill
            _ts_candle(1, ask_low=88, volume=4),  # 2 fill → 4
            _ts_candle(2, ask_low=88, volume=4),  # 2 fill → 6
            _ts_candle(3, ask_low=88, volume=4),  # 2 fill → 8
        ]
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.fill_complete is True
        assert result.contracts_filled == 8
        assert len(result.fills) == 4
        assert all(f.contracts == 2 for f in result.fills)

    def test_fill_with_gaps(self):
        fm = FillModel(capture_rate=0.50)
        candles = [
            _ts_candle(0, ask_low=92, volume=100),  # not touched
            _ts_candle(1, ask_low=88, volume=10),   # 5 fill
            _ts_candle(2, ask_low=92, volume=100),  # not touched
            _ts_candle(3, ask_low=88, volume=10),   # 3 fill → 8
        ]
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.fill_complete is True
        assert len(result.fills) == 2
        assert result.fills[0].contracts == 5
        assert result.fills[1].contracts == 3

    def test_never_fills(self):
        fm = FillModel(capture_rate=0.20)
        candles = [_ts_candle(i, ask_low=92, volume=100) for i in range(10)]
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.contracts_filled == 0
        assert result.fill_complete is False
        assert result.fills == ()

    def test_partial_fill_expires(self):
        fm = FillModel(capture_rate=0.20)
        candles = [_ts_candle(0, ask_low=88, volume=10)]  # only 2 fill
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.contracts_filled == 2
        assert result.fill_complete is False
        assert len(result.fills) == 1

    def test_zero_contracts(self):
        fm = FillModel()
        result = fm.simulate_order('yes', 90, 0, [_ts_candle()])
        assert result.fill_complete is True
        assert result.fills == ()

    def test_empty_candles(self):
        fm = FillModel()
        result = fm.simulate_order('yes', 90, 8, [])
        assert result.contracts_filled == 0
        assert result.fill_complete is False

    def test_timestamps_preserved(self):
        fm = FillModel(capture_rate=0.50)
        candles = [
            _ts_candle(0, ask_low=88, volume=6),  # 3 fill
            _ts_candle(1, ask_low=88, volume=20),  # 5 fill → 8
        ]
        result = fm.simulate_order('yes', 90, 8, candles)
        assert result.fills[0].time == candles[0].period_end
        assert result.fills[1].time == candles[1].period_end

    def test_large_order_many_periods(self):
        fm = FillModel(capture_rate=0.20)
        # 50 contracts, 20 vol/candle → 4/candle → 13 candles
        candles = [_ts_candle(i, ask_low=88, volume=20) for i in range(20)]
        result = fm.simulate_order('yes', 90, 50, candles)
        assert result.fill_complete is True
        assert result.contracts_filled == 50
        assert result.fill_events == 13


# ─── NO side (batch) ─────────────────────────────────────────────

class TestNoSideBatch:
    def test_no_side_full_fill(self):
        fm = FillModel(capture_rate=0.20)
        candles = [_ts_candle(0, bid_high=95, volume=100)]
        result = fm.simulate_order('no', 6, 8, candles)
        assert result.fill_complete is True
        assert result.contracts_filled == 8

    def test_no_side_not_touched(self):
        fm = FillModel(capture_rate=0.20)
        candles = [_ts_candle(0, bid_high=93, volume=100)]
        result = fm.simulate_order('no', 6, 8, candles)
        assert result.contracts_filled == 0


# ─── FillResult properties ────────────────────────────────────────

class TestFillResult:
    def test_fill_rate(self):
        r = FillResult(contracts_requested=10, contracts_filled=7,
                       fills=(), fill_complete=False)
        assert abs(r.fill_rate - 0.7) < 1e-10

    def test_fill_rate_zero_requested(self):
        r = FillResult(contracts_requested=0, contracts_filled=0,
                       fills=(), fill_complete=True)
        assert r.fill_rate == 0.0

    def test_fill_events(self):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        fills = [Fill(time=t, contracts=3), Fill(time=t, contracts=5)]
        r = FillResult(contracts_requested=8, contracts_filled=8,
                       fills=fills, fill_complete=True)
        assert r.fill_events == 2

    def test_fill_events_empty(self):
        r = FillResult(contracts_requested=8, contracts_filled=0,
                       fills=(), fill_complete=False)
        assert r.fill_events == 0


# ─── Capture rate sensitivity ────────────────────────────────────

class TestCaptureRateSensitivity:
    def test_higher_rate_faster_fill(self):
        candles = [_ts_candle(i, ask_low=88, volume=20) for i in range(20)]
        slow = FillModel(capture_rate=0.10).simulate_order('yes', 90, 20, candles)
        fast = FillModel(capture_rate=0.50).simulate_order('yes', 90, 20, candles)
        assert fast.fill_events < slow.fill_events
        assert fast.fill_complete and slow.fill_complete

    def test_zero_capture_rate_uses_min_fill(self):
        fm = FillModel(capture_rate=0.0, min_fill_per_touch=1)
        # capture_rate=0 → int(volume * 0) = 0, but min_fill_per_touch=1
        # so each touched candle with volume > 0 fills 1 contract
        candles = [_ts_candle(i, ask_low=88, volume=10) for i in range(10)]
        result = fm.simulate_order('yes', 90, 5, candles)
        assert result.contracts_filled == 5
        assert result.fill_events == 5


# ─── Interface agreement ─────────────────────────────────────────

class TestInterfaceAgreement:
    """simulate_order must produce identical results to calling check_fill
    incrementally on the same candle sequence."""

    def test_full_fill_agrees(self):
        fm = FillModel(capture_rate=0.30)
        candles = [
            _ts_candle(0, ask_low=88, volume=10),
            _ts_candle(1, ask_low=92, volume=50),
            _ts_candle(2, ask_low=87, volume=20),
            _ts_candle(3, ask_low=88, volume=15),
        ]

        # Batch
        batch = fm.simulate_order('yes', 90, 8, candles)

        # Incremental
        remaining = 8
        incremental_total = 0
        for tc in candles:
            filled = fm.check_fill('yes', 90, remaining, tc.candle)
            incremental_total += filled
            remaining -= filled
            if remaining <= 0:
                break

        assert batch.contracts_filled == incremental_total

    def test_partial_fill_agrees(self):
        fm = FillModel(capture_rate=0.10)
        candles = [
            _ts_candle(0, ask_low=88, volume=5),
            _ts_candle(1, ask_low=89, volume=3),
        ]

        batch = fm.simulate_order('yes', 90, 20, candles)

        remaining = 20
        incremental_total = 0
        for tc in candles:
            filled = fm.check_fill('yes', 90, remaining, tc.candle)
            incremental_total += filled
            remaining -= filled

        assert batch.contracts_filled == incremental_total

    def test_no_fill_agrees(self):
        fm = FillModel(capture_rate=0.20)
        candles = [_ts_candle(i, ask_low=95, volume=100) for i in range(5)]

        batch = fm.simulate_order('yes', 90, 8, candles)

        remaining = 8
        incremental_total = 0
        for tc in candles:
            filled = fm.check_fill('yes', 90, remaining, tc.candle)
            incremental_total += filled
            remaining -= filled

        assert batch.contracts_filled == incremental_total == 0

    def test_no_side_agrees(self):
        fm = FillModel(capture_rate=0.25)
        candles = [
            _ts_candle(0, bid_high=94, volume=30),
            _ts_candle(1, bid_high=96, volume=20),
            _ts_candle(2, bid_high=93, volume=40),
        ]

        batch = fm.simulate_order('no', 7, 10, candles)

        remaining = 10
        incremental_total = 0
        for tc in candles:
            filled = fm.check_fill('no', 7, remaining, tc.candle)
            incremental_total += filled
            remaining -= filled
            if remaining <= 0:
                break

        assert batch.contracts_filled == incremental_total


# ─── Default model ───────────────────────────────────────────────

class TestDefaultModel:
    def test_default_capture_rate(self):
        assert DEFAULT_FILL_MODEL.capture_rate == 0.20

    def test_default_works(self):
        result = DEFAULT_FILL_MODEL.check_fill('yes', 90, 1,
                                               _candle(ask_low=88, volume=10))
        assert result >= 1
