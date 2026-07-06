"""
TrackRecord: common output type for backtests and live trading.

Provides summary statistics (win rate, P&L, Sharpe-like ratio, drawdown,
calibration check) regardless of whether data came from a backtest or
live trade log.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TradeRecord:
    """A single completed trade."""
    ticker: str
    side: str               # 'yes' or 'no' (or 'buy_yes'/'buy_no' from backtest)
    entry_price: int        # cents
    contracts: int
    exit_price: int         # 100 if won, 0 if lost (for hold-to-settlement)
    fee_cents: int
    days_held: float
    edge_estimate: float = 0.0   # calibration edge at entry (fraction)
    event_ticker: str = ""
    series: str = ""
    generating_process: str = ""
    topic: str = ""
    entry_date: str = ""
    p_event: float = 0.0        # P(YES) at entry
    p_fill: float = 0.0         # P(fill) at entry

    @property
    def won(self) -> bool:
        """True if trade was profitable (exit > entry after fees)."""
        return self.pnl_cents > 0

    @property
    def pnl_cents(self) -> int:
        """Total P&L in cents across all contracts, net of fees."""
        return (self.exit_price - self.entry_price) * self.contracts - self.fee_cents

    @property
    def return_on_capital(self) -> float:
        """Simple return: pnl / capital deployed."""
        capital = self.entry_price * self.contracts
        if capital == 0:
            return 0.0
        return self.pnl_cents / capital

    @property
    def capital_days(self) -> float:
        """Capital-days: entry_price * contracts * days held."""
        return self.entry_price * self.contracts * max(self.days_held, 1)


class TrackRecord:
    """Collection of trades with summary analytics.

    Can be constructed from a backtest, from the live JSONL trade log,
    or manually from a list of TradeRecord objects.
    """

    def __init__(self, trades: list[TradeRecord] | None = None):
        self.trades: list[TradeRecord] = trades or []

    def add(self, trade: TradeRecord):
        self.trades.append(trade)

    def __len__(self):
        return len(self.trades)

    # ─── Summary statistics ───────────────────────────────────────

    def summary(self) -> dict:
        """Aggregate statistics across all trades."""
        if not self.trades:
            return {'n': 0}

        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.won)
        total_pnl = sum(t.pnl_cents for t in self.trades)
        total_capital_days = sum(t.capital_days for t in self.trades)
        returns = [t.return_on_capital for t in self.trades]
        avg_return = sum(returns) / n
        avg_days = sum(t.days_held for t in self.trades) / n

        # Sharpe-like: mean return / std of returns
        if n > 1:
            variance = sum((r - avg_return) ** 2 for r in returns) / (n - 1)
            std_return = math.sqrt(variance)
            sharpe = avg_return / std_return if std_return > 0 else 0.0
        else:
            std_return = 0.0
            sharpe = 0.0

        # Capital efficiency: P&L per capital-day
        pnl_per_cap_day = total_pnl / total_capital_days if total_capital_days > 0 else 0.0

        return {
            'n': n,
            'wins': wins,
            'win_rate': wins / n,
            'total_pnl_cents': total_pnl,
            'total_pnl_dollars': total_pnl / 100,
            'avg_return': avg_return,
            'std_return': std_return,
            'sharpe': sharpe,
            'avg_days_held': avg_days,
            'pnl_per_capital_day': pnl_per_cap_day,
            'annualized_return': pnl_per_cap_day * 365,
        }

    # ─── Grouping ─────────────────────────────────────────────────

    def by_series(self) -> dict[str, 'TrackRecord']:
        """Group trades by series ticker."""
        groups: dict[str, list[TradeRecord]] = {}
        for t in self.trades:
            groups.setdefault(t.series, []).append(t)
        return {k: TrackRecord(v) for k, v in groups.items()}

    def by_category(self) -> dict[str, 'TrackRecord']:
        """Group trades by generating_process."""
        groups: dict[str, list[TradeRecord]] = {}
        for t in self.trades:
            key = t.generating_process or 'unknown'
            groups.setdefault(key, []).append(t)
        return {k: TrackRecord(v) for k, v in groups.items()}

    # ─── Drawdown ─────────────────────────────────────────────────

    def drawdown_series(self) -> list[tuple[int, int, int]]:
        """Cumulative P&L and drawdown from high-water mark.

        Returns list of (trade_index, cumulative_pnl_cents, drawdown_cents).
        Drawdown is absolute (in cents), not percentage — because we track
        cumulative P&L, not portfolio equity, percentage is undefined when
        the high-water mark is zero or negative.
        """
        result = []
        cum_pnl = 0
        hwm = 0  # high-water mark in cents
        for i, t in enumerate(self.trades):
            cum_pnl += t.pnl_cents
            hwm = max(hwm, cum_pnl)
            dd = hwm - cum_pnl  # always >= 0
            result.append((i, cum_pnl, dd))
        return result

    def max_drawdown_cents(self) -> int:
        """Maximum drawdown in cents from high-water mark."""
        series = self.drawdown_series()
        if not series:
            return 0
        return max(dd for _, _, dd in series)

    # ─── Calibration check ────────────────────────────────────────

    def edge_vs_realized(self) -> dict:
        """Compare average predicted edge to average realized return.

        Simple aggregate check — not bucketed. Useful as a quick sanity
        check; for granular calibration analysis use research/calibration.py.
        """
        if not self.trades:
            return {}

        trades_with_edge = [t for t in self.trades if t.edge_estimate > 0]
        if not trades_with_edge:
            return {'n': 0, 'avg_predicted_edge': 0, 'avg_realized_return': 0}

        avg_edge = sum(t.edge_estimate for t in trades_with_edge) / len(trades_with_edge)
        avg_return = sum(t.return_on_capital for t in trades_with_edge) / len(trades_with_edge)

        return {
            'n': len(trades_with_edge),
            'avg_predicted_edge': avg_edge,
            'avg_realized_return': avg_return,
            'edge_capture': avg_return / avg_edge if avg_edge > 0 else 0.0,
        }

    def prediction_decomposition(self) -> dict:
        """Decompose prediction accuracy: P(YES) vs P(fill) vs realized.

        For each trade with p_event > 0, compare:
        - p_event: what the model thought P(YES) was
        - realized: did the event resolve as predicted?
        - p_fill: what the model thought P(fill) was
        - filled: did the order fill? (always True in this track record — only settled trades)

        Returns dict with aggregate statistics and per-side breakdowns.
        """
        trades_with_p = [t for t in self.trades if t.p_event > 0]
        if not trades_with_p:
            return {}

        # P(YES) calibration: for YES-side trades, p_event should match win rate
        # For NO-side trades, (1 - p_event) should match win rate
        yes_trades = [t for t in trades_with_p if t.side in ('yes', 'buy_yes')]
        no_trades = [t for t in trades_with_p if t.side in ('no', 'buy_no')]

        def _calibrate(trades, label):
            if not trades:
                return {}
            # P(win) predicted vs actual
            p_wins = []
            actuals = []
            for t in trades:
                if t.side in ('yes', 'buy_yes'):
                    p_win = t.p_event
                else:
                    p_win = 1.0 - t.p_event
                p_wins.append(p_win)
                actuals.append(1.0 if t.exit_price == 100 else 0.0)

            avg_p_win = sum(p_wins) / len(p_wins)
            actual_win_rate = sum(actuals) / len(actuals)
            return {
                'n': len(trades),
                'avg_p_win': avg_p_win,
                'actual_win_rate': actual_win_rate,
                'p_win_gap': avg_p_win - actual_win_rate,
                'avg_p_fill': sum(t.p_fill for t in trades) / len(trades),
                'avg_edge': sum(t.edge_estimate for t in trades) / len(trades),
                'avg_return': sum(t.return_on_capital for t in trades) / len(trades),
            }

        return {
            'all': _calibrate(trades_with_p, 'all'),
            'yes_side': _calibrate(yes_trades, 'yes'),
            'no_side': _calibrate(no_trades, 'no'),
        }

    # ─── I/O ──────────────────────────────────────────────────────

    @classmethod
    def from_jsonl(cls, path: Path) -> 'TrackRecord':
        """Load from the JSONL trade log (settlement records only)."""
        trades = []
        with open(path) as f:
            for line in f:
                record = json.loads(line)
                if record.get('action') != 'settlement':
                    continue
                trades.append(TradeRecord(
                    ticker=record.get('ticker', ''),
                    side=record.get('side', ''),
                    entry_price=record.get('entry_price', 0),
                    contracts=record.get('contracts', 1),
                    exit_price=100 if record.get('won') else 0,
                    fee_cents=record.get('fee_cents', 0),
                    days_held=0,  # not in current log format; capital_days floors to 1 day
                    edge_estimate=0,
                    event_ticker=record.get('ticker', '').rsplit('-', 1)[0] if record.get('ticker') else '',
                    series=record.get('ticker', '').split('-')[0] if record.get('ticker') else '',
                ))
        return cls(trades)

    def print_summary(self, label: str = ""):
        """Print a formatted summary to stdout."""
        s = self.summary()
        if s['n'] == 0:
            print(f"  [{label or 'TrackRecord'}] No trades")
            return
        header = f"  [{label}]" if label else "  [Summary]"
        print(f"{header}  n={s['n']}")
        print(f"    Win rate:    {s['wins']}/{s['n']} = {s['win_rate']:.1%}")
        print(f"    Total P&L:   {s['total_pnl_cents']:+d}¢ ({s['total_pnl_dollars']:+.2f}$)")
        print(f"    Avg return:  {s['avg_return']:+.2%} per trade")
        print(f"    Sharpe/trade: {s['sharpe']:.2f}")
        print(f"    Avg days:    {s['avg_days_held']:.1f}")
        print(f"    Max DD:      {self.max_drawdown_cents():+d}¢ ({self.max_drawdown_cents()/100:+.2f}$)")
        print(f"    Annualized:  {s['annualized_return']:+.1%} (on deployed capital)")
