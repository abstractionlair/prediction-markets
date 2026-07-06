"""Runner: generic strategy replay using ViewFactory.

Replaces bespoke expanding-window logic in replay.py with a generic loop.
The Runner is infrastructure — it works with any strategy that conforms
to the scan() interface and any estimators registered in ViewFactory.

Spec: Section 5.3, Chunk 7

Usage:
    runner = Runner(view_factory, EVStrategy, strategy_kwargs={'params': params})
    track = runner.replay(market_source, fill_simulator=my_sim)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from track_record import TrackRecord


# ── Protocols ────────────────────────────────────────────────


@runtime_checkable
class MarketSource(Protocol):
    """Provides historical market state for replay.

    Implementations load from DB, CSV, or synthetic data.
    The Runner iterates periods() and queries events/settlements.
    """

    def periods(self) -> list[datetime]:
        """All evaluation timestamps, sorted ascending."""
        ...

    def events_at(self, period: datetime) -> list[dict]:
        """API-shaped event dicts available at this period.

        Each event dict has 'event_ticker' and 'markets' (list of market dicts).
        Market dicts have at minimum: 'ticker', price fields, 'expected_expiration_time'.
        """
        ...

    def settlement(self, ticker: str) -> dict | None:
        """Settlement info for a ticker, or None if not yet settled.

        Returns: {result: 'yes'|'no', settled_at: datetime,
                  event: str, series: str}
        """
        ...


@runtime_checkable
class FillSimulator(Protocol):
    """Determines which pending orders fill during replay.

    Simple modes (candle-based, tape-based) need only the order dict and
    period timestamp. Conditional modes (e.g., Bernoulli fills conditioned
    on settlement outcome) require additional context — inject settlement
    data, view, or market source via the simulator's constructor.
    """

    def on_order(self, ticker: str, order: dict, period: datetime) -> None:
        """Called when an order is placed. Setup fill tracking."""
        ...

    def check_fills(self, ticker: str, order: dict, period: datetime) -> int:
        """Return number of additional contracts filled this period."""
        ...


# ── Built-in fill simulators ────────────────────────────────


class InstantFillSimulator:
    """All orders fill immediately (next period).

    Default fill model — useful for testing the core loop without
    fill complexity. Optimistic: every order fills completely.
    """

    def on_order(self, ticker: str, order: dict, period: datetime) -> None:
        pass

    def check_fills(self, ticker: str, order: dict, period: datetime) -> int:
        return order['contracts'] - order.get('contracts_filled', 0)


# ── Runner ───────────────────────────────────────────────────


class Runner:
    """Generic strategy replay using ViewFactory.

    Replaces bespoke expanding-window logic. Works with any strategy
    class that accepts (view, **kwargs) and has a scan() method returning
    a list of opportunities with ticker/side/limit_price/contracts attrs.

    The Runner:
    1. Builds Views via ViewFactory at each recalibration boundary
    2. Creates a fresh strategy instance per recalibration
    3. Applies independent cost verification via View.cost()
    4. Records results to TrackRecord
    """

    def __init__(self, view_factory, strategy_cls: type,
                 strategy_kwargs: dict | None = None):
        """
        Args:
            view_factory: ViewFactory (or duck-typed equivalent with build(as_of)).
            strategy_cls: Strategy class. Instantiated as strategy_cls(view, **kwargs).
            strategy_kwargs: Extra kwargs passed to strategy constructor.
        """
        self.view_factory = view_factory
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs or {}

    def replay(self, market_source: MarketSource,
               fill_simulator: FillSimulator | None = None,
               recalibration_schedule: str = 'daily',
               risk_limits=None,
               starting_capital_cents: int = 10000) -> TrackRecord:
        """Expanding-window replay over historical data.

        For each evaluation point:
        1. Build a View with as_of = recalibration boundary (via ViewFactory)
        2. Settle resolved positions (independent cost verification)
        3. Process fills via FillSimulator
        4. Reconstruct market state from MarketSource
        5. Run strategy.scan()
        6. Place orders with capital and risk checks

        Args:
            market_source: Provides periods, events, and settlement data.
            fill_simulator: Determines fills. None = InstantFillSimulator.
            recalibration_schedule: 'daily', 'weekly', or 'hourly'.
            risk_limits: Optional risk limits (duck-typed, check_deployment/
                check_event_concentration/check_position_count methods).
            starting_capital_cents: Initial cash.

        Returns:
            TrackRecord with independently cost-verified trades.
        """
        # Deferred import: framework/ doesn't depend on trading/ at import time
        from track_record import TradeRecord, TrackRecord

        if fill_simulator is None:
            fill_simulator = InstantFillSimulator()

        track = TrackRecord()
        cash = starting_capital_cents
        escrow = 0
        pending: dict[str, dict] = {}   # ticker → order (awaiting fill)
        filled: dict[str, dict] = {}    # ticker → order (filled, awaiting settlement)
        active_tickers: set[str] = set()

        current_view = None
        current_strategy = None
        last_boundary = None

        for period in market_source.periods():

            # ── 0. Recalibrate if new boundary ───────────────────
            boundary = _recalibration_boundary(period, recalibration_schedule)
            if boundary != last_boundary:
                current_view = self.view_factory.build(as_of=boundary)
                current_strategy = self.strategy_cls(
                    current_view, **self.strategy_kwargs)
                last_boundary = boundary

            # ── 1. Settle resolved markets ───────────────────────
            settled_tickers = []

            for ticker in list(filled):
                settlement = market_source.settlement(ticker)
                if settlement is None or settlement['settled_at'] > period:
                    continue
                order = filled.pop(ticker)
                released, received = _settle_and_account(
                    order, settlement, ticker,
                    order['contracts'], current_view, track, TradeRecord)
                escrow -= released
                cash += received
                settled_tickers.append(ticker)

            # Pending orders for settled markets
            for ticker in list(pending):
                settlement = market_source.settlement(ticker)
                if settlement is None or settlement['settled_at'] > period:
                    continue
                order = pending.pop(ticker)
                filled_qty = order.get('contracts_filled', 0)
                unfilled = order['contracts'] - filled_qty
                # Return unfilled escrow
                escrow -= order['price'] * unfilled
                cash += order['price'] * unfilled

                if filled_qty > 0:
                    released, received = _settle_and_account(
                        order, settlement, ticker,
                        filled_qty, current_view, track, TradeRecord)
                    escrow -= released
                    cash += received

                settled_tickers.append(ticker)

            for t in settled_tickers:
                active_tickers.discard(t)

            # ── 2. Process fills on pending orders ───────────────
            for ticker in list(pending):
                order = pending[ticker]
                newly_filled = fill_simulator.check_fills(ticker, order, period)
                if newly_filled > 0:
                    order['contracts_filled'] = (
                        order.get('contracts_filled', 0) + newly_filled)
                    if order['contracts_filled'] >= order['contracts']:
                        filled[ticker] = pending.pop(ticker)

            # ── 3. Scan for opportunities ────────────────────────
            events = market_source.events_at(period)
            if not events or current_strategy is None:
                continue

            opps = current_strategy.scan(
                events, traded_tickers=active_tickers, now=period)

            # ── 4. Place orders ──────────────────────────────────
            if risk_limits is not None and hasattr(risk_limits, 'check_position_count'):
                current_positions = len(pending) + len(filled)
                ok, _ = risk_limits.check_position_count(current_positions)
                if not ok:
                    continue  # skip all orders this period

            total_equity = cash + escrow

            for opp in opps:
                ticker = opp.ticker
                if ticker in active_tickers:
                    continue

                price = opp.limit_price
                contracts = opp.contracts
                cost = price * contracts

                if cash < cost:
                    continue

                # Per-order risk checks
                if risk_limits is not None:
                    if hasattr(risk_limits, 'check_deployment'):
                        ok, _ = risk_limits.check_deployment(
                            total_equity, escrow, cost)
                        if not ok:
                            continue
                    if hasattr(risk_limits, 'check_event_concentration'):
                        event_exposure = sum(
                            o['price'] * o['contracts']
                            for o in list(pending.values()) + list(filled.values())
                            if o.get('event') == opp.event_ticker
                        )
                        ok, _ = risk_limits.check_event_concentration(
                            total_equity, event_exposure, cost)
                        if not ok:
                            continue

                order = {
                    'side': opp.side,
                    'price': price,
                    'contracts': contracts,
                    'contracts_filled': 0,
                    'event': opp.event_ticker,
                    'edge': getattr(opp, 'ev_per_contract', 0) / 100.0,
                    'placed_at': period,
                    'gp': getattr(opp, 'generating_process', ''),
                    'topic': getattr(opp, 'topic', ''),
                    'p_event': getattr(opp, 'p_event', 0.0),
                    'p_fill': getattr(opp, 'p_fill', 0.0),
                }

                fill_simulator.on_order(ticker, order, period)

                pending[ticker] = order
                cash -= cost
                escrow += cost
                active_tickers.add(ticker)

        # ── Settle everything remaining ──────────────────────────

        if current_view is not None:
            for ticker, order in list(filled.items()):
                settlement = market_source.settlement(ticker)
                if settlement is None:
                    continue
                _settle_and_account(
                    order, settlement, ticker,
                    order['contracts'], current_view, track, TradeRecord)

            for ticker, order in list(pending.items()):
                settlement = market_source.settlement(ticker)
                if settlement is None:
                    continue
                filled_qty = order.get('contracts_filled', 0)
                if filled_qty > 0:
                    _settle_and_account(
                        order, settlement, ticker,
                        filled_qty, current_view, track, TradeRecord)

        return track

    def validate_estimator(self, estimator_name: str,
                           split_date: datetime,
                           metric_fn) -> ValidationResult:
        """Temporal split validation for any registered estimator.

        1. Build a view with as_of = split_date
        2. Extract the estimator's predictions on post-split data
        3. Compare to actuals using metric_fn
        4. Return gap metrics

        This is a mechanical process that works for any estimator
        implementing the protocol. Requires a MarketSource or equivalent
        to provide post-split actuals.

        Args:
            estimator_name: Name of the estimator to validate.
            split_date: Temporal boundary — estimator trained on pre-split,
                        evaluated on post-split.
            metric_fn: Callable(predictions, actuals) -> dict of metric values.

        Returns:
            ValidationResult with gap metrics.
        """
        raise NotImplementedError(
            "validate_estimator requires post-split data access. "
            "Implementation deferred until MarketSource provides "
            "actuals-query interface (Chunk 8+)."
        )


@dataclass
class ValidationResult:
    """Result of temporal split validation for an estimator."""
    estimator_name: str
    split_date: datetime
    n_predictions: int = 0
    metric_values: dict = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────


def _recalibration_boundary(period: datetime, schedule: str) -> datetime:
    """Truncate period to the recalibration boundary.

    'daily':  midnight UTC of the period's date
    'weekly': Monday 00:00 UTC of the period's week
    'hourly': top of the period's hour
    """
    if schedule == 'daily':
        return period.replace(hour=0, minute=0, second=0, microsecond=0)
    elif schedule == 'weekly':
        weekday = period.weekday()
        monday = period - timedelta(days=weekday)
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    elif schedule == 'hourly':
        return period.replace(minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown recalibration schedule: {schedule!r}")


def _did_win(side: str, result: str) -> bool:
    """Determine if a trade won based on side and settlement result."""
    return (side == 'yes' and result == 'yes') or (
        side == 'no' and result == 'no')


def _settle_and_account(order: dict, settlement: dict, ticker: str,
                        contracts: int, view, track, TradeRecord
                        ) -> tuple[int, int]:
    """Record a settled trade and return capital changes.

    Returns:
        (escrow_released, cash_received): escrow to release and cash to add.
        Callers update their own cash/escrow, or ignore for end-of-replay.
    """
    _record_trade(order, settlement, ticker, contracts, view, track, TradeRecord)
    won = _did_win(order['side'], settlement['result'])
    exit_price = 100 if won else 0
    fee = view.cost(order['price'], contracts)
    return order['price'] * contracts, exit_price * contracts - fee


def _record_trade(order: dict, settlement: dict, ticker: str,
                  contracts: int, view, track, TradeRecord) -> None:
    """Record a settled trade with independently verified costs.

    Independent cost verification (spec Section 5.4): the Runner applies
    View.cost() regardless of what the strategy internally calculated.
    """
    won = _did_win(order['side'], settlement['result'])
    exit_price = 100 if won else 0
    verified_fee = view.cost(order['price'], contracts)

    track.add(TradeRecord(
        ticker=ticker,
        side=order['side'],
        entry_price=order['price'],
        contracts=contracts,
        exit_price=exit_price,
        fee_cents=verified_fee,
        days_held=(
            settlement['settled_at'] - order['placed_at']
        ).total_seconds() / 86400,
        edge_estimate=order.get('edge', 0.0),
        event_ticker=order.get('event', ''),
        series=settlement.get('series', ticker.split('-')[0]),
        generating_process=order.get('gp', ''),
        topic=order.get('topic', ''),
        entry_date=order['placed_at'].strftime('%Y-%m-%d %H:%M'),
        p_event=order.get('p_event', 0.0) or 0.0,
        p_fill=order.get('p_fill', 0.0) or 0.0,
    ))
