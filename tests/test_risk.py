"""Tests for trading/risk.py — risk limits, drawdown, alpha decay."""

import json
import tempfile
from pathlib import Path

from trading.risk import (
    RiskLimits,
    DEFAULT_RISK_LIMITS,
    DrawdownMonitor,
    AlphaDecayMonitor,
)


# ─── RiskLimits ───────────────────────────────────────────────────

class TestRiskLimitsDeployment:
    def test_within_limit(self):
        rl = RiskLimits(max_capital_deployed_pct=0.80)
        ok, _ = rl.check_deployment(balance_cents=10000, capital_in_orders=5000,
                                    proposed_cost=2000)
        assert ok  # 7000/10000 = 70% < 80%

    def test_exceeds_limit(self):
        rl = RiskLimits(max_capital_deployed_pct=0.80)
        ok, reason = rl.check_deployment(balance_cents=10000, capital_in_orders=7000,
                                         proposed_cost=2000)
        assert not ok  # 9000/10000 = 90% > 80%
        assert "90%" in reason

    def test_at_boundary(self):
        rl = RiskLimits(max_capital_deployed_pct=0.80)
        ok, _ = rl.check_deployment(balance_cents=10000, capital_in_orders=6000,
                                    proposed_cost=2000)
        assert ok  # 8000/10000 = 80% = limit (not exceeded)

    def test_zero_balance(self):
        rl = RiskLimits()
        ok, reason = rl.check_deployment(balance_cents=0, capital_in_orders=0,
                                         proposed_cost=100)
        assert not ok
        assert "zero balance" in reason


class TestRiskLimitsConcentration:
    def test_within_limit(self):
        rl = RiskLimits(max_event_exposure_pct=0.15)
        ok, _ = rl.check_event_concentration(balance_cents=10000,
                                             event_exposure_cents=500,
                                             proposed_cost=500)
        assert ok  # 1000/10000 = 10% < 15%

    def test_exceeds_limit(self):
        rl = RiskLimits(max_event_exposure_pct=0.15)
        ok, reason = rl.check_event_concentration(balance_cents=10000,
                                                  event_exposure_cents=1000,
                                                  proposed_cost=1000)
        assert not ok  # 2000/10000 = 20% > 15%


class TestRiskLimitsPositionCount:
    def test_within_limit(self):
        rl = RiskLimits(max_open_positions=50)
        ok, _ = rl.check_position_count(30)
        assert ok

    def test_at_limit(self):
        rl = RiskLimits(max_open_positions=50)
        ok, _ = rl.check_position_count(50)
        assert not ok

    def test_custom_limit(self):
        rl = RiskLimits(max_open_positions=10)
        ok, _ = rl.check_position_count(10)
        assert not ok


class TestDefaultRiskLimits:
    def test_defaults(self):
        rl = DEFAULT_RISK_LIMITS
        assert rl.max_capital_deployed_pct == 0.80
        assert rl.max_event_exposure_pct == 0.15
        assert rl.max_open_positions == 50

    def test_frozen(self):
        try:
            DEFAULT_RISK_LIMITS.max_capital_deployed_pct = 0.99
            assert False, "Should have raised"
        except AttributeError:
            pass


# ─── DrawdownMonitor ──────────────────────────────────────────────

