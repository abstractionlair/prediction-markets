#!/usr/bin/env python3
"""
FLB Tail Trading System for Kalshi.

Continuously scans active markets for tail opportunities using calibration-backed
edge estimates. Places paired resting limit orders at the bid/ask midpoint.
Monitors fills and settlements.

Usage:
    # Dry run (print what would be traded, no orders placed):
    python trader.py --dry-run

    # Live trading (uses Kalshi balance):
    python trader.py

    # Demo environment:
    python trader.py --demo
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from trading.kalshi_client import KalshiClient
from trading.ev_strategy import EVStrategy, EVOpportunity
from trading.view_bootstrap import build_view_factory_from_db
from trading.strategy import (
    DEFAULT_PARAMS,
    Opportunity,
    TradePair,
    TradingParams,
    maker_fee,
    optimal_quantity,
)
from trading.flb_strategy import FLBStrategy
from trading.risk import RiskLimits, DrawdownMonitor, AlphaDecayMonitor, DEFAULT_RISK_LIMITS

# ─── Configuration (from strategy.py canonical source) ──────────────
# Import defaults so existing references work without changing every line.
MIN_TAIL = DEFAULT_PARAMS.min_tail
MAX_TAIL = DEFAULT_PARAMS.max_tail
MAX_SPREAD = DEFAULT_PARAMS.max_spread
MAX_DAYS_TO_SETTLE = DEFAULT_PARAMS.max_days_to_settle
MAX_CONTRACTS = DEFAULT_PARAMS.max_contracts
MAKER_FEE_RATE = DEFAULT_PARAMS.maker_fee_rate
MIN_EDGE = DEFAULT_PARAMS.min_edge
MAX_QUALIFYING_EVENTS = DEFAULT_PARAMS.max_qualifying_events
SCAN_INTERVAL_SECONDS = DEFAULT_PARAMS.scan_interval_seconds

# Log file
LOG_DIR = Path(__file__).parent / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("trader")


# ─── Database helpers ──────────────────────────────────────────────

def _get_pg_dsn():
    return os.environ.get("CLAUDE_HUB_PG_DSN", "postgresql://claude_hub_app@localhost/claude_hub")


def load_trading_params() -> TradingParams:
    """Load trading parameters from the database.

    Falls back to defaults if the table doesn't exist or is empty.
    This is the single source of truth for production parameters —
    change them in the DB, not in code.
    """
    try:
        conn = psycopg2.connect(_get_pg_dsn())
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM prediction_markets.trading_parameters")
        rows = {k: v for k, v in cur.fetchall()}
        conn.close()
    except Exception as e:
        log.warning(f"Could not load trading_parameters from DB ({e}), using defaults")
        return TradingParams()

    if not rows:
        return TradingParams()

    # Parse with type coercion, falling back to defaults for missing keys
    defaults = TradingParams()
    return TradingParams(
        min_tail=int(rows.get('min_tail', defaults.min_tail)),
        max_tail=int(rows.get('max_tail', defaults.max_tail)),
        max_spread=int(rows.get('max_spread', defaults.max_spread)),
        max_days_to_settle=float(rows.get('max_days_to_settle', defaults.max_days_to_settle)),
        max_contracts=int(rows.get('max_contracts', defaults.max_contracts)),
        maker_fee_rate=float(rows.get('maker_fee_rate', defaults.maker_fee_rate)),
        min_edge=float(rows.get('min_edge', defaults.min_edge)),
        max_qualifying_events=int(rows.get('max_qualifying_events', defaults.max_qualifying_events)),
        scan_interval_seconds=int(rows.get('scan_interval_seconds', defaults.scan_interval_seconds)),
    )


# ─── Calibration edge lookup ────────────────────────────────────────


class EdgeLookup:
    """Database-backed FLB edge estimates from calibration_edges table.

    Loads (generating_process, topic) -> sorted list of (hours_from, hours_to, smoothed_edge)
    and series -> (generating_process, topic) mapping from market_classifications.

    The price_method parameter selects which edge version to load:
      'mid' (default), 'bid', 'ask', or 'trade'.
    """

    def __init__(self, price_method='mid'):
        self.price_method = price_method
        self.edges = {}       # (gp, topic) -> [(hours_from, hours_to, smoothed_edge), ...]
        self.series_map = {}  # series_ticker -> (generating_process, topic)
        self._load()

    def _load(self):
        conn = psycopg2.connect(_get_pg_dsn())
        try:
            cur = conn.cursor()

            # Load calibration edges for the selected price method
            cur.execute("""
                SELECT generating_process, topic, hours_from, hours_to, smoothed_edge
                FROM prediction_markets.calibration_edges
                WHERE price_method = %s
                ORDER BY generating_process, topic, bucket_index
            """, (self.price_method,))
            for gp, topic, h_from, h_to, edge in cur:
                key = (gp, topic)
                if key not in self.edges:
                    self.edges[key] = []
                self.edges[key].append((float(h_from), float(h_to), float(edge)))

            # Load series -> (gp, topic) mapping
            cur.execute("""
                SELECT series_ticker, generating_process, topic
                FROM prediction_markets.market_classifications
                WHERE generating_process IS NOT NULL AND topic IS NOT NULL
            """)
            for series, gp, topic in cur:
                self.series_map[series] = (gp, topic)

            cur.close()
        finally:
            conn.close()

        log.info(f"Edge lookup ({self.price_method}): {len(self.edges)} (process × topic) cells, "
                 f"{sum(len(v) for v in self.edges.values())} buckets, "
                 f"{len(self.series_map)} classified series")

    def get_edge(self, series: str, hours_to_settlement: float,
                 observed_price_cents=None, side=None) -> float | None:
        """Look up smoothed edge for a series at a given time-to-settlement.

        Returns edge as a fraction (e.g., 0.03 for 3%), or None if
        the series is unclassified or no calibration data exists.
        observed_price_cents and side are accepted but ignored (legacy).
        """
        classification = self.series_map.get(series)
        if classification is None:
            return None

        buckets = self.edges.get(classification)
        if not buckets:
            return None

        # Find the bucket containing this hours_to_settlement
        for h_from, h_to, edge in buckets:
            if h_from <= hours_to_settlement <= h_to:
                return edge

        # Outside all bucket ranges: don't extrapolate (edge can flip sign
        # across horizons, e.g. hazard_process × financial: -55% to +9%)
        return None

    def get_event_rate(self, series, hours_to_settlement, observed_price):
        """Not supported on EdgeLookup — returns None."""
        return None

    def get_classification(self, series: str) -> tuple[str, str] | None:
        """Return (generating_process, topic) for a series, or None."""
        return self.series_map.get(series)


class CalibrationLookup:
    """Database-backed P(YES) estimates from calibration_rates table.

    Loads (generating_process, topic, price_bucket) -> sorted time buckets
    with smoothed_event_rate. The price_method selects which conditioning
    price was used (bid/mid/ask/trade).

    The strategy calls get_edge(series, hours, observed_price) which:
      1. Finds the price bucket for observed_price
      2. Looks up P(YES) for (process, topic, price_bucket, time)
      3. Returns edge = P(YES) - observed_price/100 (for YES-side tail)
         or edge = (1-P(YES)) - observed_price/100 (for NO-side tail)

    For the simplified interface (get_edge without price), returns None —
    this lookup requires a price.
    """

    def __init__(self, price_method='mid'):
        self.price_method = price_method
        # (gp, topic, price_lo, price_hi) -> [(h_from, h_to, event_rate)]
        self.rates = {}
        self.series_map = {}
        self._load()

    def _load(self):
        conn = psycopg2.connect(_get_pg_dsn())
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT generating_process, topic, price_lo, price_hi,
                       hours_from, hours_to, smoothed_event_rate
                FROM prediction_markets.calibration_rates
                WHERE price_method = %s
                ORDER BY generating_process, topic, price_lo, bucket_index
            """, (self.price_method,))
            for gp, topic, p_lo, p_hi, h_from, h_to, rate in cur:
                key = (gp, topic, float(p_lo), float(p_hi))
                if key not in self.rates:
                    self.rates[key] = []
                self.rates[key].append((float(h_from), float(h_to), float(rate)))

            cur.execute("""
                SELECT series_ticker, generating_process, topic
                FROM prediction_markets.market_classifications
                WHERE generating_process IS NOT NULL AND topic IS NOT NULL
            """)
            for series, gp, topic in cur:
                self.series_map[series] = (gp, topic)
            cur.close()
        finally:
            conn.close()

        n_cells = len(self.rates)
        n_buckets = sum(len(v) for v in self.rates.values())
        log.info(f"Calibration lookup ({self.price_method}): "
                 f"{n_cells} (process×topic×price) cells, "
                 f"{n_buckets} time buckets, "
                 f"{len(self.series_map)} classified series")

    @staticmethod
    def _price_bucket(p):
        """Return (lo, hi) for a YES price in dollars."""
        for lo, hi in [(i/20, (i+1)/20) for i in range(20)]:
            if lo <= p < hi:
                return (lo, hi)
        return None

    def get_event_rate(self, series, hours_to_settlement, observed_price_dollars):
        """Look up P(YES) for a series at given time and observed price.

        observed_price_dollars is the YES-side price in dollars (0-1 range).
        Returns smoothed P(YES) or None if no data for this cell.
        """
        classification = self.series_map.get(series)
        if classification is None:
            return None
        gp, topic = classification
        pb = self._price_bucket(observed_price_dollars)
        if pb is None:
            return None
        key = (gp, topic, pb[0], pb[1])
        buckets = self.rates.get(key)
        if not buckets:
            return None
        for h_from, h_to, rate in buckets:
            if h_from <= hours_to_settlement <= h_to:
                return rate
        return None

    def get_edge(self, series, hours_to_settlement,
                 observed_price_cents=None, side=None):
        """Compute edge for a tail observation.

        observed_price_cents: YES-side mid price in cents.
        side: 'yes' or 'no' — which side the strategy is buying.

        Looks up P(YES) at the observed YES-side price bucket.
        For YES tails: edge = P(YES) - yes_price  (YES underpriced → positive)
        For NO tails: edge = yes_price - P(YES)  (YES overpriced → positive)
          Equivalently: P(NO) - no_price.
        """
        if observed_price_cents is None:
            return None
        yes_price_dollars = observed_price_cents / 100.0
        p_yes = self.get_event_rate(series, hours_to_settlement, yes_price_dollars)
        if p_yes is None:
            return None
        if side == 'no':
            return yes_price_dollars - p_yes
        else:
            return p_yes - yes_price_dollars

    def get_classification(self, series):
        """Return (generating_process, topic) for a series, or None."""
        return self.series_map.get(series)


