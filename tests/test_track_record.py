"""Tests for trading/track_record.py."""

import json
import tempfile
from pathlib import Path

from track_record import TradeRecord, TrackRecord


def _make_trade(entry_price=90, exit_price=100, contracts=5, fee_cents=1,
                days_held=1.0, edge_estimate=0.025, side='yes',
                ticker='KXBTCD-T60399', series='KXBTCD',
                generating_process='continuous_underlyer'):
    return TradeRecord(
        ticker=ticker, side=side, entry_price=entry_price,
        contracts=contracts, exit_price=exit_price, fee_cents=fee_cents,
        days_held=days_held, edge_estimate=edge_estimate,
        series=series, generating_process=generating_process,
    )


# ─── TradeRecord ──────────────────────────────────────────────────

class TestTradeRecord:
    def test_winning_trade(self):
        t = _make_trade(entry_price=90, exit_price=100, contracts=5, fee_cents=1)
        assert t.won is True
        # pnl = (100-90)*5 - 1 = 49
        assert t.pnl_cents == 49

    def test_losing_trade(self):
        t = _make_trade(entry_price=90, exit_price=0, contracts=5, fee_cents=1)
        assert t.won is False
        # pnl = (0-90)*5 - 1 = -451
        assert t.pnl_cents == -451

    def test_breakeven_with_fee_is_loss(self):
        # MiniMax finding: exit == entry but fee makes it a loss
        t = _make_trade(entry_price=50, exit_price=50, contracts=1, fee_cents=1)
        assert t.won is False
        assert t.pnl_cents == -1

    def test_return_on_capital(self):
        t = _make_trade(entry_price=90, exit_price=100, contracts=1, fee_cents=1)
        # pnl = 10 - 1 = 9, capital = 90
        assert abs(t.return_on_capital - 9/90) < 1e-10

    def test_capital_days(self):
        t = _make_trade(entry_price=90, contracts=5, days_held=3.0)
        assert t.capital_days == 90 * 5 * 3.0

    def test_capital_days_floor(self):
        t = _make_trade(entry_price=90, contracts=1, days_held=0.1)
        # Floor is 1 day
        assert t.capital_days == 90 * 1 * 1

    def test_zero_entry_price(self):
        t = _make_trade(entry_price=0, exit_price=0, contracts=1, fee_cents=0)
        assert t.return_on_capital == 0.0


# ─── TrackRecord summary ──────────────────────────────────────────

class TestTrackRecordSummary:
    def test_empty(self):
        tr = TrackRecord()
        s = tr.summary()
        assert s['n'] == 0

    def test_single_win(self):
        tr = TrackRecord([_make_trade(entry_price=90, exit_price=100, fee_cents=1)])
        s = tr.summary()
        assert s['n'] == 1
        assert s['wins'] == 1
        assert s['win_rate'] == 1.0
        assert s['total_pnl_cents'] == 49  # (100-90)*5 - 1

    def test_single_loss(self):
        tr = TrackRecord([_make_trade(entry_price=90, exit_price=0, fee_cents=1)])
        s = tr.summary()
        assert s['wins'] == 0
        assert s['win_rate'] == 0.0
        assert s['total_pnl_cents'] == -451

    def test_mixed_trades(self):
        trades = [
            _make_trade(entry_price=90, exit_price=100, fee_cents=1),  # +49
            _make_trade(entry_price=90, exit_price=100, fee_cents=1),  # +49
            _make_trade(entry_price=90, exit_price=0, fee_cents=1),    # -451
        ]
        tr = TrackRecord(trades)
        s = tr.summary()
        assert s['n'] == 3
        assert s['wins'] == 2
        assert abs(s['win_rate'] - 2/3) < 1e-10
        assert s['total_pnl_cents'] == 49 + 49 - 451

    def test_sharpe_positive(self):
        # All wins with consistent returns → high Sharpe
        trades = [_make_trade(entry_price=90, exit_price=100, fee_cents=1) for _ in range(10)]
        tr = TrackRecord(trades)
        s = tr.summary()
        # All identical returns → std = 0 → Sharpe = 0 (degenerate)
        assert s['sharpe'] == 0.0

    def test_sharpe_with_variance(self):
        trades = [
            _make_trade(entry_price=90, exit_price=100, fee_cents=1),
            _make_trade(entry_price=95, exit_price=100, fee_cents=1),
            _make_trade(entry_price=85, exit_price=0, fee_cents=1),
        ]
        tr = TrackRecord(trades)
        s = tr.summary()
        # Should be a real number (not NaN or inf)
        assert isinstance(s['sharpe'], float)
        assert not (s['sharpe'] != s['sharpe'])  # not NaN


