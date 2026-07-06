#!/usr/bin/env python3
"""
Strategy replay: backtest via the production code path.

Reconstructs Kalshi-API-shaped event dicts from historical hourly candle
data and feeds them through FLBStrategy.scan() — the exact same code
that runs in live trading. The fill model determines which resting orders
actually fill based on candle volume and price action.

Data: hourly candles only (non-overlapping 1-hour partitions). Close
prices are used for strategy decisions (contemporaneous bid/ask). High/
low/volume are used for fill simulation (what happened during the hour).

Usage:
    python replay.py                          # $100 starting capital
    python replay.py --capital 100000         # $1000
    python replay.py --capture-rate 0.10      # conservative fills
"""

import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

from cost_model import KALSHI_COSTS
from fill_model import FillModel, CandleData
from flb_strategy import FLBStrategy
from flow_model import compute_opposing_flow, compute_trailing_volume, FlowModel
from risk import RiskLimits
from strategy import (
    DEFAULT_PARAMS,
    TradingParams,
    optimal_quantity,
)
from track_record import TradeRecord, TrackRecord


def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


def compute_trailing_volume_from_candles(candles, period_end):
    """Compute trailing 24h volume from hourly candle data.

    Args:
        candles: sorted list of (period_end, CandleData) for one ticker
        period_end: current period timestamp

    Returns:
        Total volume across candles in the 24h window ending at period_end.
        Equivalent to summing trade counts from the trade tape, but uses
        pre-aggregated hourly candle volumes already in memory.
    """
    if not candles:
        return 0
    import bisect
    from datetime import timedelta
    cutoff = period_end - timedelta(hours=24)
    # Binary search on period_end timestamps
    times = [c[0] for c in candles]
    start = bisect.bisect_left(times, cutoff)
    end = bisect.bisect_right(times, period_end)
    return sum(candles[i][1].volume for i in range(start, end))


class LazyTradeLoader:
    """Load trades per-ticker on demand from DB, with caching.

    Replaces bulk preload_replay_trades() to avoid loading 300K tickers
    into memory. Only loads trades for tickers that actually receive
    orders (~1K-5K over a 6-month replay).
    """

    def __init__(self, conn):
        self.conn = conn
        self.cache = {}
        self._n_loads = 0

    def get(self, ticker, default=None):
        if ticker in self.cache:
            return self.cache[ticker]
        trades = self._load_ticker(ticker)
        self.cache[ticker] = trades
        self._n_loads += 1
        return trades if trades else (default if default is not None else [])

    def _load_ticker(self, ticker):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT created_time, count, yes_price, taker_side
            FROM prediction_markets.kalshi_trades
            WHERE ticker = %s
            ORDER BY created_time
        """, (ticker,))
        trades = [(row[0], int(row[1]), float(row[2]), row[3]) for row in cur]
        cur.close()
        return trades

    def __contains__(self, ticker):
        return ticker in self.cache

    def __len__(self):
        return self._n_loads


def simulate_fill_from_tape(ticker, side, limit_price_cents, contracts,
                            placed_at, settlement_time, ticker_trades):
    """Simulate fill of a pending order against the trade tape.

    Called once at order placement. Returns a fill schedule: list of
    (fill_time, contracts_filled) tuples sorted by time.
    """
    import bisect

    trades = ticker_trades.get(ticker, [])
    if not trades:
        return []

    times = [t[0] for t in trades]
    start_idx = bisect.bisect_right(times, placed_at)
    remaining = contracts
    fills = []
    for i in range(start_idx, len(trades)):
        t_time, t_count, t_yes_price, t_taker_side = trades[i]
        if t_time >= settlement_time:
            break
        opposing = compute_opposing_flow(
            side, limit_price_cents, t_yes_price, t_taker_side, t_count)
        if opposing > 0:
            filled = min(opposing, remaining)
            fills.append((t_time, filled))
            remaining -= filled
            if remaining <= 0:
                break
    return fills


def preload_replay_trades(conn, tickers):
    """Pre-load trade tape for the replay universe.

    Args:
        tickers: set of tickers that appear in the replay

    Returns:
        dict of ticker → list of (created_time, count, yes_price, taker_side)
        sorted by created_time ascending.
    """
    from psycopg2.extras import execute_values

    cur = conn.cursor()
    ticker_list = sorted(tickers)
    cur.execute("CREATE TEMP TABLE _replay_tickers (ticker text PRIMARY KEY)")
    chunk_size = 10000
    for i in range(0, len(ticker_list), chunk_size):
        chunk = ticker_list[i:i + chunk_size]
        execute_values(cur, "INSERT INTO _replay_tickers VALUES %s",
                       [(t,) for t in chunk])

    print("  Loading replay trades...", end="", flush=True)
    cur2 = conn.cursor("replay_trades")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT t.ticker, t.created_time, t.count, t.yes_price, t.taker_side
        FROM prediction_markets.kalshi_trades t
        JOIN _replay_tickers rt ON rt.ticker = t.ticker
        ORDER BY t.ticker, t.created_time
    """)

    trades_by_ticker = {}
    current_ticker = None
    current_trades = []
    n = 0
    for ticker, created_time, count, yes_price, taker_side in cur2:
        if ticker != current_ticker:
            if current_ticker and current_trades:
                trades_by_ticker[current_ticker] = current_trades
            current_ticker = ticker
            current_trades = []
        current_trades.append((created_time, int(count), float(yes_price), taker_side))
        n += 1
    if current_ticker and current_trades:
        trades_by_ticker[current_ticker] = current_trades
    cur2.close()

    cur.execute("DROP TABLE _replay_tickers")
    cur.close()
    print(f" {n:,} trades, {len(trades_by_ticker):,} tickers")
    return trades_by_ticker


