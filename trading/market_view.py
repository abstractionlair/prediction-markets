"""
MarketView: the temporal boundary for strategies and calibrated components.

A MarketView(as_of=T) provides all market data and calibrated estimates
using only information available before T. Strategies receive a MarketView
and can only access data through it — no database connections, no imports
of calibration modules, no side channels.

The temporal boundary is enforced structurally: estimators receive pre-filtered
data, not the full dataset plus a cutoff date. An estimator cannot look ahead
because it never sees future data.

Usage in backtesting:
    all_obs, all_fill_data, classifications = preload(conn)
    for day in trading_days:
        view = MarketView(as_of=day,
                          all_observations=all_obs,
                          all_fill_data=all_fill_data,
                          classifications=classifications)
        opportunities = strategy.scan(events, view)

Usage in production:
    view = MarketView.from_db(conn)  # as_of = now, loads latest calibrations
"""

from collections import defaultdict
from datetime import datetime, timezone

from trading.cost_model import KALSHI_COSTS
from trading.event_rate import EventRateEstimator
from trading.fill_rate import FillRateEstimator


class MarketView:
    """Temporally-bounded view of all market data and calibrated estimates.

    Every estimate served by this view was calibrated using only data from
    markets that settled before as_of. The filtering happens here; downstream
    estimators never see unfiltered data.

    Supports three fill model backends:
    - FillPredictor (GBT): queue-aware gradient boosted trees. Size-dependent,
      uses OI for queue depth. Used when fill_predictor is provided.
    - FlowModel (V2): trade-tape-based, size-dependent, with trailing volume.
      Used when all_trades and settled_markets are provided.
    - FillRateEstimator (legacy): candle-based, size-independent.
      Used when all_fill_data is provided.
    """

    def __init__(self, as_of, all_observations, all_fill_data=None,
                 classifications=None, fill_price_steps=10, costs=KALSHI_COSTS,
                 all_trades=None, settled_markets=None,
                 fill_predictor=None):
        self.as_of = as_of
        self.costs = costs
        self._flow_model = None
        self._fill_rates = None
        self._fill_predictor = fill_predictor

        # ── Filter observations to before as_of ────────────────────
        filtered_obs = [o for o in all_observations if o.settled_at < as_of]

        # ── Calibrate event rate estimator ─────────────────────────
        self._event_rates = EventRateEstimator()
        self._event_rates.set_classifications(classifications or {})
        self._event_rates.calibrate(filtered_obs, price_method='mid')

        # ── Calibrate fill model ───────────────────────────────────
        if fill_predictor is not None:
            # GBT: pre-trained FillPredictor (queue-aware)
            self._n_fill_tickers = 0  # not tracked for pre-trained models
        elif all_trades is not None and all_trades:
            # V2: FlowModel from trade tape
            from trading.flow_model import FlowModel
            self._flow_model = FlowModel.calibrate(
                all_trades, settled_markets or {}, classifications or {},
                as_of=as_of)
            self._n_fill_tickers = len(all_trades)
        elif all_fill_data is not None:
            # Legacy: FillRateEstimator from candle data
            filtered_fill = {t: d for t, d in all_fill_data.items()
                             if d['settled_at'] < as_of}
            self._fill_rates = FillRateEstimator()
            self._fill_rates.calibrate(filtered_fill, n_price_steps=fill_price_steps)
            self._n_fill_tickers = len(filtered_fill)
        else:
            self._n_fill_tickers = 0

        self._n_obs = len(filtered_obs)

    # ── Strategy-facing interface ─────────────────────────────────

    def event_rate(self, series, hours_to_settlement,
                   observed_price_dollars=None, *,
                   bid_dollars=None, ask_dollars=None, trade_dollars=None):
        """(P(YES), SE, n_markets) given series, time, observed prices. Returns None if no data.

        Accepts either observed_price_dollars (backward-compatible, mid only)
        or bid_dollars + ask_dollars for multi-method combination.
        """
        return self._event_rates.get_event_rate(
            series, hours_to_settlement,
            observed_price_dollars=observed_price_dollars,
            bid_dollars=bid_dollars, ask_dollars=ask_dollars,
            trade_dollars=trade_dollars)

    def fill_estimate(self, gp, topic, hours, side, quantity,
                      limit_price_cents, trailing_volume,
                      bid_cents=None, ask_cents=None, open_interest=None):
        """Fill probability estimate for a proposed order.

        Returns an object with .p_fill_won and .p_fill_lost, or None.

        Uses FillPredictor (GBT) if available, else FlowModel (V2).
        FillPredictor requires bid_cents, ask_cents, open_interest.
        """
        if self._fill_predictor is not None:
            if bid_cents is None or ask_cents is None:
                return None
            return self._fill_predictor.estimate(
                side, limit_price_cents, quantity,
                bid_cents, ask_cents, hours,
                gp, topic, trailing_volume, open_interest or 0)
        if self._flow_model is not None:
            return self._flow_model.estimate(gp, topic, hours, side, quantity,
                                              limit_price_cents, trailing_volume)
        return None

    def fill_rates(self, gp, topic, hours_to_settlement, relative_price, side,
                    bid_cents=None, ask_cents=None, open_interest=None):
        """(P(fill|won), P(fill|lost)) for a limit order. Returns None if no data.

        Uses FillRateEstimator (legacy) if available, else FillPredictor or
        FlowModel as fallback. FillPredictor requires bid_cents/ask_cents.
        """
        if self._fill_rates is not None:
            return self._fill_rates.get_fill_rates(
                gp, topic, hours_to_settlement, relative_price, side)
        if self._fill_predictor is not None and bid_cents is not None:
            # Reconstruct limit price from relative_price + bid/ask
            if side == 'no':
                s_bid, s_ask = 100 - ask_cents, 100 - bid_cents
            else:
                s_bid, s_ask = bid_cents, ask_cents
            limit = s_bid + int(round(relative_price * (s_ask - s_bid)))
            est = self._fill_predictor.estimate(
                side, limit, 1, bid_cents, ask_cents,
                hours_to_settlement, gp, topic, 0, open_interest or 0)
            if est is None:
                return None
            return (est.p_fill_won, est.p_fill_lost)
        if self._flow_model is not None:
            est = self._flow_model.estimate(gp, topic, hours_to_settlement,
                                             side, 1, 90, 0)
            if est is None:
                return None
            return (est.p_fill_won, est.p_fill_lost)
        return None

    def classification(self, series):
        """(generating_process, topic) for a series, or None."""
        return self._event_rates.get_classification(series)

    def maker_fee(self, price_cents, contracts):
        """Maker fee in cents."""
        return self.costs.maker_fee(price_cents, contracts)

    # ── View API compatibility ───────────────────────────────────
    # These methods allow EVStrategy (which now uses the framework View
    # interface) to work with MarketView during the migration period.

    def fill_probability(self, side, limit_price, quantity, market_state):
        """Bridge to View.fill_probability() — delegates to fill_estimate/fill_rates.

        Returns an object with .p_fill_won and .p_fill_lost, or None.
        """
        gp = market_state['generating_process']
        topic = market_state['topic']
        hours = market_state['hours_to_settlement']
        bid = market_state['bid']
        ask = market_state['ask']
        trailing_vol = market_state.get('trailing_volume', 0)
        oi = market_state.get('open_interest', 0)

        # Try size-dependent fill model first (FlowModel, FillPredictor)
        result = self.fill_estimate(gp, topic, hours, side, quantity,
                                    limit_price, trailing_vol,
                                    bid_cents=bid, ask_cents=ask,
                                    open_interest=oi)
        if result is not None:
            return result

        # Fall back to FillRateEstimator (size-independent)
        if side == 'no':
            s_bid, s_ask = 100 - ask, 100 - bid
        else:
            s_bid, s_ask = bid, ask
        spread = s_ask - s_bid
        if spread <= 0:
            return None
        relative_price = (limit_price - s_bid) / spread

        rates = self.fill_rates(gp, topic, hours, relative_price, side,
                                bid_cents=bid, ask_cents=ask,
                                open_interest=oi)
        if rates is None:
            return None

        from framework.factories import FillEstimate
        return FillEstimate(p_fill_won=rates[0], p_fill_lost=rates[1])

    def cost(self, price_cents, contracts, is_maker=True):
        """Bridge to View.cost() — delegates to CostModel."""
        if is_maker:
            return self.costs.maker_fee(price_cents, contracts)
        return self.costs.taker_fee(price_cents, contracts)

    @classmethod
    def from_db(cls, conn, as_of=None, use_flow_model=False):
        """Construct a MarketView from the database.

        Loads all observations and fill/trade data, calibrates estimators.
        For live trading: call once at startup, rebuild periodically.

        If use_flow_model=True, loads trade tape for FlowModel (V2) instead
        of candle data for FillRateEstimator.
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        observations, classifications = preload_observations(conn)
        if use_flow_model:
            trades, settled = preload_trades(conn)
            return cls(as_of=as_of, all_observations=observations,
                       all_trades=trades, settled_markets=settled,
                       classifications=classifications)
        else:
            fill_data = preload_fill_data(conn)
            return cls(as_of=as_of, all_observations=observations,
                       all_fill_data=fill_data, classifications=classifications)

    @property
    def stats(self):
        """Summary of what this view contains (for logging)."""
        n_cells = sum(len(v) for v in self._event_rates.rates.values())
        if self._fill_predictor is not None:
            n_fill_cells = 0
            fill_label = "GBT fill predictor"
        elif self._flow_model is not None:
            n_fill_cells = len(self._flow_model.flow_table)
            fill_label = "flow CDFs"
        elif self._fill_rates is not None:
            n_fill_cells = len(self._fill_rates.rates)
            fill_label = "fill cells"
        else:
            n_fill_cells = 0
            fill_label = "fill cells"
        return (f"as_of={self.as_of.date()}, "
                f"{self._n_obs:,} obs, {self._n_fill_tickers:,} fill tickers, "
                f"{n_cells} rate cells, {n_fill_cells} {fill_label}")


def preload_observations(conn):
    """Load all observations for expanding-window use.

    Returns (observations, classifications) where observations is a list
    of Observation objects with settled_at attached.
    """
    cur = conn.cursor()

    # Classifications
    cur.execute("""
        SELECT series_ticker, generating_process, topic
        FROM prediction_markets.market_classifications
        WHERE generating_process IS NOT NULL AND topic IS NOT NULL
    """)
    classifications = {row[0]: (row[1], row[2]) for row in cur}

    # Load observations with settled_at for temporal filtering
    print("  Loading observations...", end="", flush=True)
    cur2 = conn.cursor("mv_obs")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT hc.ticker,
               split_part(hc.ticker, '-', 1) AS series,
               sm.settled_at::timestamptz AS settled_at,
               hc.yes_bid_close AS yes_bid,
               hc.yes_ask_close AS yes_ask,
               (hc.yes_bid_close + hc.yes_ask_close) / 2.0 AS yes_mid,
               COALESCE(hc.price_close, 0) AS trade_price,
               sm.result,
               EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - hc.period_end)) / 3600.0
                   AS hours_to_settlement
        FROM prediction_markets.kalshi_hourly_candles hc
        JOIN prediction_markets.kalshi_settled_markets sm ON sm.ticker = hc.ticker
        LEFT JOIN prediction_markets.market_classifications mc
            ON mc.series_ticker = split_part(hc.ticker, '-', 1)
        WHERE sm.result IN ('yes', 'no')
          AND sm.settled_at != ''
          AND hc.period_end < sm.settled_at::timestamptz
          AND hc.yes_bid_close > 0 AND hc.yes_ask_close > 0
          AND (hc.yes_ask_close - hc.yes_bid_close) <= 0.10
          AND mc.generating_process IS NOT NULL
    """)

    # Use a lightweight object to avoid importing calibration.py's Observation
    class Obs:
        __slots__ = ('ticker', 'series', 'settled_at', 'yes_bid', 'yes_ask',
                     'yes_mid', 'trade_price', 'result_yes', 'hours_to_settlement',
                     'generating_process', 'topic')

    observations = []
    for row in cur2:
        (ticker, series, settled_at, bid, ask, mid, trade,
         result, hours) = row
        if hours is None or hours < 0:
            continue
        mid_f = float(mid)
        if mid_f <= 0 or mid_f >= 1:
            continue
        cl = classifications.get(series)
        if cl is None:
            continue
        o = Obs()
        o.ticker = ticker
        o.series = series
        o.settled_at = settled_at
        o.yes_bid = float(bid)
        o.yes_ask = float(ask)
        o.yes_mid = mid_f
        o.trade_price = float(trade) if trade else 0.0
        o.result_yes = (result == 'yes')
        o.hours_to_settlement = float(hours)
        o.generating_process = cl[0]
        o.topic = cl[1]
        observations.append(o)
    cur2.close()
    cur.close()
    print(f" {len(observations):,}")

    return observations, classifications


