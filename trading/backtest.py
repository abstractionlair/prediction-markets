#!/usr/bin/env python3
"""
Backtest: FLB tail-buying strategy on monotone threshold chains.

Strategy:
  For each chain, identify tail strikes (YES priced 85-97¢ or NO priced 85-97¢).
  Place resting bids at the mid-price. Fill simulated via candlestick yes_ask_low.
  Hold to settlement. Compute P&L net of maker fees.

Uses the Bayesian PIT posterior Beta(α,β) to estimate theoretical edge.

Usage:
    python backtest.py
    python backtest.py --min-tail 85 --max-tail 97
"""

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy.special import betainc
import psycopg2

from trading.strategy import maker_fee, identify_tails, parse_strike_value, TradingParams, DEFAULT_PARAMS, BLOCKED_SERIES
from trading.cost_model import KALSHI_COSTS
from trading.track_record import TradeRecord, TrackRecord


# ─── Configuration ──────────────────────────────────────────────────

# PIT posterior from Bayesian analysis (well-covered chains, structural detection)
PIT_ALPHA = 1.45
PIT_BETA = 1.42

# Per-series edge from empirical tail calibration.
# Computed as (actual_win_rate - implied_win_rate) at tail prices.
# Positive = FLB (tails overpriced, buying tails is +EV); negative = reverse.
#
# The live path loads measured per-series edges from the `calibration_edges`
# database table at runtime. The dict below is an ILLUSTRATIVE EXAMPLE of the
# shape only — the values are synthetic placeholders, not measured results —
# so the backtest module is self-contained without shipping proprietary edge
# estimates. Populate from your own calibration to run it against real data.
SERIES_EDGE_PCT = {
    # <SERIES_TICKER>: <edge_pct>   (example values, replace with your own)
    'EXAMPLE_SERIES_POSITIVE': 1.5,
    'EXAMPLE_SERIES_NEUTRAL': 0.2,
    'EXAMPLE_SERIES_NEGATIVE': -1.0,
}

# Minimum empirical edge (%) to take a trade. Series with edge below
# this threshold are skipped. Series not in the table use the global model.
MIN_SERIES_EDGE_PCT = 0.5

# Fee calculation: imported from strategy.py (canonical source)
MAKER_FEE_RATE = DEFAULT_PARAMS.maker_fee_rate  # for local references


def theoretical_edge(yes_price_cents: int) -> float:
    """Expected edge (in cents) from buying YES at this price.

    Uses the PIT Beta(α,β) model. For a YES contract at price p:
      CDF position F = 1 - p
      True P(YES) = 1 - I_F(α, β)   where I is regularized incomplete beta
      Edge = True P(YES) * 100 - p   (in cents)
    """
    p = yes_price_cents / 100.0
    F = 1.0 - p
    true_p_yes = 1.0 - betainc(PIT_ALPHA, PIT_BETA, F)
    return true_p_yes * 100 - yes_price_cents


def theoretical_edge_no(yes_price_cents: int) -> float:
    """Expected edge (in cents) from buying NO at (100 - yes_price).

    For buying NO when YES is priced at p:
      CDF position F = 1 - p = position of this strike in the CDF
      True P(NO) = I_F(α, β)
      NO price = 100 - p
      Edge = True P(NO) * 100 - (100 - p)
    """
    p = yes_price_cents / 100.0
    F = 1.0 - p
    true_p_no = betainc(PIT_ALPHA, PIT_BETA, F)
    no_price = 100 - yes_price_cents
    return true_p_no * 100 - no_price


# ─── Data structures ────────────────────────────────────────────────

@dataclass
class Strike:
    ticker: str
    strike_value: float
    result: str  # 'yes' or 'no'


@dataclass
class Candle:
    ticker: str
    date: str
    yes_bid_high: int  # cents
    yes_bid_low: int
    yes_ask_high: int
    yes_ask_low: int
    volume: int
    open_interest: int


@dataclass
class Trade:
    event: str
    ticker: str
    side: str        # 'buy_yes' or 'buy_no'
    entry_price: int  # cents, what we pay
    entry_date: str
    settlement: str   # 'yes' or 'no'
    payout: int       # 100 if won, 0 if lost
    fee: int          # maker fee in cents
    contracts: int
    days_to_settlement: int
    theoretical_edge: float  # cents

    @property
    def pnl(self) -> int:
        """P&L per contract in cents."""
        return self.payout - self.entry_price - self.fee

    @property
    def pnl_total(self) -> int:
        return self.pnl * self.contracts

    @property
    def capital_deployed(self) -> int:
        """Capital tied up per contract in cents (what Kalshi holds)."""
        return self.entry_price

    @property
    def capital_days(self) -> float:
        """Capital-days per contract: entry_price * days held."""
        days = max(self.days_to_settlement, 1)  # at least 1 day
        return self.capital_deployed * days

    @property
    def return_on_capital(self) -> float:
        """Simple return per trade."""
        if self.entry_price == 0:
            return 0.0
        return self.pnl / self.entry_price

    @property
    def annualized_return(self) -> float:
        """Annualized return, compounded from per-trade return and holding period."""
        days = max(self.days_to_settlement, 1)
        r = self.return_on_capital
        if r <= -1.0:
            return -1.0
        return (1 + r) ** (365 / days) - 1


# ─── Database ────────────────────────────────────────────────────────

def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


# parse_strike_value imported from strategy.py


def load_events(conn, min_coverage=0.5, min_strikes=3):
    """Load settled threshold chain events with candlestick coverage.

    Uses structural detection: events with 3+ markets, both yes/no
    outcomes, and monotone resolution pattern. Does NOT rely on the
    market_structure tag.
    """
    cur = conn.cursor()

    cur.execute("""
        WITH chain_events AS (
            SELECT sm.event_ticker,
                   COUNT(DISTINCT sm.ticker) as total_strikes,
                   COUNT(DISTINCT sm.ticker) FILTER (WHERE result = 'yes') as n_yes,
                   COUNT(DISTINCT sm.ticker) FILTER (WHERE result = 'no') as n_no
            FROM kalshi_settled_markets sm
            WHERE sm.result IN ('yes', 'no')
            GROUP BY sm.event_ticker
            HAVING COUNT(DISTINCT sm.ticker) >= %s
               AND COUNT(DISTINCT sm.ticker) FILTER (WHERE result = 'yes') > 0
               AND COUNT(DISTINCT sm.ticker) FILTER (WHERE result = 'no') > 0
        ),
        with_candles AS (
            SELECT ce.event_ticker, ce.total_strikes,
                   COUNT(DISTINCT kc.ticker) as n_with_candles
            FROM chain_events ce
            JOIN kalshi_settled_markets sm ON sm.event_ticker = ce.event_ticker
            LEFT JOIN (SELECT DISTINCT ticker FROM kalshi_candlesticks) kc
                ON kc.ticker = sm.ticker
            GROUP BY ce.event_ticker, ce.total_strikes
            HAVING COUNT(DISTINCT kc.ticker) >= GREATEST(ce.total_strikes * %s, 3)
        )
        SELECT event_ticker FROM with_candles
    """, (min_strikes, min_coverage))

    return [row[0] for row in cur.fetchall()]