def replay(conn, edge_lookup, params: TradingParams = DEFAULT_PARAMS,
           starting_capital_cents: int = 10000,
           fill_model: FillModel | None = None,
           risk_limits: RiskLimits | None = None,
           use_synthetic_spread: bool = False,
           ev_strategy=None,
           replay_after: str | None = None,
           expanding=None,
           use_tape_fills: bool = False,
           use_flow_model: bool = False) -> TrackRecord:
    """Replay a strategy on historical hourly candle data.

    Uses either FLBStrategy (default) or EVStrategy (if ev_strategy provided)
    with historical data reconstructed into API-shaped event dicts.

    Fill modes:
    - Default: candle-based FillModel (capture_rate × volume)
    - use_tape_fills: deterministic tape-based fill simulation
    - use_flow_model: probabilistic Bernoulli fills from FlowModel estimates
    Both tape modes require expanding mode.
    """
    if fill_model is None:
        fill_model = FillModel()
    if risk_limits is None:
        risk_limits = RiskLimits()
    if (use_tape_fills or use_flow_model) and not expanding:
        raise ValueError("Tape-based fills require expanding mode (--expanding)")

    replay_trades = expanding.get('replay_trades', {}) if expanding else {}

    if ev_strategy is not None:
        strategy = ev_strategy
        use_ev = True
    else:
        strategy = FLBStrategy(edge_lookup, params)
        use_ev = False
    costs = KALSHI_COSTS

    # ── Load data ─────────────────────────────────────────────────

    cur = conn.cursor()

    # Load market_structure for events
    print("Loading event structure...")
    cur.execute("""
        SELECT event_ticker, market_structure
        FROM kalshi_settled_events
        WHERE market_structure IS NOT NULL
    """)
    event_structure = {row[0]: row[1] for row in cur}
    # Also load from active events (for recently created events not yet settled)
    cur.execute("""
        SELECT event_ticker, market_structure
        FROM kalshi_events
        WHERE market_structure IS NOT NULL
    """)
    for et, ms in cur:
        if et not in event_structure:
            event_structure[et] = ms
    print(f"  Event structures: {len(event_structure)}")

    print("Loading settled markets...")
    cur.execute("""
        SELECT ticker, event_ticker, result, settled_at
        FROM kalshi_settled_markets
        WHERE result IN ('yes', 'no') AND settled_at != '' AND event_ticker != ''
    """)
    settled = {}
    for ticker, event, result, settled_at in cur:
        try:
            if isinstance(settled_at, str):
                sdt = datetime.fromisoformat(settled_at.replace('Z', '+00:00'))
            else:
                sdt = settled_at
                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        settled[ticker] = {
            'event': event, 'result': result, 'settled_at': sdt,
            'series': event.split('-')[0],
        }
    print(f"  Settled markets: {len(settled)}")

    print("Loading hourly candles...")
    cur2 = conn.cursor("replay_candles")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT ticker, period_end,
               yes_bid_close, yes_ask_close,
               yes_bid_high, yes_ask_low,
               COALESCE(volume, 0) AS volume,
               COALESCE(price_high, 0) AS price_high,
               COALESCE(price_low, 0) AS price_low,
               COALESCE(open_interest, 0) AS open_interest
        FROM kalshi_hourly_candles
        WHERE yes_bid_close IS NOT NULL AND yes_ask_close IS NOT NULL
          AND yes_bid_close > 0 AND yes_ask_close > 0
        ORDER BY period_end
    """)

    # Build period-indexed data and per-ticker candle lists
    # period_markets: {period_end: [(ticker, bid_close, ask_close)]}
    # ticker_fill_candles: {ticker: [(period_end, CandleData)]}
    period_markets = defaultdict(list)
    ticker_fill_candles = defaultdict(list)
    n_candles = 0

    for ticker, period_end, bid_close, ask_close, bid_high, ask_low, vol, p_high, p_low, oi in cur2:
        if ticker not in settled:
            continue
        # Strategy's view of the market
        if use_synthetic_spread:
            # Use bid_high/ask_low — non-simultaneous extremes that synthesize
            # a tighter spread. Reproduces the earlier backtest's (incorrect) view.
            strat_bid = str(float(bid_high))
            strat_ask = str(float(ask_low))
        else:
            # Use close prices — contemporaneous point-in-time (correct)
            strat_bid = str(float(bid_close))
            strat_ask = str(float(ask_close))
        period_markets[period_end].append((
            ticker, strat_bid, strat_ask, int(oi),
        ))
        # Fill model sees high/low/volume (cents) + trade OHLC
        ticker_fill_candles[ticker].append((
            period_end,
            CandleData(
                yes_bid_high=int(round(float(bid_high) * 100)),
                yes_ask_low=int(round(float(ask_low) * 100)),
                volume=vol,
                price_high=int(round(float(p_high) * 100)) if p_high else 0,
                price_low=int(round(float(p_low) * 100)) if p_low else 0,
            ),
        ))
        n_candles += 1
    cur2.close()

    periods = sorted(period_markets.keys())
    if replay_after:
        cutoff = datetime.fromisoformat(replay_after).replace(tzinfo=timezone.utc)
        periods = [p for p in periods if p >= cutoff]
    print(f"  Candles: {n_candles} across {len(ticker_fill_candles)} tickers, "
          f"{len(periods)} periods")
    print(f"  Date range: {periods[0].date()} to {periods[-1].date()}")

    # ── Load replay trades if needed ────────────────────────────
    # Trailing volume now uses candle data (already in memory), so we
    # only need trade data for fill simulation of placed orders.
    if use_tape_fills and not replay_trades:
        # Lazy-load: query DB per-ticker when orders are placed (~1K-5K tickers)
        # instead of bulk-loading all 300K tickers (~10+ GB).
        replay_trades = LazyTradeLoader(conn)
        # Seed cache with calibration trades that overlap replay universe
        calibration_trades = expanding.get('trades_by_ticker', {}) if expanding else {}
        for t, v in calibration_trades.items():
            if t in ticker_fill_candles:
                replay_trades.cache[t] = v
        print(f"  Lazy trade loader ready ({len(replay_trades.cache):,} pre-cached "
              f"from calibration)")
    elif use_flow_model and not replay_trades:
        # Probabilistic fills don't need per-ticker trades at all
        replay_trades = {}
        print(f"  Flow-model fills (no per-ticker trade data needed)")
    if expanding:
        expanding['replay_trades'] = replay_trades

    # ── Simulation state ──────────────────────────────────────────

    cash = starting_capital_cents
    escrow = 0
    pending = {}       # ticker -> {side, price, contracts, event, edge, placed_at, ...}
    filled = {}        # ticker -> same
    active_tickers = set()
    track = TrackRecord()

    n_orders = 0
    n_settlements = 0
    n_partial = 0
    n_risk_blocked = 0
    n_unfilled_expired = 0
    fill_diags = []  # diagnostic records for fill model comparison

    # ── Expanding window state ──────────────────────────────────
    last_recal_date = None
    last_fill_recal_date = None
    expanding_legacy = expanding.get('legacy') if expanding else False
    expanding_flow = expanding.get('use_flow') if expanding else False
    expanding_view_factory = expanding.get('view_factory') if expanding else None
    if expanding:
        from ev_strategy import EVStrategy
        if expanding_legacy:
            legacy_view = expanding['legacy_view']
        elif expanding_view_factory is None:
            # Legacy MarketView path (FlowModel, FillPredictor)
            from market_view import MarketView
            cached_fill_estimator = None
            cached_flow_model = None

    # ── Replay each period ────────────────────────────────────────

    for period_idx, period_end in enumerate(periods):

        # ── 0. Build View if expanding window and new day ──────────
        if expanding:
            current_date = period_end.date()
            if current_date != last_recal_date:
                cutoff = period_end.replace(hour=0, minute=0, second=0, microsecond=0)

                days_since_fill = ((current_date - last_fill_recal_date).days
                                   if last_fill_recal_date else 999)
                recal_fills = days_since_fill >= 7

                joint_search = False

                if expanding_legacy:
                    label = "legacy" if not recal_fills else "legacy+fills"
                    print(f"  Recalibrating {label} at {current_date}...",
                          flush=True)
                    legacy_view.recalibrate(cutoff, recal_fills=recal_fills)
                    view = legacy_view
                elif expanding_view_factory is not None:
                    # Framework path — ViewFactory.build()
                    # Note: recalibrates ALL estimators (including fills) on every
                    # daily boundary. The old MarketView path cached fills weekly.
                    # Performance optimization via CalibrationStore is deferred.
                    view = expanding_view_factory.build(as_of=cutoff)
                else:
                    # Legacy MarketView path (FlowModel, FillPredictor)
                    joint_search = True
                    expanding_gbt = expanding.get('fill_predictor')
                    if expanding_gbt is not None:
                        view = MarketView(
                            as_of=cutoff,
                            all_observations=expanding['observations'],
                            classifications=expanding['classifications'],
                            fill_predictor=expanding_gbt)
                    elif expanding_flow:
                        if recal_fills:
                            print(f"  Rebuilding MarketView at {current_date} "
                                  f"(FlowModel recal)...", flush=True)
                            view = MarketView(
                                as_of=cutoff,
                                all_observations=expanding['observations'],
                                all_trades=expanding['trades_by_ticker'],
                                settled_markets=expanding['settled_markets'],
                                classifications=expanding['classifications'])
                            cached_flow_model = view._flow_model
                        else:
                            view = MarketView(
                                as_of=cutoff,
                                all_observations=expanding['observations'],
                                all_trades={},
                                settled_markets=expanding.get('settled_markets', {}),
                                classifications=expanding['classifications'])
                            view._flow_model = cached_flow_model

                if recal_fills:
                    last_fill_recal_date = current_date

                strategy = EVStrategy(view, params=params,
                                      joint_search=joint_search)
                use_ev = True
                last_recal_date = current_date

        # ── 1. Settle resolved markets ────────────────────────────

        for ticker in list(filled.keys()):
            md = settled[ticker]
            if md['settled_at'] <= period_end:
                order = filled.pop(ticker)
                _settle_order(order, md, ticker, costs, track)
                order_cost = order['price'] * order['contracts']
                result = md['result']
                won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
                exit_price = 100 if won else 0
                fee = costs.maker_fee(order['price'], order['contracts'])
                escrow -= order_cost
                cash += exit_price * order['contracts'] - fee
                n_settlements += 1

        # Handle pending orders for settled markets
        for ticker in list(pending.keys()):
            md = settled[ticker]
            if md['settled_at'] <= period_end:
                order = pending.pop(ticker)
                filled_qty = order['contracts_filled']
                requested = order['contracts']
                unfilled = requested - filled_qty
                # Return unfilled escrow
                escrow -= order['price'] * unfilled
                cash += order['price'] * unfilled
                if filled_qty == 0:
                    n_unfilled_expired += 1
                else:
                    # Settle the partially filled portion
                    result = md['result']
                    won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
                    exit_price = 100 if won else 0
                    fee = costs.maker_fee(order['price'], filled_qty)
                    escrow -= order['price'] * filled_qty
                    cash += exit_price * filled_qty - fee
                    _record_trade(order, md, ticker, filled_qty, costs, track)
                    n_settlements += 1
                    n_partial += 1

        # ── 2. Process fills on pending orders ────────────────────

        for ticker in list(pending.keys()):
            order = pending[ticker]

            if 'fill_schedule' in order:
                # Tape-based: process precomputed fill schedule
                schedule = order['fill_schedule']
                idx = order['fill_schedule_idx']
                while idx < len(schedule):
                    fill_time, fill_qty = schedule[idx]
                    if fill_time > period_end:
                        break
                    order['contracts_filled'] += fill_qty
                    idx += 1
                order['fill_schedule_idx'] = idx
                if order['contracts_filled'] >= order['contracts']:
                    filled[ticker] = order
                    del pending[ticker]
            else:
                # Candle-based: original fill model
                remaining = order['contracts'] - order['contracts_filled']
                candles = ticker_fill_candles.get(ticker, [])
                for pe, cd in candles:
                    if pe != period_end:
                        continue
                    filled_qty = fill_model.check_fill(order['side'], order['price'],
                                                       remaining, cd)
                    order['contracts_filled'] += filled_qty
                    if order['contracts_filled'] >= order['contracts']:
                        filled[ticker] = order
                        del pending[ticker]
                    break

        # ── 3. Reconstruct events and call strategy ───────────────

        # Build API-shaped event dicts from this period's candle close prices
        event_groups = defaultdict(list)
        for ticker, bid_str, ask_str, oi in period_markets[period_end]:
            if ticker in active_tickers:
                continue
            md = settled.get(ticker)
            if md is None:
                continue
            if md['settled_at'] <= period_end:
                continue  # already settled
            market_dict = {
                "ticker": ticker,
                "status": "active",
                "yes_bid_dollars": bid_str,
                "yes_ask_dollars": ask_str,
                "expected_expiration_time": md['settled_at'].isoformat(),
                "title": "",
                "open_interest": oi,
            }
            # Inject trailing volume from candle data (no trade tape needed)
            if use_tape_fills or use_flow_model:
                candles = ticker_fill_candles.get(ticker, [])
                market_dict['volume_24h_fp'] = str(
                    compute_trailing_volume_from_candles(candles, period_end))
            event_groups[md['event']].append(market_dict)

        events = []
        for ev, mkts in event_groups.items():
            e = {"event_ticker": ev, "markets": mkts}
            ms = event_structure.get(ev)
            if ms is not None:
                e["market_structure"] = ms
            events.append(e)

        if not events:
            continue

        # Call the strategy
        if use_ev:
            opps = strategy.scan(events, traded_tickers=active_tickers,
                                 now=period_end)
        else:
            pairs = strategy.scan(events, traded_tickers=active_tickers,
                                  now=period_end)
            # Flatten pairs into individual leg opportunities
            opps = []
            for pair in pairs:
                for opp in [pair.yes_opp, pair.no_opp]:
                    opps.append(opp)

        # ── 4. Place orders with risk limits ──────────────────────

        total_equity = cash + escrow

        # Position count limit (same as live trader)
        current_positions = len(pending) + len(filled)
        ok, _ = risk_limits.check_position_count(current_positions)
        if not ok:
            continue

        for opp in opps:
            if use_ev:
                ticker = opp.ticker
                side = opp.side
                price = opp.limit_price
                q = opp.contracts
                ev_ticker = opp.event_ticker
                edge = opp.ev_per_contract / 100.0
                gp = opp.generating_process
                topic = opp.topic
            else:
                ticker = opp.ticker
                side = opp.side
                price = opp.bid_price
                q = optimal_quantity(price, max_q=params.max_contracts)
                ev_ticker = opp.event_ticker
                edge = opp.edge
                gp = opp.generating_process
                topic = opp.topic

            if ticker in active_tickers:
                continue
            cost = price * q
            if cash < cost:
                continue

            ok, _ = risk_limits.check_deployment(total_equity, escrow, cost)
            if not ok:
                n_risk_blocked += 1
                continue

            event_exp = sum(
                o['price'] * o.get('contracts', o.get('contracts', 0))
                for o in list(pending.values()) + list(filled.values())
                if o['event'] == ev_ticker
            )
            ok, _ = risk_limits.check_event_concentration(
                total_equity, event_exp, cost)
            if not ok:
                n_risk_blocked += 1
                continue

            # Look up the market dict to store bid/ask/OI for fill simulation
            mkt = None
            for ev in events:
                for m in ev.get('markets', []):
                    if m.get('ticker') == ticker:
                        mkt = m
                        break
                if mkt:
                    break

            order = {
                'side': side, 'price': price,
                'contracts': q, 'contracts_filled': 0,
                'event': ev_ticker,
                'edge': edge, 'placed_at': period_end,
                'gp': gp, 'topic': topic,
                'p_event': opp.p_event if use_ev else None,
                'p_fill': opp.p_fill if use_ev else None,
                'yes_bid_cents': int(round(float(mkt['yes_bid_dollars']) * 100)) if mkt else 0,
                'yes_ask_cents': int(round(float(mkt['yes_ask_dollars']) * 100)) if mkt else 0,
                'open_interest': int(mkt.get('open_interest', 0)) if mkt else 0,
            }

            if use_tape_fills:
                # Precompute fill schedule from trade tape
                settle_time = settled[ticker]['settled_at']
                order['fill_schedule'] = simulate_fill_from_tape(
                    ticker, side, price, q, period_end, settle_time,
                    replay_trades)
                order['fill_schedule_idx'] = 0

                # Diagnostic: record FlowModel prediction alongside tape result
                if use_ev and hasattr(strategy, 'view') and strategy.view._flow_model:
                    result = settled[ticker]['result']
                    won = (result == side)
                    hours = (settle_time - period_end).total_seconds() / 3600
                    candles = ticker_fill_candles.get(ticker, [])
                    candle_vol = compute_trailing_volume_from_candles(candles, period_end)
                    # Compute tape-based trailing volume (matches calibration)
                    ticker_trades = replay_trades.get(ticker) if replay_trades else None
                    if ticker_trades:
                        from flow_model import compute_trailing_volume
                        tape_vol = compute_trailing_volume(ticker_trades, period_end)
                    else:
                        tape_vol = candle_vol
                    # Use tape volume for model query (matches calibration source)
                    est = strategy.view.fill_estimate(gp, topic, hours, side, q,
                                                       price, tape_vol)
                    tape_filled = sum(f[1] for f in order['fill_schedule'])
                    from flow_model import _time_bucket, _trailing_vol_bucket
                    diag = {
                        'p_fill_won': est.p_fill_won if est else None,
                        'p_fill_lost': est.p_fill_lost if est else None,
                        'won': won,
                        'tape_filled': tape_filled,
                        'tape_full': tape_filled >= q,
                        'contracts': q,
                        'gp': gp, 'topic': topic,
                        'hours': hours, 'side': side,
                        'trail_vol': tape_vol,
                        'candle_vol': candle_vol,
                        'time_bucket': _time_bucket(hours),
                        'vol_bucket': _trailing_vol_bucket(tape_vol),
                        'candle_vol_bucket': _trailing_vol_bucket(candle_vol),
                    }
                    fill_diags.append(diag)
            elif (use_flow_model or _has_fill_predictor(strategy)) and use_ev:
                # Probabilistic: Bernoulli draw conditioned on actual outcome.
                # We know the settlement result (it's a backtest), so use
                # p_fill_won or p_fill_lost accordingly — not unconditional p_fill.
                import random
                result = settled[ticker]['result']
                won = (result == side)
                hours = (settled[ticker]['settled_at'] - period_end).total_seconds() / 3600
                candles = ticker_fill_candles.get(ticker, [])
                trail_vol = compute_trailing_volume_from_candles(candles, period_end)
                est = strategy.view.fill_estimate(
                    gp, topic, hours, side, q, price, trail_vol,
                    bid_cents=order.get('yes_bid_cents', 0),
                    ask_cents=order.get('yes_ask_cents', 0),
                    open_interest=order.get('open_interest', 0))
                if est is not None:
                    p_fill_cond = est.p_fill_won if won else est.p_fill_lost
                else:
                    p_fill_cond = opp.p_fill  # fallback to unconditional
                if random.random() < p_fill_cond:
                    midpoint = period_end + (settled[ticker]['settled_at'] - period_end) / 2
                    order['fill_schedule'] = [(midpoint, q)]
                else:
                    order['fill_schedule'] = []
                order['fill_schedule_idx'] = 0

            pending[ticker] = order
            cash -= cost
            escrow += cost
            active_tickers.add(ticker)
            n_orders += 1

        # Progress
        if (period_idx + 1) % 1000 == 0:
            equity = cash + escrow
            print(f"  Period {period_idx+1}/{len(periods)}: {period_end.date()} | "
                  f"orders={n_orders} settled={n_settlements} "
                  f"equity=${equity/100:.0f} pending={len(pending)} filled={len(filled)}")

    # ── Settle everything remaining ───────────────────────────────

    for ticker, order in list(filled.items()):
        md = settled.get(ticker)
        if md is None:
            continue
        result = md['result']
        won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
        exit_price = 100 if won else 0
        fee = costs.maker_fee(order['price'], order['contracts'])
        escrow -= order['price'] * order['contracts']
        cash += exit_price * order['contracts'] - fee
        _record_trade(order, md, ticker, order['contracts'], costs, track)
        n_settlements += 1

    for ticker, order in list(pending.items()):
        md = settled.get(ticker)
        if md is None:
            continue
        filled_qty = order['contracts_filled']
        unfilled = order['contracts'] - filled_qty
        escrow -= order['price'] * unfilled
        cash += order['price'] * unfilled
        if filled_qty > 0:
            result = md['result']
            won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
            exit_price = 100 if won else 0
            fee = costs.maker_fee(order['price'], filled_qty)
            escrow -= order['price'] * filled_qty
            cash += exit_price * filled_qty - fee
            _record_trade(order, md, ticker, filled_qty, costs, track)
            n_settlements += 1
            n_partial += 1
        else:
            n_unfilled_expired += 1

    final_equity = cash + escrow
    print("\n  Replay complete:")
    print(f"    Periods: {len(periods)} ({periods[0].date()} to {periods[-1].date()})")
    print(f"    Orders placed:     {n_orders}")
    print(f"    Settlements:       {n_settlements} ({n_partial} partial)")
    print(f"    Unfilled expired:  {n_unfilled_expired}")
    print(f"    Risk-blocked:      {n_risk_blocked}")
    print(f"    Starting capital:  ${starting_capital_cents/100:.2f}")
    print(f"    Final equity:      ${final_equity/100:.2f}")
    print(f"    Return:            {(final_equity - starting_capital_cents) / starting_capital_cents:.1%}")
    if escrow != 0:
        print(f"    WARNING: Unsettled escrow: ${escrow/100:.2f}")

    # ── Fill model diagnostic ───────────────────────────────────
    if fill_diags:
        _print_fill_diagnostic(fill_diags)

    return track


def _print_fill_diagnostic(diags):
    """Print fill model calibration diagnostic: predicted vs tape fill rates."""
    from collections import defaultdict

    print(f"\n  {'='*60}")
    print(f"  FILL MODEL DIAGNOSTIC ({len(diags)} orders)")
    print(f"  {'='*60}")

    # Overall
    has_est = [d for d in diags if d['p_fill_won'] is not None]
    no_est = len(diags) - len(has_est)
    if no_est:
        print(f"  ({no_est} orders had no FlowModel estimate)")
    if not has_est:
        print("  No orders with FlowModel estimates.")
        return

    # Split by outcome
    won = [d for d in has_est if d['won']]
    lost = [d for d in has_est if not d['won']]

    def _stats(subset, label):
        if not subset:
            return
        tape_fill_rate = sum(1 for d in subset if d['tape_full']) / len(subset)
        # Use outcome-conditioned prediction: p_fill_won for winners, p_fill_lost for losers
        pred_fill_rate = sum(
            d['p_fill_won'] if d['won'] else d['p_fill_lost']
            for d in subset) / len(subset)
        print(f"  {label:30s}  n={len(subset):5d}  "
              f"tape={tape_fill_rate:5.1%}  pred={pred_fill_rate:5.1%}  "
              f"gap={tape_fill_rate - pred_fill_rate:+5.1%}")

    _stats(has_est, "ALL")
    _stats(won, "  won")
    _stats(lost, "  lost")

    # By category
    cats = defaultdict(list)
    for d in has_est:
        cats[d['gp']].append(d)
    print()
    for cat in sorted(cats, key=lambda c: -len(cats[c])):
        subset = cats[cat]
        _stats(subset, cat)
        cat_won = [d for d in subset if d['won']]
        cat_lost = [d for d in subset if not d['won']]
        _stats(cat_won, f"  {cat}/won")
        _stats(cat_lost, f"  {cat}/lost")

    # By side
    print()
    for side_val in ('yes', 'no'):
        subset = [d for d in has_est if d['side'] == side_val]
        _stats(subset, f"side={side_val}")

    # By quantity
    print()
    for q in sorted(set(d['contracts'] for d in has_est)):
        subset = [d for d in has_est if d['contracts'] == q]
        if len(subset) >= 10:
            _stats(subset, f"Q={q}")

    # By time bucket (if available)
    if 'time_bucket' in has_est[0]:
        print("\n  --- By time bucket ---")
        tbs = defaultdict(list)
        for d in has_est:
            tbs[d['time_bucket']].append(d)
        for tb in ['<1h', '1-3h', '3-6h', '6-12h', '12-24h', '1-3d', '3-7d']:
            if tb in tbs and len(tbs[tb]) >= 5:
                _stats(tbs[tb], f"time={tb}")

    # By volume bucket (if available)
    if 'vol_bucket' in has_est[0]:
        print("\n  --- By volume bucket ---")
        vbs = defaultdict(list)
        for d in has_est:
            vbs[d['vol_bucket']].append(d)
        for vb in ['dead', 'low', 'moderate', 'active', 'high']:
            if vb in vbs and len(vbs[vb]) >= 5:
                _stats(vbs[vb], f"vol={vb}")

    # Volume mismatch: candle vs tape
    if 'candle_vol_bucket' in has_est[0]:
        print("\n  --- Volume Mismatch (candle vs tape) ---")
        mismatch = sum(1 for d in has_est
                      if d['vol_bucket'] != d['candle_vol_bucket'])
        print(f"  {mismatch}/{len(has_est)} orders have different "
              f"candle vs tape vol bucket ({mismatch/len(has_est):.0%})")
        # Show transition matrix
        transitions = defaultdict(int)
        for d in has_est:
            transitions[(d['candle_vol_bucket'], d['vol_bucket'])] += 1
        print(f"  {'candle→tape':25s} {'count':>6s}")
        for (cv, tv), n in sorted(transitions.items(), key=lambda x: -x[1]):
            if cv != tv:
                print(f"  {cv:>10s} → {tv:<10s} {n:6d}")

    # Cross: category × tape volume for worst categories
    if 'vol_bucket' in has_est[0]:
        print("\n  --- Category × Tape Volume (won only) ---")
        for cat in sorted(cats, key=lambda c: -len(cats[c])):
            cat_won = [d for d in cats[cat] if d['won']]
            if len(cat_won) < 10:
                continue
            cat_vbs = defaultdict(list)
            for d in cat_won:
                cat_vbs[d['vol_bucket']].append(d)
            for vb in ['dead', 'low', 'moderate', 'active', 'high']:
                if vb in cat_vbs and len(cat_vbs[vb]) >= 3:
                    _stats(cat_vbs[vb], f"  {cat}/{vb}")


def _has_fill_predictor(strategy):
    """Check if the strategy's view has a FillPredictor."""
    return (hasattr(strategy, 'view')
            and hasattr(strategy.view, '_fill_predictor')
            and strategy.view._fill_predictor is not None)