# ─── Data structures ────────────────────────────────────────────────
# Opportunity and TradePair are imported from strategy.py


@dataclass
class OpenOrder:
    """Tracks a resting order we've placed."""
    order_id: str
    client_order_id: str
    ticker: str
    side: str
    price: int
    contracts: int
    placed_at: str
    event_ticker: str = ""


@dataclass
class TradingState:
    """Current state of the trading system."""
    balance_cents: int = 0
    open_orders: dict = field(default_factory=dict)   # order_id -> OpenOrder
    filled_orders: dict = field(default_factory=dict)  # ticker -> OpenOrder (entry price preserved)
    positions: dict = field(default_factory=dict)      # ticker -> position dict
    traded_tickers: set = field(default_factory=set)   # tickers we've already traded

    @property
    def capital_in_orders(self) -> int:
        """Capital locked in resting orders (cents).

        The Kalshi balance API does NOT subtract resting order escrow,
        but the server DOES enforce it on order placement. We must
        track this ourselves.
        """
        return sum(o.price * o.contracts for o in self.open_orders.values())

    @property
    def available_cents(self) -> int:
        """Available capital = reported balance - resting order escrow."""
        return self.balance_cents - self.capital_in_orders


# ─── Scanner ─────────────────────────────────────────────────────────
# BLOCKED_SERIES and TradePair are imported from strategy.py


def _load_market_structure() -> dict[str, str]:
    """Load market_structure for all events from our DB."""
    try:
        conn = psycopg2.connect(_get_pg_dsn())
        cur = conn.cursor()
        cur.execute("""
            SELECT event_ticker, market_structure
            FROM prediction_markets.kalshi_events
            WHERE market_structure IS NOT NULL
        """)
        result = {row[0]: row[1] for row in cur}
        conn.close()
        return result
    except Exception as e:
        log.warning(f"Failed to load market_structure: {e}")
        return {}


