"""ObservationsFeature: hourly candle observations joined to settlements.

This is the primary data source for EventRateEstimator calibration.
Each observation represents one hourly candle for a market that eventually
settled, with the settlement outcome attached.

Availability time: settled_at (when the market settled, which is when
we reliably know the outcome). Temporal filter: settled_at < as_of.
"""

from dataclasses import dataclass
from datetime import datetime

from framework.feature import StoredFeature


@dataclass(slots=True)
class Observation:
    """A single price observation for a settled market.

    Compatible with EventRateEstimator.calibrate() which expects objects
    with these attributes.
    """
    ticker: str
    series: str
    settled_at: datetime
    yes_bid: float
    yes_ask: float
    yes_mid: float
    trade_price: float
    result_yes: bool
    hours_to_settlement: float
    generating_process: str
    topic: str


class ObservationsFeature(StoredFeature):
    """Hourly candle observations joined to settlements and classifications.

    Joins kalshi_hourly_candles with kalshi_settled_markets and
    market_classifications to produce Observation objects suitable
    for EventRateEstimator calibration.

    Filters applied:
    - settled_at < as_of (temporal boundary, strict <)
    - Only 'yes'/'no' results (excludes voided/pending)
    - Candle must be before settlement (period_end < settled_at)
    - Valid bid/ask (both > 0, spread <= 10 cents)
    - Classification must exist (generating_process and topic not null)
    """

    # Quality filters matching the existing preload_observations logic
    MAX_SPREAD = 0.10
    MIN_HOURS = 0.0

    def __init__(self, conn_factory):
        super().__init__(
            name='observations',
            table='prediction_markets.kalshi_hourly_candles',
            availability_column='settled_at',
            conn_factory=conn_factory,
        )

    def query(self, as_of: datetime, **params) -> list[Observation]:
        """Return all observations from markets settled before as_of.

        Returns a list of Observation objects, filtered and validated.
        The result is compatible with EventRateEstimator.calibrate().
        """
        conn = self._conn_factory()
        cur = conn.cursor("obs_feature")
        cur.itersize = 100_000
        try:
            cur.execute("""
                SELECT hc.ticker,
                       split_part(hc.ticker, '-', 1) AS series,
                       sm.settled_at,
                       hc.yes_bid_close,
                       hc.yes_ask_close,
                       (hc.yes_bid_close + hc.yes_ask_close) / 2.0 AS yes_mid,
                       COALESCE(hc.price_close, 0) AS trade_price,
                       sm.result,
                       EXTRACT(EPOCH FROM (sm.settled_at::timestamptz - hc.period_end))
                           / 3600.0 AS hours_to_settlement,
                       mc.generating_process,
                       mc.topic
                FROM prediction_markets.kalshi_hourly_candles hc
                JOIN prediction_markets.kalshi_settled_markets sm
                    ON sm.ticker = hc.ticker
                JOIN prediction_markets.market_classifications mc
                    ON mc.series_ticker = split_part(hc.ticker, '-', 1)
                WHERE sm.result IN ('yes', 'no')
                  AND sm.settled_at IS NOT NULL
                  AND sm.settled_at < %s
                  AND hc.period_end < sm.settled_at
                  AND hc.yes_bid_close > 0
                  AND hc.yes_ask_close > 0
                  AND (hc.yes_ask_close - hc.yes_bid_close) <= %s
                  AND mc.generating_process IS NOT NULL
                  AND mc.topic IS NOT NULL
            """, (as_of, self.MAX_SPREAD))

            observations = []
            for row in cur:
                (ticker, series, settled_at, bid, ask, mid, trade,
                 result, hours, gp, topic) = row
                if hours is None or hours < self.MIN_HOURS:
                    continue
                mid_f = float(mid)
                if mid_f <= 0 or mid_f >= 1:
                    continue
                observations.append(Observation(
                    ticker=ticker,
                    series=series,
                    settled_at=settled_at,
                    yes_bid=float(bid),
                    yes_ask=float(ask),
                    yes_mid=mid_f,
                    trade_price=float(trade) if trade else 0.0,
                    result_yes=(result == 'yes'),
                    hours_to_settlement=float(hours),
                    generating_process=gp,
                    topic=topic,
                ))
            return observations
        finally:
            cur.close()

    def __repr__(self) -> str:
        return "ObservationsFeature()"