def _settle_order(order, md, ticker, costs, track):
    """Record a settled trade in the track record (no capital changes)."""
    _record_trade(order, md, ticker, order['contracts'], costs, track)


def _record_trade(order, md, ticker, contracts, costs, track):
    """Add a TradeRecord for a settled trade."""
    result = md['result']
    won = (result == 'yes') if order['side'] == 'yes' else (result == 'no')
    exit_price = 100 if won else 0
    fee = costs.maker_fee(order['price'], contracts)
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
        entry_date=order['placed_at'].strftime('%Y-%m-%d %H:%M'),
        p_event=order.get('p_event', 0.0) or 0.0,
        p_fill=order.get('p_fill', 0.0) or 0.0,
    ))


def main():
    parser = argparse.ArgumentParser(
        description="Replay FLB strategy on historical data via production code path.",
    )
    parser.add_argument('--capital', type=int, default=10000,
                        help='Starting capital in cents (default: $100)')
    parser.add_argument('--capture-rate', type=float, default=0.20,
                        help='Fill model capture rate (default: 0.20)')
    parser.add_argument('--min-fill', type=int, default=1,
                        help='Minimum contracts per fill touch (default: 1)')
    parser.add_argument('--max-spread', type=int, default=10,
                        help='Maximum spread in cents (default: 10)')
    parser.add_argument('--synthetic-spread', action='store_true',
                        help='Use bid_high/ask_low instead of close prices (for comparison)')
    parser.add_argument('--calibration', choices=['legacy', 'bid', 'mid', 'ask', 'trade'],
                        default='legacy',
                        help='Edge source: legacy (old table) or price-conditioned calibration')
    parser.add_argument('--no-volume-gate', action='store_true',
                        help='Fill on any price touch regardless of volume')
    parser.add_argument('--ev', action='store_true',
                        help='Use EV-maximizing strategy instead of FLB')
    parser.add_argument('--expanding', action='store_true',
                        help='Use expanding-window calibration (recalibrate daily)')
    parser.add_argument('--expanding-legacy', action='store_true',
                        help='Like --expanding but uses old ExpandingEventRates/ExpandingFillRates')
    parser.add_argument('--csv', type=str, default=None,
                        help='Write trade-level CSV with running P&L to this path')
    parser.add_argument('--min-hours', type=float, default=None,
                        help='Minimum hours to settlement (overrides DB param)')
    parser.add_argument('--after',
                        help='Only replay periods after this date (YYYY-MM-DD)')
    parser.add_argument('--tape-fills', action='store_true',
                        help='Use trade-tape-based fill simulation (requires --expanding)')
    parser.add_argument('--flow-fills', action='store_true',
                        help='Use FlowModel probabilistic fills (requires --expanding)')
    parser.add_argument('--gbt-fills', type=str, default=None,
                        help='Path to pre-trained FillPredictor model. '
                             'Uses GBT queue-aware fill predictions (requires --expanding)')
    args = parser.parse_args()

    if args.calibration == 'legacy':
        from trader import EdgeLookup
        edge_lookup = EdgeLookup()
    else:
        from trader import CalibrationLookup
        edge_lookup = CalibrationLookup(price_method=args.calibration)

    from trader import load_trading_params
    params = load_trading_params()
    overrides = {}
    if args.max_spread != 10:
        overrides['max_spread'] = args.max_spread
    if args.min_hours is not None:
        overrides['min_hours_to_settle'] = args.min_hours
    if overrides:
        params = TradingParams(**{**params.__dict__, **overrides})
    fm = FillModel(capture_rate=args.capture_rate, min_fill_per_touch=args.min_fill,
                   require_volume=not args.no_volume_gate)

    conn = get_conn()

    use_flow = args.tape_fills or args.flow_fills

    ev_strat = None
    expanding = None
    fill_predictor = None

    # Load pre-trained FillPredictor if requested
    if args.gbt_fills:
        from fill_predictor import FillPredictor
        print(f"Loading FillPredictor from {args.gbt_fills}...")
        fill_predictor = FillPredictor.load(args.gbt_fills)
        print("  Loaded.")

    if args.expanding or args.expanding_legacy:
        if args.expanding_legacy:
            from expanding_calibration import (
                ExpandingEventRates, ExpandingFillRates, LegacyMarketView)
            print("Loading legacy expanding-window estimators...")
            leg_event = ExpandingEventRates(conn)
            leg_fill = ExpandingFillRates(conn)
            legacy_view = LegacyMarketView(leg_event, leg_fill)
            expanding = {'legacy': True, 'legacy_view': legacy_view}
        else:
            from market_view import preload_observations, preload_fill_data
            print("Preloading data for expanding-window calibration...")
            observations, classifications = preload_observations(conn)
            if fill_predictor is not None:
                expanding = {
                    'observations': observations,
                    'classifications': classifications,
                    'fill_predictor': fill_predictor,
                    'use_flow': True,  # triggers joint search path
                }
            elif use_flow:
                from market_view import preload_trades
                print("Preloading trade tape for FlowModel...")
                trades_by_ticker, settled_markets = preload_trades(conn, max_tickers=20000)
                expanding = {
                    'observations': observations,
                    'trades_by_ticker': trades_by_ticker,
                    'settled_markets': settled_markets,
                    'classifications': classifications,
                    'use_flow': True,
                }
            else:
                fill_data = preload_fill_data(conn)
                from view_bootstrap import build_view_factory
                expanding = {
                    'view_factory': build_view_factory(
                        observations, classifications, fill_data=fill_data),
                }
        # Dummy edge_lookup — expanding mode handles its own calibration
        from trader import EdgeLookup
        edge_lookup = EdgeLookup()

    # Preload replay trades for tape-based fills
    replay_trades_data = {}
    if use_flow and expanding:
        # Load trade tape for the replay universe (tickers from candle data)
        # This happens after candle loading in replay(), but we need to
        # set up the expanding dict to carry it through
        expanding['replay_trades'] = {}  # will be populated in replay if needed

    track = replay(conn, edge_lookup, params=params,
                   starting_capital_cents=args.capital,
                   fill_model=fm,
                   use_synthetic_spread=args.synthetic_spread,
                   ev_strategy=ev_strat,
                   replay_after=args.after,
                   expanding=expanding,
                   use_tape_fills=args.tape_fills,
                   use_flow_model=args.flow_fills)
    conn.close()

    label = ("EV expanding-legacy" if args.expanding_legacy else
             "EV expanding" if args.expanding else "production strategy")
    print(f"\n{'='*70}")
    print(f"REPLAY RESULTS ({label}, {args.capture_rate:.0%} capture rate)")
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

    decomp = track.prediction_decomposition()
    if decomp:
        print("\n  Prediction decomposition:")
        for label, d in decomp.items():
            if not d:
                continue
            print(f"    [{label}] n={d['n']}:")
            print(f"      P(win) predicted: {d['avg_p_win']:.1%}  "
                  f"actual: {d['actual_win_rate']:.1%}  "
                  f"gap: {d['p_win_gap']:+.1%}")
            print(f"      P(fill): {d['avg_p_fill']:.1%}  "
                  f"edge: {d['avg_edge']:.2%}  "
                  f"return: {d['avg_return']:.2%}")

        # Also decompose by generating_process
        for gp, sub in sorted(track.by_category().items(), key=lambda x: -len(x[1])):
            gp_decomp = sub.prediction_decomposition()
            d = gp_decomp.get('all', {})
            if not d:
                continue
            print(f"    [{gp}] n={d['n']}: "
                  f"P(win) {d['avg_p_win']:.1%} vs {d['actual_win_rate']:.1%} "
                  f"({d['p_win_gap']:+.1%})  "
                  f"edge={d['avg_edge']:.2%} return={d['avg_return']:.2%}")

    if args.csv:
        _write_trade_csv(track, args.csv, args.capital)


