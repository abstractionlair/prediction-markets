"""EstimatorFactory implementations for existing estimators.

Bridges existing estimators (EventRateEstimator, FillRateEstimator) to the
framework's EstimatorFactory protocol. Each factory:
- Declares data requirements (features needed for calibration)
- Declares estimator dependencies
- Implements calibrate() receiving pre-filtered data only (no as_of)
- Provides a query_fn for bridging View's typed interface to the estimator's API

The query_fn is picked up by ViewFactory.build() and passed to
register_bound(), which stores it in the EstimatorFeature adapter.

Estimator imports are deferred to calibrate() so the framework module
has no import-time dependency on trading/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FillEstimate:
    """Fill probability estimate returned by the fill model query.

    Provides a uniform interface (.p_fill_won, .p_fill_lost) regardless
    of which fill model backend is in use.
    """
    p_fill_won: float
    p_fill_lost: float


class EventRateFactory:
    """EstimatorFactory for EventRateEstimator.

    Data requirements:
    - 'observations': list of observation objects (pre-filtered by as_of)
    - 'classifications': dict of series -> (generating_process, topic)

    Produces an EventRateEstimator calibrated on the provided observations.
    The estimator serves P(YES) lookups via get_event_rate().
    """

    def __init__(self, price_method=None):
        """
        Args:
            price_method: Which price method(s) to calibrate. None (default)
                calibrates all methods (bid, mid, ask, trade). Pass 'mid'
                to match legacy MarketView behavior.
        """
        self._price_method = price_method

    @property
    def name(self) -> str:
        return 'event_rate'

    @property
    def data_requirements(self) -> list[str]:
        return ['observations', 'classifications']

    @property
    def depends_on(self) -> list[str]:
        return []

    def calibrate(self, data: dict[str, Any],
                  dependencies: dict | None = None) -> Any:
        from event_rate import EventRateEstimator
        estimator = EventRateEstimator()
        estimator.set_classifications(data['classifications'])
        estimator.calibrate(data['observations'],
                            price_method=self._price_method)
        return estimator

    @staticmethod
    def query_fn(estimator, series, hours, **kwargs):
        """Bridge View.event_rate() → EventRateEstimator.get_event_rate().

        View passes: series=str, hours=float, plus optional price kwargs
        (bid_dollars, ask_dollars, trade_dollars, observed_price_dollars).
        """
        return estimator.get_event_rate(series, hours, **kwargs)


class ClassificationFactory:
    """EstimatorFactory for classification lookup.

    Classifications are a simple dict mapping series -> (gp, topic).
    They participate in the framework because the View queries them
    through the registry with temporal boundary enforcement.

    Data requirements:
    - 'classifications': dict of series -> (generating_process, topic)
    """

    @property
    def name(self) -> str:
        return 'classification'

    @property
    def data_requirements(self) -> list[str]:
        return ['classifications']

    @property
    def depends_on(self) -> list[str]:
        return []

    def calibrate(self, data: dict[str, Any],
                  dependencies: dict | None = None) -> Any:
        return dict(data['classifications'])

    @staticmethod
    def query_fn(classifications, series, **kwargs):
        """Bridge View.classification() → dict lookup."""
        return classifications.get(series)


class FillRateFactory:
    """EstimatorFactory for FillRateEstimator.

    Data requirements:
    - 'fill_data': dict of ticker -> {gp, topic, settled_at, result,
        candles: [{period_end, bid_cents, ask_cents, fill_candle}]}

    The inner FillRateEstimator serves (P(fill|won), P(fill|lost)) lookups
    by (gp, topic, time_bucket, relative_price, side).

    The query_fn converts from the View's fill_probability interface
    (side, limit_price, quantity, market_state) to the estimator's
    relative_price-based interface.
    """

    def __init__(self, n_price_steps: int = 10):
        self._n_price_steps = n_price_steps

    @property
    def name(self) -> str:
        return 'fill_model'

    @property
    def data_requirements(self) -> list[str]:
        return ['fill_data']

    @property
    def depends_on(self) -> list[str]:
        return []

    def calibrate(self, data: dict[str, Any],
                  dependencies: dict | None = None) -> Any:
        from fill_rate import FillRateEstimator
        estimator = FillRateEstimator()
        estimator.calibrate(data['fill_data'],
                            n_price_steps=self._n_price_steps)
        return estimator

    @staticmethod
    def query_fn(estimator, side, limit_price, quantity,
                 bid, ask, hours_to_settlement,
                 generating_process, topic, **kwargs):
        """Bridge View.fill_probability() → FillRateEstimator.get_fill_rates().

        Converts absolute limit_price to relative_price using bid/ask spread.
        FillRateEstimator is size-independent (quantity is unused).
        Returns FillEstimate or None.
        """
        if side == 'no':
            s_bid, s_ask = 100 - ask, 100 - bid
        else:
            s_bid, s_ask = bid, ask
        spread = s_ask - s_bid
        if spread <= 0:
            return None
        relative_price = (limit_price - s_bid) / spread
        result = estimator.get_fill_rates(
            generating_process, topic, hours_to_settlement,
            relative_price, side)
        if result is None:
            return None
        return FillEstimate(p_fill_won=result[0], p_fill_lost=result[1])