# ─── TrackRecord grouping ─────────────────────────────────────────

class TestTrackRecordGrouping:
    def test_by_series(self):
        trades = [
            _make_trade(series='KXBTCD'),
            _make_trade(series='KXBTCD'),
            _make_trade(series='KXETHD'),
        ]
        tr = TrackRecord(trades)
        groups = tr.by_series()
        assert len(groups) == 2
        assert len(groups['KXBTCD']) == 2
        assert len(groups['KXETHD']) == 1

    def test_by_category(self):
        trades = [
            _make_trade(generating_process='continuous_underlyer'),
            _make_trade(generating_process='convergent_binary'),
        ]
        tr = TrackRecord(trades)
        groups = tr.by_category()
        assert len(groups) == 2


# ─── TrackRecord drawdown ─────────────────────────────────────────

class TestTrackRecordDrawdown:
    def test_no_drawdown_all_wins(self):
        trades = [_make_trade(exit_price=100) for _ in range(5)]
        tr = TrackRecord(trades)
        assert tr.max_drawdown_cents() == 0

    def test_drawdown_after_loss(self):
        trades = [
            _make_trade(exit_price=100, fee_cents=1),   # +49
            _make_trade(exit_price=0, fee_cents=1),      # -451
        ]
        tr = TrackRecord(trades)
        dd = tr.max_drawdown_cents()
        # After trade 1: cum = 49, hwm = 49
        # After trade 2: cum = 49 - 451 = -402, hwm = 49
        # dd = 49 - (-402) = 451 cents
        assert dd == 451

    def test_drawdown_starting_with_loss(self):
        # Gemini/GPT finding: hwm starts at 0, losing first should still track
        trades = [
            _make_trade(exit_price=0, fee_cents=1),      # -451
            _make_trade(exit_price=100, fee_cents=1),     # +49
        ]
        tr = TrackRecord(trades)
        dd = tr.max_drawdown_cents()
        # After trade 1: cum = -451, hwm = 0, dd = 0 - (-451) = 451
        # After trade 2: cum = -402, hwm = 0, dd = 0 - (-402) = 402
        assert dd == 451

    def test_empty_drawdown(self):
        tr = TrackRecord()
        assert tr.max_drawdown_cents() == 0


# ─── TrackRecord calibration check ────────────────────────────────

class TestTrackRecordCalibration:
    def test_edge_vs_realized(self):
        trades = [
            _make_trade(entry_price=90, exit_price=100, edge_estimate=0.03),
            _make_trade(entry_price=90, exit_price=100, edge_estimate=0.03),
            _make_trade(entry_price=90, exit_price=0, edge_estimate=0.03),
        ]
        tr = TrackRecord(trades)
        cal = tr.edge_vs_realized()
        assert cal['n'] == 3
        assert cal['avg_predicted_edge'] == 0.03

    def test_no_edge_data(self):
        trades = [_make_trade(edge_estimate=0.0)]
        tr = TrackRecord(trades)
        cal = tr.edge_vs_realized()
        assert cal['n'] == 0


# ─── TrackRecord I/O ──────────────────────────────────────────────

class TestTrackRecordIO:
    def test_from_jsonl(self):
        records = [
            {"action": "order_placed", "ticker": "T1", "side": "yes", "price": 90},
            {"action": "settlement", "ticker": "T1-T100", "side": "yes",
             "entry_price": 90, "contracts": 5, "won": True, "fee_cents": 1},
            {"action": "settlement", "ticker": "T2-T200", "side": "no",
             "entry_price": 95, "contracts": 3, "won": False, "fee_cents": 1},
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
            path = Path(f.name)

        tr = TrackRecord.from_jsonl(path)
        assert len(tr) == 2  # only settlements
        assert tr.trades[0].won is True
        assert tr.trades[1].won is False
        path.unlink()

    def test_add_trade(self):
        tr = TrackRecord()
        tr.add(_make_trade())
        assert len(tr) == 1
