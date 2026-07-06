"""
Risk management for FLB tail trading.

Provides pre-trade risk checks, drawdown monitoring, and alpha decay
detection. Wired into the trader's main loop to gate order placement.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RiskLimits:
    """Pre-trade risk checks.

    All limits are configurable. The check() method returns (allowed, reason)
    for a proposed trade.
    """
    # Capital deployment: don't lock up more than this fraction of balance
    max_capital_deployed_pct: float = 0.80

    # Per-event concentration: no single event > this fraction of balance
    max_event_exposure_pct: float = 0.15

    # Maximum number of open positions (resting + filled)
    max_open_positions: int = 50

    # Drawdown kill switch: stop trading if cumulative realized P&L
    # drops below this (cents). 0 = disabled.
    max_loss_cents: int = 0

    def check_deployment(self, balance_cents: int, capital_in_orders: int,
                         proposed_cost: int) -> tuple[bool, str]:
        """Check if proposed trade would exceed capital deployment limit."""
        if balance_cents <= 0:
            return False, "zero balance"
        new_deployed = capital_in_orders + proposed_cost
        deployed_pct = new_deployed / balance_cents
        if deployed_pct > self.max_capital_deployed_pct:
            return False, (f"deployment {deployed_pct:.0%} would exceed "
                           f"{self.max_capital_deployed_pct:.0%} limit")
        return True, ""

    def check_event_concentration(self, balance_cents: int,
                                  event_exposure_cents: int,
                                  proposed_cost: int) -> tuple[bool, str]:
        """Check if proposed trade would over-concentrate in one event."""
        if balance_cents <= 0:
            return False, "zero balance"
        new_exposure = event_exposure_cents + proposed_cost
        exposure_pct = new_exposure / balance_cents
        if exposure_pct > self.max_event_exposure_pct:
            return False, (f"event exposure {exposure_pct:.0%} would exceed "
                           f"{self.max_event_exposure_pct:.0%} limit")
        return True, ""

    def check_position_count(self, current_positions: int) -> tuple[bool, str]:
        """Check if we've hit the maximum open position count."""
        if current_positions >= self.max_open_positions:
            return False, f"{current_positions} positions >= {self.max_open_positions} limit"
        return True, ""


# Default risk limits
DEFAULT_RISK_LIMITS = RiskLimits()


class DrawdownMonitor:
    """Tracks cumulative realized P&L and drawdown from trade log.

    Reads settlement records from the JSONL trade log to compute
    cumulative P&L and high-water mark. Alerts when drawdown exceeds
    a threshold.
    """

    def __init__(self, max_loss_cents: int = 0):
        self.max_loss_cents = max_loss_cents  # 0 = disabled
        self.cumulative_pnl: int = 0
        self.high_water_mark: int = 0
        self.trade_count: int = 0

    def load_from_log(self, log_path: Path):
        """Load cumulative P&L from the JSONL trade log."""
        if not log_path.exists():
            return
        self.cumulative_pnl = 0
        self.high_water_mark = 0
        self.trade_count = 0
        with open(log_path) as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get('action') != 'settlement':
                    continue
                pnl = record.get('pnl_cents', 0)
                self.cumulative_pnl += pnl
                self.high_water_mark = max(self.high_water_mark, self.cumulative_pnl)
                self.trade_count += 1

    def update(self, pnl_cents: int):
        """Update with a new settlement."""
        self.cumulative_pnl += pnl_cents
        self.high_water_mark = max(self.high_water_mark, self.cumulative_pnl)
        self.trade_count += 1

    @property
    def drawdown_cents(self) -> int:
        """Current drawdown from high-water mark in cents."""
        return self.high_water_mark - self.cumulative_pnl

    def should_stop(self) -> tuple[bool, str]:
        """Check if cumulative loss from inception exceeds the kill switch.

        This is an absolute loss limit, not a drawdown-from-HWM check.
        A system that makes $100 then loses $150 will trigger at -$50,
        not at the $150 drawdown from peak. This is intentional: we want
        to protect against net capital destruction, not paper drawdowns.
        """
        if self.max_loss_cents > 0 and self.cumulative_pnl < -self.max_loss_cents:
            return True, (f"cumulative P&L {self.cumulative_pnl}¢ "
                          f"below -{self.max_loss_cents}¢ kill switch")
        return False, ""

    def status(self) -> dict:
        return {
            'cumulative_pnl_cents': self.cumulative_pnl,
            'high_water_mark_cents': self.high_water_mark,
            'drawdown_cents': self.drawdown_cents,
            'trade_count': self.trade_count,
        }