def scan_for_pairs(client: KalshiClient, state: TradingState,
                   strategy: FLBStrategy,
                   unpaired: bool = False) -> list[TradePair]:
    """Fetch events from API, then delegate to FLBStrategy for scanning.

    This is the thin execution layer: API call + logging.
    All filtering, pairing, and ranking logic lives in FLBStrategy.
    """
    try:
        all_events = client.get_events(status="open", with_nested_markets=True)
    except Exception as e:
        log.error(f"Failed to fetch events: {e}")
        return []

    log.info(f"  Fetched {len(all_events)} open events")

    # Enrich events with market_structure from our DB
    ms_lookup = _load_market_structure()
    for event in all_events:
        et = event.get("event_ticker", "")
        if et in ms_lookup and "market_structure" not in event:
            event["market_structure"] = ms_lookup[et]

    qualified = strategy.scan(all_events, traded_tickers=state.traded_tickers,
                              unpaired=unpaired)

    qualifying_events = len({p.event_ticker for p in qualified})
    n_paired = sum(1 for p in qualified if p.yes_opp is not p.no_opp)
    log.info(f"Qualified {len(qualified)} pairs from {qualifying_events} events "
             f"({n_paired} paired)")
    return qualified


# ─── Order placement ─────────────────────────────────────────────────

def _try_place_one(opp: Opportunity, client: KalshiClient, state: TradingState,
                    traded_event_sides: set, placed_tickers: set,
                    max_contracts: int, dry_run: bool,
                    pair_tag: str = "") -> OpenOrder | None:
    """Try to place a single order. Returns OpenOrder on success, None on skip/failure."""
    if opp.ticker in placed_tickers or opp.ticker in state.traded_tickers:
        return None

    event_side = (opp.event_ticker, opp.side)
    if event_side in traded_event_sides:
        return None

    if opp.bid_price >= opp.best_ask:
        return None

    contracts = optimal_quantity(opp.bid_price, max_q=max_contracts)
    cost_cents = opp.bid_price * contracts
    if state.available_cents < cost_cents:
        return None

    client_id = f"flb-{uuid.uuid4().hex[:12]}"
    label = f"[PAIR {pair_tag}] " if pair_tag else ""

    if dry_run:
        fee_cents = maker_fee(opp.bid_price, contracts)
        log.info(f"  [DRY RUN] {label}Would buy {opp.side.upper()} {opp.ticker} "
                 f"× {contracts} @ {opp.bid_price}¢ (fee={fee_cents}¢)  "
                 f"(edge={opp.edge*100:.1f}%, edge/day={opp.edge_per_day*100:.1f}%, "
                 f"spread={opp.spread}¢, "
                 f"settle={opp.days_to_settle:.1f}d, "
                 f"{opp.generating_process}/{opp.topic}, "
                 f"avail=${state.available_cents / 100:.2f})")
        traded_event_sides.add(event_side)
        placed_tickers.add(opp.ticker)
        dry_id = f"dry-{uuid.uuid4().hex[:8]}"
        oo = OpenOrder(
            order_id=dry_id,
            client_order_id=dry_id,
            ticker=opp.ticker,
            side=opp.side,
            price=opp.bid_price,
            contracts=contracts,
            placed_at=datetime.now(timezone.utc).isoformat(),
            event_ticker=opp.event_ticker,
        )
        state.open_orders[dry_id] = oo
        state.traded_tickers.add(opp.ticker)
        return oo

    try:
        if opp.side == 'yes':
            resp = client.create_order(
                ticker=opp.ticker, side='yes', action='buy',
                count=contracts, yes_price=opp.bid_price,
                post_only=True, client_order_id=client_id,
            )
        else:
            resp = client.create_order(
                ticker=opp.ticker, side='no', action='buy',
                count=contracts, no_price=opp.bid_price,
                post_only=True, client_order_id=client_id,
            )

        order = resp.get("order", {})
        order_id = order.get("order_id", "")

        oo = OpenOrder(
            order_id=order_id, client_order_id=client_id,
            ticker=opp.ticker, side=opp.side,
            price=opp.bid_price, contracts=contracts,
            placed_at=datetime.now(timezone.utc).isoformat(),
            event_ticker=opp.event_ticker,
        )
        state.open_orders[order_id] = oo
        state.traded_tickers.add(opp.ticker)
        traded_event_sides.add(event_side)
        placed_tickers.add(opp.ticker)

        log.info(f"  {label}PLACED: buy {opp.side.upper()} {opp.ticker} "
                 f"× {contracts} @ {opp.bid_price}¢  "
                 f"order={order_id}  "
                 f"(edge={opp.edge*100:.1f}%, edge/day={opp.edge_per_day*100:.1f}%, "
                 f"spread={opp.spread}¢, {opp.generating_process}/{opp.topic})")
        _log_order_to_db(oo)
        return oo

    except Exception as e:
        msg = str(e)
        if "post_only" in msg.lower() or "would cross" in msg.lower():
            log.info(f"  Post-only rejected {opp.ticker} @ {opp.bid_price}¢ "
                     f"(would cross spread)")
        else:
            log.error(f"  FAILED to place order on {opp.ticker}: {e}")
        return None