def load_chain(conn, event_ticker):
    """Load strikes and results for an event."""
    cur = conn.cursor()
    cur.execute("""
        SELECT ticker, result, settled_at
        FROM kalshi_settled_markets
        WHERE event_ticker = %s AND result IN ('yes', 'no')
    """, (event_ticker,))

    strikes = []
    settled_at = None
    for ticker, result, sa in cur.fetchall():
        sv = parse_strike_value(ticker)
        if sv is not None:
            strikes.append(Strike(ticker=ticker, strike_value=sv, result=result))
            if sa:
                settled_at = sa

    strikes.sort(key=lambda s: s.strike_value)
    return strikes, settled_at


def load_candles(conn, tickers):
    """Load all candles for a list of tickers, keyed by (ticker, date)."""
    if not tickers:
        return {}
    cur = conn.cursor()

    # Use ANY array for efficient lookup
    cur.execute("""
        SELECT ticker, period_end::date::text, yes_bid_high, yes_bid_low,
               yes_ask_high, yes_ask_low, volume, open_interest
        FROM kalshi_candlesticks
        WHERE ticker = ANY(%s)
          AND yes_bid_high IS NOT NULL
          AND yes_ask_low IS NOT NULL
    """, (tickers,))

    candles = {}
    for row in cur.fetchall():
        ticker, date, bid_h, bid_l, ask_h, ask_l, vol, oi = row
        candles[(ticker, date)] = Candle(
            ticker=ticker, date=date,
            yes_bid_high=bid_h or 0, yes_bid_low=bid_l or 0,
            yes_ask_high=ask_h or 0, yes_ask_low=ask_l or 0,
            volume=vol or 0, open_interest=oi or 0,
        )

    return candles


# ─── Backtest logic ─────────────────────────────────────────────────

def find_tail_opportunities(strikes, candles, date, min_tail=85, max_tail=97,
                            max_spread=15):
    """Find tail opportunities in the chain for a given date.

    Returns list of (strike, side, mid_price, spread, edge) tuples.
    """
    opportunities = []

    for s in strikes:
        c = candles.get((s.ticker, date))
        if c is None or c.yes_bid_high <= 0 or c.yes_ask_low <= 0:
            continue

        # Skip invalid quotes
        if c.yes_ask_low < c.yes_bid_high:
            continue  # ask below bid = stale/invalid

        spread = c.yes_ask_low - c.yes_bid_high
        if spread > max_spread:
            continue  # ghost quote

        mid = (c.yes_bid_high + c.yes_ask_low) // 2

        # Check YES tail: high YES price (deep ITM)
        if min_tail <= mid <= max_tail:
            edge = theoretical_edge(mid)
            if edge > 0:
                opportunities.append((s, 'buy_yes', mid, spread, edge))

        # Check NO tail: low YES price means high NO price
        no_mid = 100 - mid
        if min_tail <= no_mid <= max_tail:
            edge = theoretical_edge_no(mid)
            if edge > 0:
                opportunities.append((s, 'buy_no', no_mid, spread, edge))

    return opportunities


def simulate_fill(candle, side, bid_price):
    """Simulate whether a resting bid would fill during this candle.

    For buy_yes at price X: fill if yes_ask_low <= X (someone sold YES to us)
    For buy_no at price X: NO ask_low = 100 - yes_bid_high.
        fill if no_ask_low <= X, i.e., 100 - yes_bid_high <= X,
        i.e., yes_bid_high >= 100 - X.

    Returns True if filled.
    """
    if side == 'buy_yes':
        return candle.yes_ask_low <= bid_price
    else:  # buy_no
        # NO ask = 100 - YES bid. Fill if NO ask comes down to our price.
        # no_ask_low = 100 - yes_bid_high
        no_ask_low = 100 - candle.yes_bid_high
        return no_ask_low <= bid_price