def preload_fill_data(conn):
    """Load all fill calibration data for expanding-window use.

    Returns dict of ticker -> {gp, topic, settled_at, result, candles: [...]}.
    """
    from trading.fill_model import CandleData

    cur = conn.cursor()

    # Classifications
    cur.execute("""
        SELECT series_ticker, generating_process, topic
        FROM prediction_markets.market_classifications
        WHERE generating_process IS NOT NULL AND topic IS NOT NULL
    """)
    classifications = {row[0]: (row[1], row[2]) for row in cur}

    # Settlements
    cur.execute("""
        SELECT ticker, event_ticker, result, settled_at
        FROM prediction_markets.kalshi_settled_markets
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
        series = event.split('-')[0]
        cl = classifications.get(series)
        if cl is None:
            continue
        settled[ticker] = {
            'series': series, 'gp': cl[0], 'topic': cl[1],
            'settled_at': sdt, 'result': result,
        }

    # Candles
    print("  Loading fill data...", end="", flush=True)
    cur2 = conn.cursor("mv_fill")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT ticker, period_end,
               yes_bid_close, yes_ask_close,
               yes_bid_high, yes_ask_low,
               COALESCE(volume, 0),
               COALESCE(price_high, 0), COALESCE(price_low, 0)
        FROM prediction_markets.kalshi_hourly_candles
        WHERE yes_bid_close IS NOT NULL AND yes_ask_close IS NOT NULL
          AND yes_bid_close > 0 AND yes_ask_close > 0
        ORDER BY ticker, period_end
    """)

    ticker_candles = defaultdict(list)
    n = 0
    for (ticker, period_end, bid_close, ask_close,
         bid_high, ask_low, vol, p_high, p_low) in cur2:
        if ticker not in settled:
            continue
        ticker_candles[ticker].append({
            'period_end': period_end,
            'bid_cents': int(round(float(bid_close) * 100)),
            'ask_cents': int(round(float(ask_close) * 100)),
            'fill_candle': CandleData(
                yes_bid_high=int(round(float(bid_high) * 100)),
                yes_ask_low=int(round(float(ask_low) * 100)),
                volume=vol,
                price_high=int(round(float(p_high) * 100)) if p_high else 0,
                price_low=int(round(float(p_low) * 100)) if p_low else 0,
            ),
        })
        n += 1
    cur2.close()
    cur.close()

    # Merge
    fill_data = {}
    for ticker, candles in ticker_candles.items():
        candles.sort(key=lambda c: c['period_end'])
        fill_data[ticker] = {**settled[ticker], 'candles': candles}

    print(f" {n:,} candles, {len(fill_data):,} tickers")
    return fill_data