def place_pairs(client: KalshiClient, state: TradingState,
                pairs: list[TradePair],
                max_contracts: int = MAX_CONTRACTS,
                dry_run: bool = False,
                risk_limits: RiskLimits = DEFAULT_RISK_LIMITS) -> list[OpenOrder]:
    """Place paired orders atomically — both legs or neither.

    For each pair, checks risk limits, then that both legs can be
    afforded and placed. Skips the entire pair if any check fails.
    """
    placed = []
    traded_event_sides = set()
    for oid, o in state.open_orders.items():
        traded_event_sides.add((o.event_ticker, o.side))

    placed_tickers = set()

    # Pre-check: position count limit
    current_positions = len(state.open_orders) + len(state.positions)
    ok, reason = risk_limits.check_position_count(current_positions)
    if not ok:
        log.info(f"  Risk limit: {reason}")
        return placed

    for pair in pairs:
        is_true_pair = pair.yes_opp is not pair.no_opp
        legs = [pair.yes_opp, pair.no_opp] if is_true_pair else [pair.yes_opp]

        # Pre-check: can we afford all legs?
        total_cost = 0
        for opp in legs:
            if opp.ticker in placed_tickers or opp.ticker in state.traded_tickers:
                break
            if (opp.event_ticker, opp.side) in traded_event_sides:
                break
            if opp.bid_price >= opp.best_ask:
                break
            contracts = optimal_quantity(opp.bid_price, max_q=max_contracts)
            total_cost += opp.bid_price * contracts
        else:
            # All legs passed checks
            if state.available_cents < total_cost:
                continue  # can't afford this pair, try next

            # Risk limit: capital deployment
            ok, reason = risk_limits.check_deployment(
                state.balance_cents, state.capital_in_orders, total_cost)
            if not ok:
                log.info(f"  Risk limit ({pair.event_ticker}): {reason}")
                continue

            # Risk limit: per-event concentration
            event_exposure = sum(
                o.price * o.contracts for o in state.open_orders.values()
                if o.event_ticker == pair.event_ticker
            )
            ok, reason = risk_limits.check_event_concentration(
                state.balance_cents, event_exposure, total_cost)
            if not ok:
                log.info(f"  Risk limit ({pair.event_ticker}): {reason}")
                continue

            # Place all legs — abort pair if any leg fails
            pair_placed = []
            for i, opp in enumerate(legs):
                tag = "A" if is_true_pair and i == 0 else "B" if is_true_pair else ""
                if not dry_run and pair_placed:
                    time.sleep(0.25)  # rate limit: 250ms between API calls
                oo = _try_place_one(opp, client, state, traded_event_sides,
                                    placed_tickers, max_contracts, dry_run,
                                    pair_tag=tag)
                if oo:
                    pair_placed.append(oo)
                elif is_true_pair:
                    # Leg failed — cancel any already-placed legs in this pair
                    for prev_oo in pair_placed:
                        if not dry_run and not prev_oo.order_id.startswith("dry-"):
                            try:
                                client.cancel_order(prev_oo.order_id)
                                log.info(f"  Rolled back {prev_oo.side} {prev_oo.ticker} "
                                         f"(pair leg failed)")
                            except Exception as ce:
                                log.error(f"  Failed to cancel {prev_oo.order_id}: {ce}")
                        # Clean up state
                        state.open_orders.pop(prev_oo.order_id, None)
                        state.traded_tickers.discard(prev_oo.ticker)
                        placed_tickers.discard(prev_oo.ticker)
                        traded_event_sides.discard((prev_oo.event_ticker, prev_oo.side))
                    pair_placed.clear()
                    log.info(f"  Skipped pair {pair.event_ticker} (leg {i+1} failed)")
                    break
            placed.extend(pair_placed)
            continue

        # If we broke out of the for/else, skip this pair

    if placed:
        log.info(f"  Placed {len(placed)} orders")
    else:
        log.info(f"  No orders placed (available: {state.available_cents}¢)")
    return placed


# ─── EV Strategy scanning and order placement ────────────────────────

def scan_ev_opportunities(client: KalshiClient, state: TradingState,
                          strategy: EVStrategy) -> list[EVOpportunity]:
    """Fetch events from API, run EVStrategy to find positive-EV opportunities."""
    try:
        all_events = client.get_events(status="open", with_nested_markets=True)
    except Exception as e:
        log.error(f"Failed to fetch events: {e}")
        return []

    log.info(f"  Fetched {len(all_events)} open events")

    ms_lookup = _load_market_structure()
    for event in all_events:
        et = event.get("event_ticker", "")
        if et in ms_lookup and "market_structure" not in event:
            event["market_structure"] = ms_lookup[et]

    opps = strategy.scan(all_events, traded_tickers=state.traded_tickers)
    log.info(f"  {len(opps)} positive-EV opportunities")
    return opps


def place_ev_orders(client: KalshiClient, state: TradingState,
                    opportunities: list[EVOpportunity],
                    dry_run: bool = False,
                    risk_limits: RiskLimits = DEFAULT_RISK_LIMITS) -> list[OpenOrder]:
    """Place individual orders for EV opportunities (no pairing)."""
    placed = []
    traded_event_sides = set()
    for o in state.open_orders.values():
        traded_event_sides.add((o.event_ticker, o.side))
    placed_tickers = set()

    current_positions = len(state.open_orders) + len(state.positions)
    ok, reason = risk_limits.check_position_count(current_positions)
    if not ok:
        log.info(f"  Risk limit: {reason}")
        return placed

    for opp in opportunities:
        if opp.ticker in placed_tickers or opp.ticker in state.traded_tickers:
            continue
        event_side = (opp.event_ticker, opp.side)
        if event_side in traded_event_sides:
            continue

        cost_cents = opp.limit_price * opp.contracts
        if state.available_cents < cost_cents:
            continue

        ok, reason = risk_limits.check_deployment(
            state.balance_cents, state.capital_in_orders, cost_cents)
        if not ok:
            log.info(f"  Risk limit ({opp.event_ticker}): {reason}")
            continue

        event_exposure = sum(
            o.price * o.contracts for o in state.open_orders.values()
            if o.event_ticker == opp.event_ticker
        )
        ok, reason = risk_limits.check_event_concentration(
            state.balance_cents, event_exposure, cost_cents)
        if not ok:
            log.info(f"  Risk limit ({opp.event_ticker}): {reason}")
            continue

        # Place the order
        client_id = f"ev-{uuid.uuid4().hex[:12]}"

        if dry_run:
            fee_cents = maker_fee(opp.limit_price, opp.contracts)
            log.info(f"  [DRY RUN] Would buy {opp.side.upper()} {opp.ticker} "
                     f"× {opp.contracts} @ {opp.limit_price}¢ (fee={fee_cents}¢)  "
                     f"(EV={opp.ev_per_contract:.1f}¢, P={opp.p_event:.2f}, "
                     f"settle={opp.days_to_settle:.1f}d, "
                     f"{opp.generating_process}/{opp.topic})")
            dry_id = f"dry-{uuid.uuid4().hex[:8]}"
            oo = OpenOrder(
                order_id=dry_id, client_order_id=dry_id,
                ticker=opp.ticker, side=opp.side,
                price=opp.limit_price, contracts=opp.contracts,
                placed_at=datetime.now(timezone.utc).isoformat(),
                event_ticker=opp.event_ticker,
            )
            state.open_orders[dry_id] = oo
            state.traded_tickers.add(opp.ticker)
            placed.append(oo)
            traded_event_sides.add(event_side)
            placed_tickers.add(opp.ticker)
            continue

        try:
            if opp.side == 'yes':
                resp = client.create_order(
                    ticker=opp.ticker, side='yes', action='buy',
                    count=opp.contracts, yes_price=opp.limit_price,
                    post_only=True, client_order_id=client_id,
                )
            else:
                resp = client.create_order(
                    ticker=opp.ticker, side='no', action='buy',
                    count=opp.contracts, no_price=opp.limit_price,
                    post_only=True, client_order_id=client_id,
                )

            order = resp.get("order", {})
            order_id = order.get("order_id", "")
            oo = OpenOrder(
                order_id=order_id, client_order_id=client_id,
                ticker=opp.ticker, side=opp.side,
                price=opp.limit_price, contracts=opp.contracts,
                placed_at=datetime.now(timezone.utc).isoformat(),
                event_ticker=opp.event_ticker,
            )
            state.open_orders[order_id] = oo
            state.traded_tickers.add(opp.ticker)
            traded_event_sides.add(event_side)
            placed_tickers.add(opp.ticker)
            placed.append(oo)

            log.info(f"  PLACED: buy {opp.side.upper()} {opp.ticker} "
                     f"× {opp.contracts} @ {opp.limit_price}¢  "
                     f"order={order_id}  "
                     f"(EV={opp.ev_per_contract:.1f}¢, "
                     f"{opp.generating_process}/{opp.topic})")
            _log_order_to_db(oo, opp)
            time.sleep(0.25)

        except Exception as e:
            msg = str(e)
            if "post_only" in msg.lower() or "would cross" in msg.lower():
                log.info(f"  Post-only rejected {opp.ticker} @ {opp.limit_price}¢")
            else:
                log.error(f"  FAILED to place order on {opp.ticker}: {e}")

    if placed:
        log.info(f"  Placed {len(placed)} orders")
    else:
        log.info(f"  No orders placed (available: {state.available_cents}¢)")
    return placed