def _write_trade_csv(track, path, starting_capital_cents):
    """Write trade-level CSV with running P&L."""
    import csv
    trades = sorted(track.trades, key=lambda t: t.entry_date or '')
    cum_pnl = 0
    equity = starting_capital_cents

    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow([
            'trade_num', 'date', 'ticker', 'event', 'series',
            'side', 'entry_price', 'contracts', 'won', 'exit_price',
            'fee_cents', 'pnl_cents', 'cum_pnl_cents', 'equity_cents',
            'return_pct', 'cum_return_pct', 'days_held',
            'generating_process', 'topic',
        ])
        for i, t in enumerate(trades, 1):
            cum_pnl += t.pnl_cents
            equity = starting_capital_cents + cum_pnl
            w.writerow([
                i, t.entry_date, t.ticker, t.event_ticker, t.series,
                t.side, t.entry_price, t.contracts,
                'W' if t.won else 'L', t.exit_price,
                t.fee_cents, t.pnl_cents, cum_pnl, equity,
                f'{t.return_on_capital:.2%}',
                f'{cum_pnl / starting_capital_cents:.2%}',
                f'{t.days_held:.2f}',
                t.generating_process, t.topic,
            ])

    print(f"\n  Wrote {len(trades)} trades to {path}")


if __name__ == "__main__":
    main()