def parse_settled_at(settled_at) -> datetime | None:
    """Parse settled_at to a timezone-aware datetime."""
    if not settled_at:
        return None
    try:
        if isinstance(settled_at, str):
            return datetime.fromisoformat(settled_at.replace('Z', '+00:00'))
        dt = settled_at
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def run_backtest(conn, min_tail=85, max_tail=97, max_spread=15,
                 contracts_per_trade=5, entry_window_days=7,
                 allow_unknown_series=False):
    """Run the backtest across ALL settled markets with candlestick data.

    Screens every market for tail pricing — not limited to threshold chains.
    Any contract priced in the tail zone with a tight spread is a candidate.

    allow_unknown_series: if False (default), only trade series with known
        empirical edge >= MIN_SERIES_EDGE_PCT. If True, also trade unknown
        series using the global PIT model.
    """
    cur = conn.cursor()

    # Load all settled markets that have candlestick data
    print("Loading all settled markets with candlestick data...")
    cur.execute("""
        SELECT sm.ticker, sm.event_ticker, sm.result, sm.settled_at,
               kc.period_end::date::text as candle_date,
               kc.yes_bid_high, kc.yes_bid_low,
               kc.yes_ask_high, kc.yes_ask_low,
               kc.volume, kc.open_interest
        FROM kalshi_settled_markets sm
        JOIN kalshi_candlesticks kc ON kc.ticker = sm.ticker
        WHERE sm.result IN ('yes', 'no')
          AND kc.yes_bid_high IS NOT NULL
          AND kc.yes_ask_low IS NOT NULL
          AND kc.yes_bid_high > 0
          AND kc.yes_ask_low > 0
          AND kc.yes_ask_low >= kc.yes_bid_high
        ORDER BY sm.ticker, kc.period_end
    """)
    rows = cur.fetchall()
    print(f"  Candle rows loaded: {len(rows)}")

    # Group by ticker
    market_data = defaultdict(lambda: {'result': None, 'event': None,
                                       'settled_at': None, 'candles': []})
    for (ticker, event, result, settled_at,
         cdate, bid_h, bid_l, ask_h, ask_l, vol, oi) in rows:
        md = market_data[ticker]
        md['result'] = result
        md['event'] = event
        md['settled_at'] = settled_at
        md['candles'].append(Candle(
            ticker=ticker, date=cdate,
            yes_bid_high=bid_h or 0, yes_bid_low=bid_l or 0,
            yes_ask_high=ask_h or 0, yes_ask_low=ask_l or 0,
            volume=vol or 0, open_interest=oi or 0,
        ))

    print(f"  Unique markets: {len(market_data)}")

    all_trades = []
    markets_screened = 0
    markets_with_tail = 0
    markets_with_fill = 0
    skipped_series_edge = 0

    for ticker, md in market_data.items():
        markets_screened += 1
        result = md['result']
        event = md['event']
        series = ticker.split('-')[0]
        settled_dt = parse_settled_at(md['settled_at'])

        # Per-series edge filter
        # Conservative: only trade series where we have empirical edge data
        # and the edge exceeds our minimum threshold.
        series_edge = SERIES_EDGE_PCT.get(series)
        if series_edge is None:
            # Unknown series — skip unless --allow-unknown flag
            if not allow_unknown_series:
                skipped_series_edge += 1
                continue
        elif series_edge < MIN_SERIES_EDGE_PCT:
            skipped_series_edge += 1
            continue

        found_tail = False
        traded = False

        for candle in md['candles']:
            spread = candle.yes_ask_low - candle.yes_bid_high
            if spread > max_spread:
                continue

            mid = (candle.yes_bid_high + candle.yes_ask_low) // 2

            # Compute days to settlement
            days_to_settle = None
            if settled_dt:
                try:
                    cdate_dt = datetime.fromisoformat(
                        candle.date + 'T12:00:00+00:00')
                    days_to_settle = (settled_dt - cdate_dt).total_seconds() / 86400
                except Exception:
                    pass

            if days_to_settle is not None and days_to_settle > entry_window_days:
                continue
            if days_to_settle is not None and days_to_settle < 0:
                continue

            # Check both sides
            for side, tail_price, edge_fn in [
                ('buy_yes', mid, theoretical_edge),
                ('buy_no', 100 - mid, lambda p: theoretical_edge_no(p)),
            ]:
                if side == 'buy_no':
                    edge = theoretical_edge_no(mid)
                else:
                    edge = theoretical_edge(mid)

                # Override with per-series empirical edge if available
                if series_edge is not None:
                    edge = series_edge * tail_price / 100.0  # scale by price level

                if not (min_tail <= tail_price <= max_tail):
                    continue
                if edge <= 0:
                    continue

                found_tail = True
                bid = tail_price

                # Simulate fill
                filled = simulate_fill(candle, side, bid)
                if not filled:
                    continue

                # Filled
                if side == 'buy_yes':
                    payout = 100 if result == 'yes' else 0
                else:
                    payout = 100 if result == 'no' else 0

                fee = maker_fee(bid, contracts_per_trade)
                dtts = int(days_to_settle) if days_to_settle is not None else -1

                trade = Trade(
                    event=event,
                    ticker=ticker,
                    side=side,
                    entry_price=bid,
                    entry_date=candle.date,
                    settlement=result,
                    payout=payout,
                    fee=fee // contracts_per_trade,
                    contracts=contracts_per_trade,
                    days_to_settlement=dtts,
                    theoretical_edge=edge,
                )
                all_trades.append(trade)
                traded = True
                break  # one trade per side per market, take first fill

            if traded:
                break  # done with this market once we have a trade

        if found_tail:
            markets_with_tail += 1
        if traded:
            markets_with_fill += 1

    n_events = len(set(t.event for t in all_trades))
    print(f"  Markets screened:   {markets_screened}")
    print(f"  Skipped (series):   {skipped_series_edge}")
    print(f"  Markets with tail:  {markets_with_tail}")
    print(f"  Markets with fill:  {markets_with_fill}")
    print(f"  Events with trades: {n_events}")
    return all_trades, markets_screened, n_events


# ─── Calibration-based backtest (production-aligned) ──────────────