def preload_trades(conn, max_tickers=None):
    """Load trade tape and settled market data for FlowModel calibration.

    Args:
        conn: database connection
        max_tickers: if set, randomly sample this many tickers to cap memory.
            At 5K tickers, ~600K trades (~200 MB). At 242K, ~28M (~10 GB).
            Recommended: 10000-20000 for good calibration within memory limits.

    Returns:
        trades_by_ticker: dict of ticker → list of
            (created_time, count, yes_price, taker_side)
            sorted by created_time ascending.
        settled: dict of ticker → (settled_at, result, event_ticker)

    Only loads trades for settled, classified markets that have at least
    one trade at tail prices (yes_price >= 0.85 or yes_price <= 0.15).
    Within qualifying tickers, ALL trades are loaded for trailing volume.
    """
    cur = conn.cursor()

    # Classifications
    cur.execute("""
        SELECT series_ticker, generating_process, topic
        FROM prediction_markets.market_classifications
        WHERE generating_process IS NOT NULL AND topic IS NOT NULL
    """)
    classifications = {row[0]: (row[1], row[2]) for row in cur}

    # Settled markets
    print("  Loading settled markets for trades...", end="", flush=True)
    cur.execute("""
        SELECT ticker, settled_at::timestamptz, result, event_ticker
        FROM prediction_markets.kalshi_settled_markets
        WHERE result IN ('yes', 'no') AND settled_at != '' AND event_ticker != ''
    """)
    settled = {}
    for ticker, settled_at, result, event_ticker in cur:
        series = event_ticker.split('-')[0]
        if series not in classifications:
            continue
        settled[ticker] = (settled_at, result, event_ticker)
    print(f" {len(settled):,}")

    # Find tickers with tail-price trades
    print("  Finding tail tickers...", end="", flush=True)
    settled_tickers = list(settled.keys())
    # Use temp table for efficient filtering
    cur.execute("CREATE TEMP TABLE _settled_tickers (ticker text PRIMARY KEY)")
    from psycopg2.extras import execute_values
    # Batch insert in chunks for large ticker sets
    chunk_size = 10000
    for i in range(0, len(settled_tickers), chunk_size):
        chunk = settled_tickers[i:i + chunk_size]
        execute_values(cur, "INSERT INTO _settled_tickers VALUES %s",
                       [(t,) for t in chunk])

    cur.execute("""
        SELECT DISTINCT t.ticker
        FROM prediction_markets.kalshi_trades t
        JOIN _settled_tickers s ON s.ticker = t.ticker
        WHERE t.yes_price >= 0.85 OR t.yes_price <= 0.15
    """)
    tail_tickers = {row[0] for row in cur}
    cur.execute("DROP TABLE _settled_tickers")
    print(f" {len(tail_tickers):,}")

    # Sample if too many tickers for memory
    if max_tickers and len(tail_tickers) > max_tickers:
        import random
        random.seed(42)
        tail_tickers = set(random.sample(sorted(tail_tickers), max_tickers))
        print(f"  Sampled {max_tickers:,} tickers for memory")

    # Load all trades for tail tickers
    print("  Loading trades...", end="", flush=True)

    # Create temp table for tail tickers
    cur.execute("CREATE TEMP TABLE _tail_tickers (ticker text PRIMARY KEY)")
    tail_list = sorted(tail_tickers)
    for i in range(0, len(tail_list), chunk_size):
        chunk = tail_list[i:i + chunk_size]
        execute_values(cur, "INSERT INTO _tail_tickers VALUES %s",
                       [(t,) for t in chunk])

    cur2 = conn.cursor("mv_trades")
    cur2.itersize = 100000
    cur2.execute("""
        SELECT t.ticker, t.created_time, t.count, t.yes_price, t.taker_side
        FROM prediction_markets.kalshi_trades t
        JOIN _tail_tickers tt ON tt.ticker = t.ticker
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

    cur.execute("DROP TABLE _tail_tickers")
    cur.close()
    print(f" {n:,} trades, {len(trades_by_ticker):,} tickers")

    return trades_by_ticker, settled