class TestDrawdownMonitor:
    def test_initial_state(self):
        dm = DrawdownMonitor()
        assert dm.cumulative_pnl == 0
        assert dm.high_water_mark == 0
        assert dm.drawdown_cents == 0

    def test_winning_updates(self):
        dm = DrawdownMonitor()
        dm.update(100)  # +100¢
        assert dm.cumulative_pnl == 100
        assert dm.high_water_mark == 100
        assert dm.drawdown_cents == 0

    def test_drawdown_after_loss(self):
        dm = DrawdownMonitor()
        dm.update(100)   # cum=100, hwm=100
        dm.update(-300)  # cum=-200, hwm=100
        assert dm.cumulative_pnl == -200
        assert dm.high_water_mark == 100
        assert dm.drawdown_cents == 300

    def test_recovery(self):
        dm = DrawdownMonitor()
        dm.update(100)
        dm.update(-200)  # cum=-100, hwm=100, dd=200
        dm.update(400)   # cum=300, hwm=300, dd=0
        assert dm.drawdown_cents == 0
        assert dm.high_water_mark == 300

    def test_kill_switch_disabled(self):
        dm = DrawdownMonitor(max_loss_cents=0)
        dm.update(-10000)
        stop, _ = dm.should_stop()
        assert not stop

    def test_kill_switch_triggered(self):
        dm = DrawdownMonitor(max_loss_cents=5000)
        dm.update(-3000)
        stop, _ = dm.should_stop()
        assert not stop  # -3000 > -5000

        dm.update(-3000)  # cum = -6000
        stop, reason = dm.should_stop()
        assert stop
        assert "-5000" in reason

    def test_load_from_log(self):
        records = [
            {"action": "order_placed", "ticker": "T1"},
            {"action": "settlement", "pnl_cents": 50},
            {"action": "settlement", "pnl_cents": -200},
            {"action": "settlement", "pnl_cents": 30},
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
            path = Path(f.name)

        dm = DrawdownMonitor()
        dm.load_from_log(path)
        assert dm.trade_count == 3
        assert dm.cumulative_pnl == 50 - 200 + 30  # -120
        assert dm.high_water_mark == 50
        path.unlink()

    def test_load_from_missing_log(self):
        dm = DrawdownMonitor()
        dm.load_from_log(Path("/nonexistent/trades.jsonl"))
        assert dm.trade_count == 0

    def test_status(self):
        dm = DrawdownMonitor()
        dm.update(100)
        dm.update(-50)
        s = dm.status()
        assert s['cumulative_pnl_cents'] == 50
        assert s['high_water_mark_cents'] == 100
        assert s['drawdown_cents'] == 50
        assert s['trade_count'] == 2


# ─── AlphaDecayMonitor ────────────────────────────────────────────

class TestAlphaDecayMonitor:
    def test_insufficient_data(self):
        adm = AlphaDecayMonitor(window_size=50)
        for _ in range(10):
            adm.record(0.02, True)
        pause, reason = adm.should_pause()
        assert not pause
        assert "insufficient" in reason

    def test_healthy_performance(self):
        adm = AlphaDecayMonitor(window_size=50, min_edge_ratio=0.25)
        # 95% win rate with 2% predicted edge — very healthy
        for i in range(50):
            adm.record(0.02, i < 47)  # 47/50 = 94% wins
        pause, _ = adm.should_pause()
        assert not pause

    def test_decayed_performance(self):
        adm = AlphaDecayMonitor(window_size=50, min_edge_ratio=0.25)
        # 70% win rate with 2% predicted edge — well below expected ~92%
        for i in range(50):
            adm.record(0.02, i < 35)  # 35/50 = 70% wins
        pause, reason = adm.should_pause()
        assert pause
        assert "decay" in reason

    def test_no_positive_edge(self):
        adm = AlphaDecayMonitor(window_size=10)
        for _ in range(10):
            adm.record(0.0, True)
        pause, reason = adm.should_pause()
        assert not pause
        assert "no positive edge" in reason

    def test_recent_trades_window(self):
        adm = AlphaDecayMonitor(window_size=5)
        for i in range(20):
            adm.record(0.02, True)
        assert len(adm.recent_trades) == 5

    def test_load_from_log(self):
        # Alpha monitor matches order_placed (with edge) to settlements (with won)
        records = [
            {"action": "order_placed", "ticker": "T1", "edge_estimate": 0.02},
            {"action": "order_placed", "ticker": "T2", "edge_estimate": 0.03},
            {"action": "settlement", "ticker": "T1", "won": True},
            {"action": "settlement", "ticker": "T2", "won": False},
            {"action": "settlement", "ticker": "T3", "won": True},  # no matching placement
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
            path = Path(f.name)

        adm = AlphaDecayMonitor(window_size=50)
        adm.load_from_log(path)
        # Only T1 and T2 have matching placements with edge > 0
        assert len(adm._trades) == 2
        assert adm._trades[0] == (0.02, True)   # T1: edge=0.02, won
        assert adm._trades[1] == (0.03, False)  # T2: edge=0.03, lost
        path.unlink()

    def test_status(self):
        adm = AlphaDecayMonitor(window_size=10)
        for i in range(10):
            adm.record(0.025, i < 9)
        s = adm.status()
        assert s['n'] == 10
        assert s['win_rate'] == 0.9
        assert abs(s['avg_predicted_edge'] - 0.025) < 1e-10