def run_simulation(conn, edge_lookup, params: TradingParams = DEFAULT_PARAMS,
                   starting_capital_cents: int = 10000,
                   contracts_per_trade: int = 0,  # 0 = use optimal_quantity
                   entry_window_days: int = 7) -> TrackRecord:
    """Full strategy simulation with capital constraints and time.

    Replays chronologically through hourly candle data:
    - At each time step, group active markets by event
    - Apply full strategy: tail detection, chain pairing, edge lookup,
      ranking by edge/day, top-N event selection
    - Enforce capital limits (80% deployment, 15% per-event)
    - Track resting orders, fills, settlements, and available capital
    - One trade per market (no re-entry after fill or skip)

    This is the closest we can get to simulating the live trader on
    historical data.
    """
    from trading.risk import RiskLimits
    from trading.strategy import detect_chain, optimal_quantity, rank_and_select_pairs

    costs = KALSHI_COSTS
    risk = RiskLimits()
    max_spread = params.max_spread

    # ── Load data ─────────────────────────────────────────────────
    cur = conn.cursor()

    print("Loading settled markets...")
    cur.execute("""
        SELECT ticker, event_ticker, result, settled_at
        FROM prediction_markets.kalshi_settled_markets
        WHERE result IN ('yes', 'no') AND settled_at != '' AND event_ticker != ''
    """)
    settled = {}
    for ticker, event, result, settled_at in cur:
        sdt = parse_settled_at(settled_at)
        if sdt is None:
            continue
        settled[ticker] = {'event': event, 'result': result, 'settled_at': sdt}
    print(f"  Settled markets: {len(settled)}")

    # Load hourly candles for settled markets
    # Need: by-period index (for scanning) and by-ticker index (for fill sim)
    from trading.fill_model import FillModel, CandleData
    fill_model = FillModel(capture_rate=0.20)

    print("Loading hourly candles...")
    cur2 = conn.cursor("sim_hourly")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT hc.ticker, hc.period_end,
               (hc.yes_bid_high * 100)::int AS bid_high,
               (hc.yes_ask_low * 100)::int AS ask_low,
               COALESCE(hc.volume, 0) AS volume
        FROM prediction_markets.kalshi_hourly_candles hc
        WHERE hc.yes_bid_high IS NOT NULL AND hc.yes_ask_low IS NOT NULL
          AND hc.yes_bid_high > 0 AND hc.yes_ask_low > 0
        ORDER BY hc.period_end
    """)

    from itertools import groupby
    from operator import itemgetter

    # Build two indexes:
    # 1. candle_rows: [(period_end, ticker, bid_h, ask_l)] for scanning (spread-filtered)
    # 2. ticker_candles: {ticker: [(period_end, CandleData)]} for fill simulation (all candles)
    print("Building indexes...")
    candle_rows = []
    ticker_candles = defaultdict(list)  # ticker -> [(period_end, CandleData)]
    for ticker, period_end, bid_h, ask_l, vol in cur2:
        if ticker not in settled:
            continue
        cd = CandleData(yes_bid_high=bid_h, yes_ask_low=ask_l, volume=vol)
        ticker_candles[ticker].append((period_end, cd))
        # Only include tight-spread candles for opportunity scanning
        if (ask_l - bid_h) <= max_spread:
            candle_rows.append((period_end, ticker, bid_h, ask_l))
    cur2.close()
    print(f"  Scan observations: {len(candle_rows)} (spread ≤ {max_spread}¢)")
    print(f"  Fill sim tickers: {len(ticker_candles)}")

    candle_rows.sort(key=itemgetter(0))

    # ── Simulation state ──────────────────────────────────────────
    # Capital model: total_equity = cash + escrow
    # cash decreases on placement, increases on settlement payout
    # escrow tracks capital locked in orders (both pending and filled)
    # total_equity only changes from P&L (settlement gains/losses + fees)

    cash = starting_capital_cents
    escrow = 0                        # capital locked in all orders
    # pending_orders: placed but not yet fully filled
    # filled_orders: fully filled, waiting for settlement
    pending_orders = {}    # ticker -> {side, price, contracts_requested, contracts_filled, ...}
    filled_orders = {}     # ticker -> {side, price, contracts, ...}
    active_tickers = set() # tickers with pending or filled orders (no re-entry)
    track = TrackRecord()

    n_periods = 0
    n_orders_placed = 0
    n_settlements = 0
    n_risk_blocked = 0

    # ── Process each time step ────────────────────────────────────

    for period_end, candles_in_period in groupby(candle_rows, key=itemgetter(0)):
        candles_list = list(candles_in_period)
        n_periods += 1

        # ── 1. Check settlements ──────────────────────────────────
        # Settle filled orders whose market has resolved
        for ticker in list(filled_orders.keys()):
            md = settled[ticker]
            if md['settled_at'] <= period_end:
                order = filled_orders.pop(ticker)
                result = md['result']
                won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
                exit_price = 100 if won else 0
                fee = costs.maker_fee(order['price'], order['contracts'])
                payout = exit_price * order['contracts']
                order_cost = order['price'] * order['contracts']
                escrow -= order_cost
                cash += payout - fee

                track.add(TradeRecord(
                    ticker=ticker, side=order['side'],
                    entry_price=order['price'], contracts=order['contracts'],
                    exit_price=exit_price, fee_cents=fee,
                    days_held=(md['settled_at'] - order['placed_at']).total_seconds() / 86400,
                    edge_estimate=order['edge'],
                    event_ticker=order['event'],
                    series=ticker.split('-')[0],
                    generating_process=order.get('gp', ''),
                    topic=order.get('topic', ''),
                ))
                n_settlements += 1

        # Cancel pending orders for settled markets (unfilled portion returns escrow)
        for ticker in list(pending_orders.keys()):
            md = settled[ticker]
            if md['settled_at'] <= period_end:
                order = pending_orders.pop(ticker)
                # Return unfilled escrow
                unfilled = order['contracts_requested'] - order['contracts_filled']
                unfilled_cost = order['price'] * unfilled
                escrow -= unfilled_cost
                cash += unfilled_cost
                # Settle the filled portion
                if order['contracts_filled'] > 0:
                    result = md['result']
                    won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
                    exit_price = 100 if won else 0
                    filled_cost = order['price'] * order['contracts_filled']
                    fee = costs.maker_fee(order['price'], order['contracts_filled'])
                    payout = exit_price * order['contracts_filled']
                    escrow -= filled_cost
                    cash += payout - fee

                    track.add(TradeRecord(
                        ticker=ticker, side=order['side'],
                        entry_price=order['price'], contracts=order['contracts_filled'],
                        exit_price=exit_price, fee_cents=fee,
                        days_held=(md['settled_at'] - order['placed_at']).total_seconds() / 86400,
                        edge_estimate=order['edge'],
                        event_ticker=order['event'],
                        series=ticker.split('-')[0],
                        generating_process=order.get('gp', ''),
                        topic=order.get('topic', ''),
                    ))
                    n_settlements += 1

        # ── 2. Process fills on pending orders ────────────────────
        for ticker in list(pending_orders.keys()):
            order = pending_orders[ticker]
            # Get this period's candle for this ticker
            candles_for_ticker = ticker_candles.get(ticker, [])
            # Find the candle matching this period
            for pe, cd in candles_for_ticker:
                if pe != period_end:
                    continue
                remaining = order['contracts_requested'] - order['contracts_filled']
                if fill_model._price_touched(order['side'], order['price'], cd):
                    fillable = fill_model._fillable_contracts(cd.volume, remaining)
                    order['contracts_filled'] += fillable
                    if order['contracts_filled'] >= order['contracts_requested']:
                        # Fully filled — move to filled_orders
                        filled_orders[ticker] = {
                            k: v for k, v in order.items()
                            if k != 'contracts_requested' and k != 'contracts_filled'
                        }
                        filled_orders[ticker]['contracts'] = order['contracts_requested']
                        del pending_orders[ticker]
                break

        # ── 3. Scan for new opportunities ─────────────────────────
        # Group this period's candles by event
        event_markets = defaultdict(list)
        for _, ticker, bid_h, ask_l in candles_list:
            if ticker in active_tickers:
                continue
            md = settled.get(ticker)
            if md is None:
                continue
            event_markets[md['event']].append((ticker, bid_h, ask_l))

        # Build opportunities
        from trading.strategy import Opportunity, TradePair
        opportunities = []
        event_is_chain = {}

        for event_ticker, markets in event_markets.items():
            series = event_ticker.split('-')[0]
            if series in BLOCKED_SERIES:
                continue

            classification = edge_lookup.get_classification(series)
            if classification is None:
                continue
            gp, topic = classification

            # Chain detection
            if event_ticker not in event_is_chain:
                tickers = [t for t, _, _ in markets]
                event_is_chain[event_ticker] = detect_chain(tickers)

            for ticker, bid_h, ask_l in markets:
                md = settled[ticker]
                hours = (md['settled_at'] - period_end).total_seconds() / 3600
                if hours < 0 or hours > entry_window_days * 24:
                    continue

                edge = edge_lookup.get_edge(series, hours)
                if edge is None or edge < params.min_edge:
                    continue

                tails = identify_tails(bid_h, ask_l, params)
                for tail in tails:
                    from trading.strategy import edge_per_day as compute_epd
                    epd = compute_epd(edge, hours / 24)
                    opportunities.append(Opportunity(
                        ticker=ticker, event_ticker=event_ticker, series=series,
                        side=tail.side, bid_price=tail.mid,
                        best_bid=tail.best_bid, best_ask=tail.best_ask,
                        spread=tail.spread, edge=edge,
                        days_to_settle=hours / 24, edge_per_day=epd,
                        generating_process=gp, topic=topic,
                    ))

        # ── 4. Pair and rank ──────────────────────────────────────
        # Build pairs from chains only
        event_sides = defaultdict(lambda: {'yes': [], 'no': []})
        for o in opportunities:
            event_sides[o.event_ticker][o.side].append(o)

        pairs = []
        for ev, sides in event_sides.items():
            if sides['yes'] and sides['no'] and event_is_chain.get(ev, False):
                best_yes = min(sides['yes'], key=lambda o: o.spread)
                best_no = min(sides['no'], key=lambda o: o.spread)
                avg_epd = (best_yes.edge_per_day + best_no.edge_per_day) / 2
                pairs.append(TradePair(yes_opp=best_yes, no_opp=best_no,
                                       edge_per_day=avg_epd, is_chain=True))

        qualified = rank_and_select_pairs(pairs, max_events=params.max_qualifying_events)

        # ── 5. Place orders with capital/risk limits ──────────────
        total_equity = cash + escrow  # total portfolio value

        for pair in qualified:
            legs = [pair.yes_opp, pair.no_opp]
            total_cost = 0
            leg_details = []

            for opp in legs:
                if opp.ticker in active_tickers:
                    break
                q = optimal_quantity(opp.bid_price, max_q=params.max_contracts) if contracts_per_trade == 0 else contracts_per_trade
                cost = opp.bid_price * q
                total_cost += cost
                leg_details.append((opp, q, cost))
            else:
                # All legs passed — check risk limits
                if cash < total_cost:
                    continue  # can't afford

                # Deployment check: escrow / total_equity
                ok, _ = risk.check_deployment(total_equity, escrow, total_cost)
                if not ok:
                    n_risk_blocked += 1
                    continue

                # Event concentration: event_escrow / total_equity
                # Event exposure from both pending and filled orders
                event_exp = sum(
                    o['price'] * o.get('contracts', o.get('contracts_requested', 0))
                    for o in list(pending_orders.values()) + list(filled_orders.values())
                    if o['event'] == pair.event_ticker
                )
                ok, _ = risk.check_event_concentration(total_equity, event_exp, total_cost)
                if not ok:
                    n_risk_blocked += 1
                    continue

                # Place both legs as pending orders
                for opp, q, cost in leg_details:
                    pending_orders[opp.ticker] = {
                        'side': opp.side, 'price': opp.bid_price,
                        'contracts_requested': q, 'contracts_filled': 0,
                        'event': opp.event_ticker,
                        'edge': opp.edge, 'placed_at': period_end,
                        'gp': opp.generating_process, 'topic': opp.topic,
                    }
                    cash -= cost  # escrow the full amount upfront
                    escrow += cost
                    active_tickers.add(opp.ticker)
                    n_orders_placed += 1

        if n_periods % 1000 == 0:
            equity = cash + escrow
            print(f"  Period {n_periods}: {period_end.date()} | "
                  f"orders={n_orders_placed} settlements={n_settlements} "
                  f"equity=${equity/100:.0f} pending={len(pending_orders)} filled={len(filled_orders)}")

    # ── Settle remaining orders (filled + partial pending) ─────────
    for ticker, order in list(filled_orders.items()):
        md = settled.get(ticker)
        if md is None:
            continue
        contracts = order['contracts']
        result = md['result']
        won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
        exit_price = 100 if won else 0
        fee = costs.maker_fee(order['price'], contracts)
        payout = exit_price * contracts
        escrow -= order['price'] * contracts
        cash += payout - fee
        track.add(TradeRecord(
            ticker=ticker, side=order['side'],
            entry_price=order['price'], contracts=contracts,
            exit_price=exit_price, fee_cents=fee,
            days_held=(md['settled_at'] - order['placed_at']).total_seconds() / 86400,
            edge_estimate=order['edge'],
            event_ticker=order['event'],
            series=ticker.split('-')[0],
            generating_process=order.get('gp', ''),
            topic=order.get('topic', ''),
        ))
        n_settlements += 1

    # Settle partial fills from pending orders
    n_partial = 0
    for ticker, order in list(pending_orders.items()):
        md = settled.get(ticker)
        if md is None:
            continue
        filled = order['contracts_filled']
        requested = order['contracts_requested']
        unfilled = requested - filled
        # Return unfilled escrow
        escrow -= order['price'] * unfilled
        cash += order['price'] * unfilled
        # Settle filled portion
        if filled > 0:
            result = md['result']
            won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
            exit_price = 100 if won else 0
            fee = costs.maker_fee(order['price'], filled)
            payout = exit_price * filled
            escrow -= order['price'] * filled
            cash += payout - fee
            track.add(TradeRecord(
                ticker=ticker, side=order['side'],
                entry_price=order['price'], contracts=filled,
                exit_price=exit_price, fee_cents=fee,
                days_held=(md['settled_at'] - order['placed_at']).total_seconds() / 86400,
                edge_estimate=order['edge'],
                event_ticker=order['event'],
                series=ticker.split('-')[0],
                generating_process=order.get('gp', ''),
                topic=order.get('topic', ''),
            ))
            n_settlements += 1
            n_partial += 1

    final_equity = cash + escrow
    print("\n  Simulation complete:")
    print(f"    Periods processed: {n_periods}")
    print(f"    Orders placed:     {n_orders_placed}")
    print(f"    Settlements:       {n_settlements} ({n_partial} partial fills)")
    print(f"    Risk-blocked:      {n_risk_blocked}")
    print(f"    Starting capital:  ${starting_capital_cents/100:.2f}")
    print(f"    Final equity:      ${final_equity/100:.2f}")
    print(f"    Return:            {(final_equity - starting_capital_cents) / starting_capital_cents:.1%}")
    if escrow != 0:
        print(f"    WARNING: Unsettled escrow: ${escrow/100:.2f}")
    return track


def run_backtest_calibration(conn, edge_lookup, params: TradingParams = DEFAULT_PARAMS,
                              contracts_per_trade: int = 5,
                              entry_window_days: int = 7) -> TrackRecord:
    """Backtest using merged candle data and the same edge source as live.

    Loads hourly + daily candles for all settled markets, merges them
    (one entry per ticker per period, hourly preferred), and simulates
    fills using candle bid/ask high/low — same logic as the legacy
    backtest but with much more data and precise hours-to-settlement
    for edge lookup.

    Uses EdgeLookup from calibration_edges DB table — same edge source
    as production.
    """
    cur = conn.cursor()
    costs = KALSHI_COSTS
    max_spread_cents = params.max_spread

    # Load settled markets
    print("Loading settled markets...")
    cur.execute("""
        SELECT ticker, event_ticker, result, settled_at
        FROM prediction_markets.kalshi_settled_markets
        WHERE result IN ('yes', 'no') AND settled_at != ''
    """)
    settled = {}
    for ticker, event, result, settled_at in cur:
        settled[ticker] = {'event': event, 'result': result, 'settled_at': settled_at}
    print(f"  Settled markets: {len(settled)}")

    # Load hourly candles for settled markets (bulk — uses server-side cursor)
    print("Loading hourly candles...")
    cur2 = conn.cursor("bt_hourly")
    cur2.itersize = 50000
    cur2.execute("""
        SELECT hc.ticker, hc.period_end,
               (hc.yes_bid_high * 100)::int AS bid_high,
               (hc.yes_bid_low * 100)::int AS bid_low,
               (hc.yes_ask_high * 100)::int AS ask_high,
               (hc.yes_ask_low * 100)::int AS ask_low,
               hc.volume
        FROM prediction_markets.kalshi_hourly_candles hc
        WHERE hc.yes_bid_high IS NOT NULL AND hc.yes_ask_low IS NOT NULL
          AND hc.yes_bid_high > 0 AND hc.yes_ask_low > 0
    """)

    # Group candles by ticker
    market_candles = defaultdict(list)  # ticker -> [(period_end, bid_h, bid_l, ask_h, ask_l, vol)]
    hourly_count = 0
    for ticker, period_end, bid_h, bid_l, ask_h, ask_l, vol in cur2:
        if ticker not in settled:
            continue
        market_candles[ticker].append((period_end, bid_h, bid_l, ask_h, ask_l, vol or 0))
        hourly_count += 1
    cur2.close()
    print(f"  Hourly candle rows: {hourly_count} across {len(market_candles)} markets")

    # Load daily candles for markets that don't have hourly data
    hourly_tickers = set(market_candles.keys())
    print("Loading daily candles (gap-fill)...")
    cur.execute("""
        SELECT ticker, period_end,
               yes_bid_high, yes_bid_low, yes_ask_high, yes_ask_low, volume
        FROM prediction_markets.kalshi_candlesticks
        WHERE yes_bid_high IS NOT NULL AND yes_ask_low IS NOT NULL
          AND yes_bid_high > 0 AND yes_ask_low > 0
    """)
    daily_count = 0
    daily_tickers = 0
    for ticker, period_end, bid_h, bid_l, ask_h, ask_l, vol in cur:
        if ticker not in settled:
            continue
        if ticker in hourly_tickers:
            continue  # prefer hourly data
        market_candles[ticker].append((period_end, bid_h, bid_l, ask_h, ask_l, vol or 0))
        daily_count += 1
    daily_tickers = len(market_candles) - len(hourly_tickers)
    print(f"  Daily candle rows: {daily_count} across {daily_tickers} gap-fill markets")
    print(f"  Total markets with candle data: {len(market_candles)}")

    # Run backtest
    track = TrackRecord()
    markets_screened = 0
    markets_with_tail = 0
    markets_with_fill = 0
    skipped_edge = 0

    for ticker, candles in market_candles.items():
        markets_screened += 1
        md = settled[ticker]
        result = md['result']
        event = md['event']
        series = ticker.split('-')[0]
        settled_dt = parse_settled_at(md['settled_at'])

        if series in BLOCKED_SERIES:
            skipped_edge += 1
            continue

        classification = edge_lookup.get_classification(series)
        if classification is None:
            skipped_edge += 1
            continue
        gp, topic = classification

        found_tail = False
        traded = False

        for period_end, bid_h, bid_l, ask_h, ask_l, vol in candles:
            # Spread check using ask_low - bid_high (tightest spread in candle)
            spread = ask_l - bid_h
            if spread > max_spread_cents or spread < 0:
                continue

            # Hours to settlement
            if settled_dt is None:
                continue
            try:
                if isinstance(period_end, str):
                    pe = datetime.fromisoformat(period_end.replace('Z', '+00:00'))
                else:
                    pe = period_end
                    if pe.tzinfo is None:
                        pe = pe.replace(tzinfo=timezone.utc)
                hours_to_settle = (settled_dt - pe).total_seconds() / 3600
            except Exception:
                continue

            if hours_to_settle < 0 or hours_to_settle > entry_window_days * 24:
                continue

            # Edge lookup with precise hours
            edge = edge_lookup.get_edge(series, hours_to_settle)
            if edge is None or edge < params.min_edge:
                continue

            # Tail detection using bid_high / ask_low (tightest quote in candle)
            tails = identify_tails(bid_h, ask_l, params)
            if not tails:
                continue

            for tail in tails:
                found_tail = True
                bid = tail.mid

                # Simulate fill using candle high/low
                candle_obj = Candle(
                    ticker=ticker, date='',
                    yes_bid_high=bid_h, yes_bid_low=bid_l,
                    yes_ask_high=ask_h, yes_ask_low=ask_l,
                    volume=vol, open_interest=0,
                )
                side_str = 'buy_yes' if tail.side == 'yes' else 'buy_no'
                if not simulate_fill(candle_obj, side_str, bid):
                    continue

                # Filled — compute P&L
                won = (result == 'yes') if tail.side == 'yes' else (result == 'no')
                fee = costs.maker_fee(bid, contracts_per_trade)
                exit_price = 100 if won else 0
                days_held = hours_to_settle / 24.0

                track.add(TradeRecord(
                    ticker=ticker,
                    side=tail.side,
                    entry_price=bid,
                    contracts=contracts_per_trade,
                    exit_price=exit_price,
                    fee_cents=fee,
                    days_held=days_held,
                    edge_estimate=edge,
                    event_ticker=event,
                    series=series,
                    generating_process=gp,
                    topic=topic,
                ))
                traded = True
                break  # one fill per market per candle

            if traded:
                break  # one trade per market total

        if found_tail:
            markets_with_tail += 1
        if traded:
            markets_with_fill += 1

    print(f"  Markets screened:   {markets_screened}")
    print(f"  Skipped (edge/class): {skipped_edge}")
    print(f"  Markets with tail:  {markets_with_tail}")
    print(f"  Markets with fill:  {markets_with_fill}")
    print(f"  Trades:             {len(track)}")
    return track


# ─── Reporting ───────────────────────────────────────────────────────

def print_edge_table():
    """Print theoretical edge by price level from the PIT posterior."""
    print("THEORETICAL EDGE FROM PIT POSTERIOR Beta(%.2f, %.2f)" %
          (PIT_ALPHA, PIT_BETA))
    print("=" * 70)
    print(f"  {'YES price':>10} {'True P(YES)':>12} {'Edge':>8} {'Fee(5c)':>8} "
          f"{'Net edge':>10} {'NO price':>10} {'Edge(NO)':>10}")
    print(f"  {'-'*68}")

    for p in [3, 5, 7, 10, 15, 85, 90, 93, 95, 97]:
        F = 1.0 - p / 100.0
        true_yes = 1.0 - betainc(PIT_ALPHA, PIT_BETA, F)
        edge_yes = theoretical_edge(p)
        edge_no = theoretical_edge_no(p)
        fee = maker_fee(p, 5) / 5  # per contract for 5-lot
        net = edge_yes - fee

        print(f"  {p:>9}¢ {true_yes:>11.3%} {edge_yes:>7.2f}¢ {fee:>7.2f}¢ "
              f"{net:>9.2f}¢ {100-p:>9}¢ {edge_no:>9.2f}¢")


def print_results(trades, markets_screened, events_with_trades):
    """Print backtest results."""
    if not trades:
        print("\nNo trades generated.")
        return

    print(f"\n{'='*70}")
    print("BACKTEST RESULTS")
    print(f"{'='*70}")
    print(f"  Markets screened: {markets_screened}")
    print(f"  Events with trades: {events_with_trades}")
    print(f"  Total trades: {len(trades)}")

    # Separate by side
    yes_trades = [t for t in trades if t.side == 'buy_yes']
    no_trades = [t for t in trades if t.side == 'buy_no']

    for label, subset in [("ALL", trades), ("Buy YES", yes_trades), ("Buy NO", no_trades)]:
        if not subset:
            continue
        wins = sum(1 for t in subset if t.pnl > 0)
        total_pnl = sum(t.pnl for t in subset)
        avg_entry = np.mean([t.entry_price for t in subset])
        avg_edge = np.mean([t.theoretical_edge for t in subset])
        np.mean([t.fee for t in subset])
        avg_pnl = np.mean([t.pnl for t in subset])
        avg_days = np.mean([max(t.days_to_settlement, 1) for t in subset])

        # Capital efficiency
        total_capital_days = sum(t.capital_days for t in subset)
        pnl_per_capital_day = total_pnl / total_capital_days if total_capital_days > 0 else 0
        # Annualize: if we earn X per capital-day, annual return = X * 365
        annual_return_on_capital = pnl_per_capital_day * 365
        avg_return = np.mean([t.return_on_capital for t in subset])

        print(f"\n  [{label}]  n={len(subset)}")
        print(f"    Win rate:     {wins}/{len(subset)} = {wins/len(subset):.1%}")
        print(f"    Avg entry:    {avg_entry:.1f}¢")
        print(f"    Avg P&L:      {avg_pnl:+.2f}¢/contract")
        print(f"    Total P&L:    {total_pnl:+d}¢ ({total_pnl/100:+.2f}$)")
        print(f"    Theo. edge:   {avg_edge:.2f}¢/contract")
        print(f"    Avg days:     {avg_days:.1f}")
        print(f"    Avg return:   {avg_return:+.2%} per trade")
        print(f"    Capital-days: {total_capital_days:.0f}¢·days")
        print(f"    P&L / cap-day: {pnl_per_capital_day:+.4f}¢/¢·day")
        print(f"    Annualized:   {annual_return_on_capital:+.1%} (on deployed capital)")

    # By series (aggregate)
    by_series = defaultdict(list)
    for t in trades:
        series = t.event.split('-')[0]
        by_series[series].append(t)

    print(f"\n  {'Series':>25} {'Trd':>4} {'Win':>4} {'P&L':>7} {'Entry':>6} {'Days':>5} {'Ret':>7}")
    print(f"  {'-'*62}")
    for series in sorted(by_series.keys(), key=lambda s: -len(by_series[s])):
        st = by_series[series]
        wins = sum(1 for t in st if t.pnl > 0)
        total = sum(t.pnl for t in st)
        avg_e = np.mean([t.entry_price for t in st])
        avg_d = np.mean([max(t.days_to_settlement, 1) for t in st])
        avg_r = np.mean([t.return_on_capital for t in st])
        print(f"  {series:>25} {len(st):>4} {wins:>4} {total:>+6d}¢ {avg_e:>5.0f}¢ {avg_d:>5.1f} {avg_r:>+6.1%}")

    # Top events by trade count
    by_event = defaultdict(list)
    for t in trades:
        by_event[t.event].append(t)
    print("\n  Top events (by trade count):")
    print(f"  {'Event':>40} {'Trd':>4} {'Win':>4} {'P&L':>7} {'Entry':>6} {'Days':>5}")
    print(f"  {'-'*65}")
    for event in sorted(by_event.keys(), key=lambda e: -len(by_event[e]))[:25]:
        et = by_event[event]
        wins = sum(1 for t in et if t.pnl > 0)
        total = sum(t.pnl for t in et)
        avg_e = np.mean([t.entry_price for t in et])
        avg_d = np.mean([max(t.days_to_settlement, 1) for t in et])
        print(f"  {event:>40} {len(et):>4} {wins:>4} {total:>+6d}¢ {avg_e:>5.0f}¢ {avg_d:>5.1f}")

    # Expected vs actual win rate
    sum(
        (1.0 - betainc(PIT_ALPHA, PIT_BETA, 1.0 - t.entry_price / 100.0))
        if t.side == 'buy_yes'
        else betainc(PIT_ALPHA, PIT_BETA, 1.0 - t.entry_price / 100.0)  # wrong: need F for the strike
        for t in trades
    )
    # Simpler: for buying at price p in the tail, expected win rate ≈ true_prob
    expected_wins2 = 0
    for t in trades:
        p = t.entry_price / 100.0
        if t.side == 'buy_yes':
            F = 1.0 - p
            win_prob = 1.0 - betainc(PIT_ALPHA, PIT_BETA, F)
        else:
            # Buying NO at price (100-yes_mid). YES price = 100 - entry_price
            yes_p = (100 - t.entry_price) / 100.0
            F = 1.0 - yes_p
            win_prob = betainc(PIT_ALPHA, PIT_BETA, F)
        expected_wins2 += win_prob
    actual_wins = sum(1 for t in trades if t.pnl > 0)
    print(f"\n  Expected wins (PIT model): {expected_wins2:.1f}/{len(trades)}"
          f" = {expected_wins2/len(trades):.1%}")
    print(f"  Actual wins:              {actual_wins}/{len(trades)}"
          f" = {actual_wins/len(trades):.1%}")

    # Break-even analysis: what win rate makes this profitable?
    avg_entry = np.mean([t.entry_price for t in trades])
    win_payoff = 100 - avg_entry  # average gain per win
    loss_payoff = avg_entry       # average loss per loss
    breakeven_wr = loss_payoff / (win_payoff + loss_payoff)
    print(f"\n  Break-even win rate at avg entry {avg_entry:.0f}¢:"
          f" {breakeven_wr:.1%}")
    print(f"  (Win={100-avg_entry:.0f}¢, Loss={avg_entry:.0f}¢)")

    # Monte Carlo: given the PIT model, what's the distribution of
    # outcomes for this many trades?
    len(trades)
    win_probs = []
    for t in trades:
        p = t.entry_price / 100.0
        if t.side == 'buy_yes':
            F = 1.0 - p
            win_probs.append(1.0 - betainc(PIT_ALPHA, PIT_BETA, F))
        else:
            yes_p = (100 - t.entry_price) / 100.0
            F = 1.0 - yes_p
            win_probs.append(betainc(PIT_ALPHA, PIT_BETA, F))

    rng = np.random.default_rng(42)
    n_sims = 50000
    sim_pnls = []
    for _ in range(n_sims):
        pnl = 0
        for t, wp in zip(trades, win_probs):
            if rng.random() < wp:
                pnl += (100 - t.entry_price)
            else:
                pnl -= t.entry_price
        sim_pnls.append(pnl)
    sim_pnls = np.array(sim_pnls)
    print(f"\n  Monte Carlo ({n_sims:,} sims, same trade set, PIT win probs):")
    print(f"    Mean P&L:   {sim_pnls.mean():+.0f}¢ ({sim_pnls.mean()/100:+.2f}$)")
    print(f"    Median P&L: {np.median(sim_pnls):+.0f}¢")
    print(f"    P(profit):  {(sim_pnls > 0).mean():.1%}")
    print(f"    5th pctile: {np.percentile(sim_pnls, 5):+.0f}¢")
    print(f"    95th pctile:{np.percentile(sim_pnls, 95):+.0f}¢")
    print(f"    Actual P&L: {sum(t.pnl for t in trades):+d}¢"
          f"  (percentile: {(sim_pnls <= sum(t.pnl for t in trades)).mean():.0%})")

    # Individual trades (losses + sample of wins)
    losses = [t for t in trades if t.pnl < 0]
    wins_sample = [t for t in trades if t.pnl >= 0][:20]
    print(f"\n  LOSSES ({len(losses)}):")
    print(f"  {'Ticker':>40} {'Side':>8} {'Entry':>6} {'Pay':>4} "
          f"{'P&L':>6} {'Edge':>6} {'Days':>5} {'WinP':>5}")
    print(f"  {'-'*80}")
    for t in sorted(losses, key=lambda x: x.pnl):
        p = t.entry_price / 100.0
        if t.side == 'buy_yes':
            F = 1.0 - p
            wp = 1.0 - betainc(PIT_ALPHA, PIT_BETA, F)
        else:
            yes_p = (100 - t.entry_price) / 100.0
            F = 1.0 - yes_p
            wp = betainc(PIT_ALPHA, PIT_BETA, F)
        print(f"  {t.ticker:>40} {t.side:>8} {t.entry_price:>5}¢ {t.payout:>3}¢ "
              f"{t.pnl:>+5}¢ {t.theoretical_edge:>5.1f}¢ "
              f"{t.days_to_settlement:>5} {wp:>4.0%}")
    if len(wins_sample) < len(trades) - len(losses):
        print(f"\n  WINS (showing 20 of {len(trades) - len(losses)}):")
    else:
        print(f"\n  WINS ({len(trades) - len(losses)}):")
    print(f"  {'Ticker':>40} {'Side':>8} {'Entry':>6} "
          f"{'P&L':>6} {'Edge':>6} {'Days':>5}")
    print(f"  {'-'*72}")
    for t in sorted(wins_sample, key=lambda x: -x.pnl)[:20]:
        print(f"  {t.ticker:>40} {t.side:>8} {t.entry_price:>5}¢ "
              f"{t.pnl:>+5}¢ {t.theoretical_edge:>5.1f}¢ "
              f"{t.days_to_settlement:>5}")


def main():
    parser = argparse.ArgumentParser(
        description="Backtest FLB tail-buying strategy on settled Kalshi markets.",
    )
    parser.add_argument('--min-tail', type=int, default=85,
                        help='Minimum price (cents) for tail zone')
    parser.add_argument('--max-tail', type=int, default=97,
                        help='Maximum price (cents) for tail zone')
    parser.add_argument('--max-spread', type=int, default=10,
                        help='Maximum spread (cents) to consider quote real')
    parser.add_argument('--contracts', type=int, default=5,
                        help='Contracts per trade')
    parser.add_argument('--window', type=int, default=7,
                        help='Entry window: days before settlement to start looking')
    parser.add_argument('--simulate', action='store_true',
                        help='Full strategy simulation with capital constraints and time')
    parser.add_argument('--capital', type=int, default=10000,
                        help='Starting capital in cents (default: $100)')
    parser.add_argument('--legacy', action='store_true',
                        help='Use legacy PIT Beta model instead of calibration edges')
    parser.add_argument('--allow-unknown', action='store_true',
                        help='(Legacy mode) Trade unknown series using global PIT model')
    args = parser.parse_args()

    conn = get_conn()

    if args.simulate:
        from trading.trader import EdgeLookup
        edge_lookup = EdgeLookup()
        params = TradingParams(
            min_tail=args.min_tail,
            max_tail=args.max_tail,
            max_spread=args.max_spread,
        )
        track = run_simulation(
            conn, edge_lookup, params=params,
            starting_capital_cents=args.capital,
            contracts_per_trade=args.contracts if args.contracts != 5 else 0,
            entry_window_days=args.window,
        )
        conn.close()

        print(f"\n{'='*70}")
        print("SIMULATION RESULTS (full strategy with capital constraints)")
        print(f"{'='*70}")
        track.print_summary("ALL")

        for gp, sub in sorted(track.by_category().items(), key=lambda x: -len(x[1])):
            sub.print_summary(gp)

        cal = track.edge_vs_realized()
        if cal.get('n', 0) > 0:
            print("\n  Edge calibration:")
            print(f"    Predicted edge: {cal['avg_predicted_edge']:.2%}")
            print(f"    Realized return: {cal['avg_realized_return']:.2%}")
            print(f"    Edge capture: {cal['edge_capture']:.0%}")

    elif args.legacy:
        # Legacy mode: PIT Beta model + hardcoded SERIES_EDGE_PCT
        print_edge_table()
        trades, n_examined, n_with_trades = run_backtest(
            conn,
            min_tail=args.min_tail,
            max_tail=args.max_tail,
            max_spread=args.max_spread,
            contracts_per_trade=args.contracts,
            entry_window_days=args.window,
            allow_unknown_series=args.allow_unknown,
        )
        conn.close()
        print_results(trades, n_examined, n_with_trades)
    else:
        # Default: calibration-based edges (same as live trader)
        from trading.trader import EdgeLookup
        edge_lookup = EdgeLookup()

        params = TradingParams(
            min_tail=args.min_tail,
            max_tail=args.max_tail,
            max_spread=args.max_spread,
        )
        track = run_backtest_calibration(
            conn, edge_lookup, params=params,
            contracts_per_trade=args.contracts,
            entry_window_days=args.window,
        )
        conn.close()

        print(f"\n{'='*70}")
        print("BACKTEST RESULTS (calibration-based edges)")
        print(f"{'='*70}")
        track.print_summary("ALL")

        # By generating process
        for gp, sub in sorted(track.by_category().items(),
                               key=lambda x: -len(x[1])):
            sub.print_summary(gp)

        # By series (top 15)
        series_groups = track.by_series()
        top_series = sorted(series_groups.items(), key=lambda x: -len(x[1]))[:15]
        if top_series:
            print("\n  Top series:")
            for s, sub in top_series:
                sub_s = sub.summary()
                print(f"    {s:>25}: n={sub_s['n']:>4} "
                      f"win={sub_s['win_rate']:.0%} "
                      f"pnl={sub_s['total_pnl_cents']:+d}¢")

        # Calibration check
        cal = track.edge_vs_realized()
        if cal.get('n', 0) > 0:
            print("\n  Edge calibration:")
            print(f"    Predicted edge: {cal['avg_predicted_edge']:.2%}")
            print(f"    Realized return: {cal['avg_realized_return']:.2%}")
            print(f"    Edge capture: {cal['edge_capture']:.0%}")


if __name__ == "__main__":
    main()