# ─── State management ────────────────────────────────────────────────

def _order_to_dict(o: OpenOrder) -> dict:
    return {
        "order_id": o.order_id,
        "client_order_id": o.client_order_id,
        "ticker": o.ticker,
        "side": o.side,
        "price": o.price,
        "contracts": o.contracts,
        "placed_at": o.placed_at,
        "event_ticker": o.event_ticker,
    }


def load_state(state_path: Path) -> TradingState:
    """Load trading state from disk."""
    if state_path.exists():
        with open(state_path) as f:
            data = json.load(f)
        state = TradingState(
            balance_cents=data.get("balance_cents", 0),
            traded_tickers=set(data.get("traded_tickers", [])),
        )
        for oid, od in data.get("open_orders", {}).items():
            state.open_orders[oid] = OpenOrder(**od)
        for ticker, od in data.get("filled_orders", {}).items():
            state.filled_orders[ticker] = OpenOrder(**od)
        return state
    return TradingState()


def save_state(state: TradingState, state_path: Path):
    """Persist trading state to disk."""
    data = {
        "balance_cents": state.balance_cents,
        "traded_tickers": list(state.traded_tickers),
        "open_orders": {oid: _order_to_dict(o) for oid, o in state.open_orders.items()},
        "filled_orders": {t: _order_to_dict(o) for t, o in state.filled_orders.items()},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(data, f, indent=2)


def sync_state(client: KalshiClient, state: TradingState):
    """Sync state with Kalshi: update balance, check order status."""
    # Update balance
    try:
        bal = client.get_balance()
        # API returns balance in cents already
        state.balance_cents = int(bal.get("balance", 0))
        log.info(f"Balance: ${state.balance_cents / 100:.2f}")
    except Exception as e:
        log.error(f"Failed to get balance: {e}")

    # Rebuild open_orders from Kalshi's actual resting orders.
    # This handles fresh starts, cancellations, and fills correctly.
    try:
        resting = client.get_orders(status="resting")
        new_orders = {}
        for o in resting:
            oid = o["order_id"]
            ticker = o.get("ticker", "")
            side = o.get("side", "")
            # Compute price in cents
            if side == "yes":
                price_str = o.get("yes_price_dollars", "0")
            else:
                price_str = o.get("no_price_dollars", "0")
            price = int(round(float(price_str) * 100)) if price_str else 0
            count = int(round(float(o.get("remaining_count_fp", "0"))))

            new_orders[oid] = OpenOrder(
                order_id=oid,
                client_order_id=o.get("client_order_id", ""),
                ticker=ticker,
                side=side,
                price=price,
                contracts=count,
                placed_at=o.get("created_time", ""),
                event_ticker="-".join(ticker.split("-")[:2]) if ticker else "",
            )
        # Log changes and preserve entry prices for filled orders
        old_ids = set(state.open_orders.keys())
        new_ids = set(new_orders.keys())
        for oid in old_ids - new_ids:
            oo = state.open_orders[oid]
            log.info(f"Order no longer resting: {oo.side} {oo.ticker} @ {oo.price}¢")
            # Preserve entry price in filled_orders for P&L tracking
            if oo.ticker not in state.filled_orders:
                state.filled_orders[oo.ticker] = oo
        for oid in new_ids - old_ids:
            oo = new_orders[oid]
            log.info(f"Found resting order: {oo.side} {oo.ticker} @ {oo.price}¢")
        state.open_orders = new_orders
    except Exception as e:
        log.error(f"Failed to sync orders: {e}")

    # Rebuild traded_tickers from live positions and resting orders.
    # Start fresh each cycle so canceled/expired orders don't permanently block.
    fresh_tickers = set()
    try:
        positions = client.get_positions()
        for p in positions:
            ticker = p.get('ticker', '')
            qty = float(p.get('position_fp', '0'))
            if abs(qty) > 0 and ticker:
                fresh_tickers.add(ticker)
    except Exception as e:
        log.error(f"Failed to sync positions: {e}")

    try:
        resting = client.get_orders(status="resting")
        for o in resting:
            ticker = o.get('ticker', '')
            if ticker:
                fresh_tickers.add(ticker)
    except Exception:
        pass

    stale = state.traded_tickers - fresh_tickers
    if stale:
        log.info(f"Cleared {len(stale)} stale traded_tickers")
    state.traded_tickers = fresh_tickers

    log.info(f"Open orders: {len(state.open_orders)}, "
             f"Capital in orders: ${state.capital_in_orders / 100:.2f}, "
             f"Available: ${state.available_cents / 100:.2f}, "
             f"Tracked tickers: {len(state.traded_tickers)}")

    # Record portfolio fills to database for fill model calibration
    _sync_portfolio_fills(client)

    # Update order log statuses (resting → filled/cancelled/expired)
    _sync_order_statuses(client)

    # Poll queue positions for resting orders
    _poll_queue_positions(client, state)


def _sync_portfolio_fills(client: KalshiClient):
    """Pull recent fills from Kalshi and store in kalshi_portfolio_fills.

    Idempotent — skips fills already in the database.
    Runs on each sync cycle; fast because most fills are already stored.
    """
    try:
        import psycopg2
        from psycopg2.extras import execute_values

        dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
        if not dsn:
            return
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()

        fills = []
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/portfolio/fills", params)
            batch = resp.get('fills', [])
            fills.extend(batch)
            cursor = resp.get('cursor')
            if not cursor or not batch:
                break

        if not fills:
            conn.close()
            return

        buffer = []
        for f in fills:
            fill_id = f.get('fill_id', '')
            if not fill_id:
                continue
            buffer.append((
                fill_id,
                f.get('order_id', ''),
                f.get('trade_id', ''),
                f.get('ticker', f.get('market_ticker', '')),
                f.get('side', ''),
                f.get('action', ''),
                f.get('count_fp', f.get('count', '0')),
                f.get('yes_price_dollars', f.get('yes_price_fixed', '0')),
                f.get('no_price_dollars', f.get('no_price_fixed', '0')),
                f.get('fee_cost', '0'),
                f.get('is_taker', False),
                f.get('created_time', ''),
            ))

        if buffer:
            execute_values(cur, """
                INSERT INTO prediction_markets.kalshi_portfolio_fills
                    (fill_id, order_id, trade_id, ticker, side, action,
                     count, yes_price, no_price, fee_cost, is_taker, created_time)
                VALUES %s
                ON CONFLICT (fill_id) DO NOTHING
            """, buffer, page_size=200)
            inserted = cur.rowcount
            conn.commit()
            if inserted > 0:
                log.info(f"Recorded {inserted} new portfolio fills")

        conn.close()
    except Exception as e:
        log.debug(f"Portfolio fill sync: {e}")


# ─── Order logging (DB) ───────────────────────────────────────────────


def _log_order_to_db(oo: OpenOrder, opp=None):
    """Persist order placement to kalshi_order_log.

    Records the order with market state at placement time for fill model
    training. Called once per order at placement. Status updates (fills,
    cancellations) are handled by _sync_order_statuses.

    Args:
        oo: The OpenOrder just created.
        opp: EVOpportunity (if EV strategy) — carries market state fields.
    """
    try:
        dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
        if not dsn:
            return
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO prediction_markets.kalshi_order_log
                (order_id, client_order_id, ticker, event_ticker, side,
                 price_cents, quantity, yes_bid, yes_ask, spread,
                 volume, open_interest, placed_at, status,
                 edge_estimate, ev_estimate, generating_process, topic)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
        """, (
            oo.order_id, oo.client_order_id, oo.ticker, oo.event_ticker,
            oo.side, oo.price, oo.contracts,
            getattr(opp, 'yes_bid', None),
            getattr(opp, 'yes_ask', None),
            (getattr(opp, 'yes_ask', 0) - getattr(opp, 'yes_bid', 0))
            if opp and getattr(opp, 'yes_ask', 0) else None,
            getattr(opp, 'volume', None),
            getattr(opp, 'open_interest', None),
            oo.placed_at,
            'resting',
            getattr(opp, 'p_fill', None),  # closest to "edge estimate"
            getattr(opp, 'ev_per_contract', None),
            getattr(opp, 'generating_process', None),
            getattr(opp, 'topic', None),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"Order log write: {e}")


def _sync_order_statuses(client: KalshiClient):
    """Update order_log statuses from Kalshi API and portfolio_fills.

    Finds orders in 'resting' status, checks if they're still resting,
    and updates to filled/cancelled/expired based on API state and fills.
    """
    try:
        dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
        if not dsn:
            return
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()

        # Get all orders we think are resting
        cur.execute("""
            SELECT order_id FROM prediction_markets.kalshi_order_log
            WHERE status = 'resting'
        """)
        resting_ids = {row[0] for row in cur}
        if not resting_ids:
            conn.close()
            return

        # Get actually resting orders from Kalshi
        actual_resting = {o['order_id'] for o in client.get_orders(status='resting')}

        # Orders no longer resting — determine final status
        resolved = resting_ids - actual_resting
        if not resolved:
            conn.close()
            return

        for oid in resolved:
            # Check if it was filled (has entries in portfolio_fills)
            cur.execute("""
                SELECT SUM(count), MIN(created_time)
                FROM prediction_markets.kalshi_portfolio_fills
                WHERE order_id = %s
            """, (oid,))
            row = cur.fetchone()
            filled_qty = int(float(row[0])) if row and row[0] else 0
            first_fill_time = row[1] if row else None

            cur.execute("""
                SELECT quantity FROM prediction_markets.kalshi_order_log
                WHERE order_id = %s
            """, (oid,))
            orig_qty = cur.fetchone()
            orig_qty = orig_qty[0] if orig_qty else 0

            if filled_qty >= orig_qty:
                status = 'filled'
            elif filled_qty > 0:
                status = 'partial'
            else:
                status = 'cancelled'

            cur.execute("""
                UPDATE prediction_markets.kalshi_order_log
                SET status = %s, filled_quantity = %s, filled_at = %s
                WHERE order_id = %s
            """, (status, filled_qty, first_fill_time, oid))

        conn.commit()
        conn.close()
        if resolved:
            log.info(f"Updated {len(resolved)} order statuses in order_log")
    except Exception as e:
        log.debug(f"Order status sync: {e}")


def _poll_queue_positions(client: KalshiClient, state: TradingState):
    """Poll and record queue positions for all resting orders.

    Calls GET /portfolio/orders/queue_positions for each event with
    resting orders, stores in kalshi_queue_positions table.
    """
    if not state.open_orders:
        return
    try:
        dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
        if not dsn:
            return
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()

        # Group tickers by event for efficient API calls
        tickers = [oo.ticker for oo in state.open_orders.values()
                   if not oo.order_id.startswith("dry-")]
        if not tickers:
            conn.close()
            return

        qp = client.get_queue_positions(tickers=tickers)
        now = datetime.now(timezone.utc)

        if qp:
            buffer = []
            for order_id, position in qp.items():
                buffer.append((order_id, now, int(round(position))))
            if buffer:
                from psycopg2.extras import execute_values
                execute_values(cur, """
                    INSERT INTO prediction_markets.kalshi_queue_positions
                        (order_id, observed_at, queue_position)
                    VALUES %s
                    ON CONFLICT (order_id, observed_at) DO NOTHING
                """, buffer, page_size=200)
                conn.commit()
                log.info(f"Recorded {len(buffer)} queue positions")

        conn.close()
    except Exception as e:
        log.debug(f"Queue position poll: {e}")


# ─── Settlement monitoring ────────────────────────────────────────────

@dataclass
class SettlementResult:
    ticker: str
    side: str
    entry_price: int
    contracts: int
    result: str        # 'yes' or 'no'
    pnl_cents: int     # total P&L in cents


def check_settlements(client: KalshiClient, state: TradingState,
                      trade_log_path: Path) -> list[SettlementResult]:
    """Check for settled positions and compute P&L.

    Looks at all tickers we've traded, checks if the market has settled,
    and logs the outcome.
    """
    settlements = []

    # Get all positions to find ones with non-zero qty
    try:
        positions = client.get_positions()
    except Exception as e:
        log.error(f"Failed to get positions: {e}")
        return []

    # Find positions with actual holdings (check ALL positions,
    # not just ones in traded_tickers — state may have been cleared)
    active_positions = {}
    for p in positions:
        ticker = p.get('ticker', '')
        qty_str = p.get('position_fp', '0')
        qty = float(qty_str) if qty_str else 0
        if abs(qty) > 0:
            active_positions[ticker] = {
                'qty': qty,
                'exposure': p.get('market_exposure_dollars', '0'),
            }

    if not active_positions:
        return []

    # Check each active position for settlement
    for ticker, pos_info in active_positions.items():
        try:
            m = client.get_market(ticker)
            market = m.get('market', m)
        except Exception as e:
            log.debug(f"Failed to get market {ticker}: {e}")
            continue

        status = market.get('status', '')
        result = market.get('result', '')

        if status not in ('settled', 'finalized') or not result:
            continue

        # Market has settled! Compute P&L.
        qty = pos_info['qty']
        # Determine our side and entry price from state
        # qty > 0 means YES position, qty < 0 means NO position
        if qty > 0:
            side = 'yes'
            won = (result == 'yes')
        else:
            side = 'no'
            won = (result == 'no')

        contracts = int(abs(qty))

        # Find entry price: check filled_orders first, then open_orders, then estimate
        entry_price = 0
        if ticker in state.filled_orders:
            entry_price = state.filled_orders[ticker].price
        else:
            for oo in list(state.open_orders.values()):
                if oo.ticker == ticker:
                    entry_price = oo.price
                    break

        if entry_price == 0:
            # Last resort: estimate from exposure
            exp = float(pos_info['exposure'])
            if contracts > 0:
                entry_price = int(exp * 100 / contracts)

        # Compute P&L including maker fee
        fee_cents = maker_fee(entry_price, contracts)

        if won:
            pnl = (100 - entry_price) * contracts - fee_cents
        else:
            pnl = -entry_price * contracts - fee_cents

        sr = SettlementResult(
            ticker=ticker,
            side=side,
            entry_price=entry_price,
            contracts=contracts,
            result=result,
            pnl_cents=pnl,
        )
        settlements.append(sr)

        outcome = "WIN" if won else "LOSS"
        log.info(f"  SETTLED: {outcome} {side.upper()} {ticker} "
                 f"× {contracts} @ {entry_price}¢ → {result.upper()} "
                 f"P&L={pnl:+d}¢ ({pnl/100:+.2f}$)")

        # Log to trade file
        log_trade("settlement", {
            "ticker": ticker,
            "side": side,
            "entry_price": entry_price,
            "contracts": contracts,
            "result": result,
            "won": won,
            "fee_cents": fee_cents,
            "pnl_cents": pnl,
        }, trade_log_path)

        # Clean up state for settled ticker
        state.traded_tickers.discard(ticker)
        state.filled_orders.pop(ticker, None)

    if settlements:
        total_pnl = sum(s.pnl_cents for s in settlements)
        wins = sum(1 for s in settlements if s.pnl_cents > 0)
        log.info(f"  Settlement summary: {wins}/{len(settlements)} wins, "
                 f"P&L={total_pnl:+d}¢ ({total_pnl/100:+.2f}$)")

    return settlements


# ─── Trade log ────────────────────────────────────────────────────────

def log_trade(action: str, details: dict, log_path: Path):
    """Append a trade event to the JSONL log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Main loop ────────────────────────────────────────────────────────

RUNNING = True

def signal_handler(_sig, _frame):
    global RUNNING
    log.info("Shutdown requested...")
    RUNNING = False


def main():
    parser = argparse.ArgumentParser(description="EV-maximizing Kalshi trading system")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print trades without placing orders")
    parser.add_argument("--demo", action="store_true",
                        help="Use Kalshi demo environment")
    parser.add_argument("--max-contracts", type=int, default=MAX_CONTRACTS,
                        help="Maximum contracts per order (sizing is fee-optimized within this)")
    parser.add_argument("--interval", type=int, default=SCAN_INTERVAL_SECONDS,
                        help="Seconds between scans")
    parser.add_argument("--once", action="store_true",
                        help="Run one scan cycle and exit")
    parser.add_argument("--legacy", action="store_true",
                        help="Use FLB paired strategy (old system) instead of EV strategy")
    parser.add_argument("--unpaired", action="store_true",
                        help="Allow unpaired single-leg trades (legacy mode only)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize client (loads credentials from ~/.config/kalshi/)
    try:
        client = KalshiClient(demo=args.demo)
    except FileNotFoundError:
        log.error("Kalshi credentials not found in ~/.config/kalshi/")
        log.error("  Expected: ~/.config/kalshi/key_id and ~/.config/kalshi/private_key.pem")
        log.error("  Generate an API key at https://kalshi.com/account/api-keys")
        sys.exit(1)
    if args.dry_run:
        state_path = LOG_DIR / "state-dry-run.json"
    elif args.demo:
        state_path = LOG_DIR / "state-demo.json"
    else:
        state_path = LOG_DIR / "state.json"
    if args.dry_run:
        trade_log_path = LOG_DIR / "trades-dry-run.jsonl"
    elif args.demo:
        trade_log_path = LOG_DIR / "trades-demo.jsonl"
    else:
        trade_log_path = LOG_DIR / "trades.jsonl"
    if args.dry_run and state_path.exists():
        state_path.unlink()  # fresh state each dry run
    state = load_state(state_path)

    env_label = "DEMO" if args.demo else "PRODUCTION"
    mode_label = "DRY RUN" if args.dry_run else "LIVE"
    use_ev = not args.legacy
    strat_label = "EV" if use_ev else "FLB legacy"
    log.info(f"Trader starting ({env_label}, {mode_label}, {strat_label})")
    log.info(f"Max contracts/order: {args.max_contracts} (fee-optimized sizing)")

    # Load trading parameters from DB (single source of truth)
    params = load_trading_params()
    log.info(f"Params: spread≤{params.max_spread}¢, "
             f"max_days={params.max_days_to_settle}")

    # Build strategy
    if use_ev:
        conn = psycopg2.connect(_get_pg_dsn())
        log.info("Building View from database...")
        view_factory = build_view_factory_from_db(conn)
        view = view_factory.build_live()
        log.info(f"  {view.stats}")
        strategy = EVStrategy(view, params=params)
        conn.close()
        last_view_date = datetime.now(timezone.utc).date()
    else:
        edge_lookup = EdgeLookup()
        strategy = FLBStrategy(edge_lookup, params)
        last_view_date = None
    risk_limits = DEFAULT_RISK_LIMITS

    # Drawdown and alpha decay monitors
    drawdown = DrawdownMonitor(max_loss_cents=5000)  # $50 kill switch
    drawdown.load_from_log(trade_log_path)
    alpha = AlphaDecayMonitor(window_size=50)
    alpha.load_from_log(trade_log_path)

    # Sync with Kalshi — always get real balance
    sync_state(client, state)
    if args.dry_run:
        log.info(f"[DRY RUN] Using real balance: ${state.balance_cents / 100:.2f}")

    cycle = 0
    while RUNNING:
        cycle += 1
        log.info(f"\n{'='*50}")
        log.info(f"SCAN CYCLE {cycle}")
        log.info(f"{'='*50}")

        # Rebuild View daily (same cadence as expanding-window replay)
        if use_ev and last_view_date != datetime.now(timezone.utc).date():
            conn = psycopg2.connect(_get_pg_dsn())
            log.info("Rebuilding View (daily recalibration)...")
            view_factory = build_view_factory_from_db(conn)
            view = view_factory.build_live()
            log.info(f"  {view.stats}")
            strategy = EVStrategy(view, params=params)
            conn.close()
            last_view_date = datetime.now(timezone.utc).date()

        # Sync state
        sync_state(client, state)

        # Check for settlements
        if not args.dry_run:
            settlements = check_settlements(client, state, trade_log_path)
            for s in (settlements or []):
                drawdown.update(s.pnl_cents)
                # Note: edge_estimate not available at settlement time.
                # Alpha decay monitor is populated from log at startup
                # (when edge_estimate is logged with order_placed records).
                # Don't call alpha.record(0, ...) here — it would corrupt
                # the rolling average with zero-edge entries.
            if settlements:
                # Re-sync balance after settlements freed capital
                sync_state(client, state)

        # Risk checks: drawdown kill switch
        stop, reason = drawdown.should_stop()
        if stop:
            log.warning(f"DRAWDOWN KILL SWITCH: {reason}")
            break

        # Risk checks: alpha decay (check but don't stop — just warn)
        pause, reason = alpha.should_pause()
        if pause:
            log.warning(f"ALPHA DECAY WARNING: {reason}")

        # Scan for opportunities
        if use_ev:
            log.info("Scanning for EV opportunities...")
            opps = scan_ev_opportunities(client, state, strategy)

            if opps:
                log.info("\nTop opportunities (by EV):")
                for i, o in enumerate(opps[:10]):
                    log.info(f"  {i+1:>2}. {o.side.upper():>3} {o.ticker[:35]:<35} "
                             f"{o.limit_price}¢ × {o.contracts}  "
                             f"EV={o.ev_per_contract:.1f}¢ ({o.ev_per_day:.1f}¢/d)  "
                             f"P={o.p_event:.2f}  "
                             f"settle={o.days_to_settle:.1f}d  "
                             f"{o.generating_process}/{o.topic}")

                placed = place_ev_orders(client, state, opps,
                                         dry_run=args.dry_run,
                                         risk_limits=risk_limits)

                # Build edge lookup for logging
                ev_by_ticker = {o.ticker: o.ev_per_contract / 100.0 for o in opps}

                for oo in placed:
                    log_trade("order_placed", {
                        "order_id": oo.order_id,
                        "ticker": oo.ticker,
                        "side": oo.side,
                        "price": oo.price,
                        "contracts": oo.contracts,
                        "edge_estimate": ev_by_ticker.get(oo.ticker, 0),
                    }, trade_log_path)
        else:
            log.info("Scanning for tail pairs (legacy)...")
            pairs = scan_for_pairs(client, state, strategy,
                                   unpaired=args.unpaired)

            if pairs:
                log.info("\nQualified pairs (randomized):")
                for i, pair in enumerate(pairs[:10]):
                    is_true = pair.yes_opp is not pair.no_opp
                    y, n = pair.yes_opp, pair.no_opp
                    if is_true:
                        log.info(f"  {i+1:>2}. YES {y.ticker[:35]:<35} {y.bid_price}¢ "
                                 f"+ NO {n.ticker[:35]:<35} {n.bid_price}¢  "
                                 f"edge/d={pair.edge_per_day*100:.1f}% "
                                 f"settle={y.days_to_settle:.1f}d "
                                 f"{pair.generating_process}/{pair.topic}")
                    else:
                        log.info(f"  {i+1:>2}. {y.side.upper():>3} {y.ticker[:35]:<35} "
                                 f"{y.bid_price}¢  "
                                 f"edge/d={pair.edge_per_day*100:.1f}% "
                                 f"settle={y.days_to_settle:.1f}d "
                                 f"{pair.generating_process}/{pair.topic}")

                placed = place_pairs(client, state, pairs,
                                     max_contracts=args.max_contracts,
                                     dry_run=args.dry_run,
                                     risk_limits=risk_limits)

                edge_by_ticker = {}
                for p in pairs:
                    edge_by_ticker[p.yes_opp.ticker] = p.yes_opp.edge
                    edge_by_ticker[p.no_opp.ticker] = p.no_opp.edge

                for oo in placed:
                    log_trade("order_placed", {
                        "order_id": oo.order_id,
                        "ticker": oo.ticker,
                        "side": oo.side,
                        "price": oo.price,
                        "contracts": oo.contracts,
                        "edge_estimate": edge_by_ticker.get(oo.ticker, 0),
                    }, trade_log_path)

        # Save state
        save_state(state, state_path)

        if args.once:
            break

        log.info(f"\nSleeping {args.interval}s until next scan...")
        for _ in range(args.interval):
            if not RUNNING:
                break
            time.sleep(1)

    log.info("Trader stopped.")
    save_state(state, state_path)


if __name__ == "__main__":
    main()