class AlphaDecayMonitor:
    """Detects declining edge by comparing recent outcomes to predictions.

    Tracks a rolling window of trade outcomes and compares realized win
    rate to the calibration edge that justified each trade. If realized
    edge falls significantly below predicted, signals a pause.
    """

    def __init__(self, window_size: int = 50, min_edge_ratio: float = 0.25):
        """
        Args:
            window_size: Number of recent trades to evaluate.
            min_edge_ratio: Minimum ratio of realized/predicted edge.
                If realized edge falls below this fraction of predicted,
                signal a pause. 0.25 = tolerate 75% decay before alerting.
        """
        self.window_size = window_size
        self.min_edge_ratio = min_edge_ratio
        self._trades: list[tuple[float, bool]] = []  # (edge_estimate, won)

    def record(self, edge_estimate: float, won: bool):
        """Record a trade outcome."""
        self._trades.append((edge_estimate, won))

    def load_from_log(self, log_path: Path):
        """Load recent trades from JSONL log.

        Matches order_placed records (which carry edge_estimate) to
        settlement records (which carry won/lost) by ticker.
        """
        if not log_path.exists():
            return
        self._trades.clear()
        # First pass: collect edge estimates from order_placed by ticker
        edges_by_ticker: dict[str, float] = {}
        settlements: list[tuple[str, bool]] = []
        with open(log_path) as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get('action') == 'order_placed':
                    ticker = record.get('ticker', '')
                    edge = record.get('edge_estimate', 0)
                    if ticker and edge > 0:
                        edges_by_ticker[ticker] = edge
                elif record.get('action') == 'settlement':
                    ticker = record.get('ticker', '')
                    won = record.get('won', False)
                    settlements.append((ticker, won))
        # Match settlements to their placement edges
        for ticker, won in settlements:
            edge = edges_by_ticker.get(ticker, 0)
            if edge > 0:
                self._trades.append((edge, won))

    @property
    def recent_trades(self) -> list[tuple[float, bool]]:
        """The most recent window_size trades."""
        return self._trades[-self.window_size:]

    def should_pause(self) -> tuple[bool, str]:
        """Check if recent performance suggests alpha decay.

        Compares average predicted edge to realized win rate excess.
        Only evaluates when we have enough trades in the window.
        """
        recent = self.recent_trades
        if len(recent) < self.window_size:
            return False, f"insufficient data ({len(recent)}/{self.window_size})"

        # Average predicted edge
        avg_edge = sum(e for e, _ in recent) / len(recent)
        if avg_edge <= 0:
            return False, "no positive edge predictions in window"

        # Realized win rate vs implied
        # For tail trades at ~90¢, the implied win rate is ~90%.
        # Edge of 2% means we expect 92% win rate.
        # We approximate: realized_edge ≈ win_rate - (1 - avg_edge)
        # But simpler: just compare win_rate to predicted.
        wins = sum(1 for _, w in recent if w)
        win_rate = wins / len(recent)

        # Expected win rate ≈ 0.90 + edge (rough, since most trades are at ~90¢)
        # More precisely: avg(entry_price/100) + avg(edge)
        # For now, just check if wins are significantly below expectation
        # using edge as the excess win rate we expect.
        # Realized excess = win_rate - (1 - avg_edge) ≈ win_rate - 0.90
        # But we don't have entry prices here, so use a simpler check:
        # Is win_rate below a reasonable floor?

        # Simple check: if we predicted avg_edge of 2%, we expect ~92% wins.
        # If we're getting <88% wins (edge_ratio < 0.25 of predicted surplus),
        # that's a decay signal.
        expected_floor = 0.85  # baseline for tail zone
        predicted_surplus = avg_edge
        realized_surplus = win_rate - expected_floor

        if predicted_surplus > 0:
            ratio = realized_surplus / predicted_surplus
            if ratio < self.min_edge_ratio:
                return True, (f"edge decay: realized surplus {realized_surplus:.1%} / "
                              f"predicted {predicted_surplus:.1%} = {ratio:.0%} "
                              f"(below {self.min_edge_ratio:.0%} threshold, "
                              f"window={len(recent)} trades)")
        return False, ""

    def status(self) -> dict:
        recent = self.recent_trades
        if not recent:
            return {'n': 0}
        wins = sum(1 for _, w in recent if w)
        avg_edge = sum(e for e, _ in recent) / len(recent)
        return {
            'n': len(recent),
            'window_size': self.window_size,
            'win_rate': wins / len(recent),
            'avg_predicted_edge': avg_edge,
        }
