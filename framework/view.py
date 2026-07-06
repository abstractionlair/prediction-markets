"""View: the capability boundary between infrastructure and strategy code.

A View is a frozen-in-time projection of all features. Strategies and
dependent models receive a View and can only access data through it.
The temporal boundary (as_of) is private -- consumers cannot inspect
or change it.

Views are constructed by the environment (ViewFactory, Runner), never
by strategy or model code.

Enforcement levels (spec Section 6.2):
- Validated: availability_time checked at construction
- Conventional: as_of privacy, no DB access (Python lacks access control)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from framework.factories import FillEstimate
    from framework.feature import FeatureRegistry


class TemporalBoundaryError(Exception):
    """Raised when a feature's availability_time exceeds the view's as_of."""


class View:
    """Frozen-in-time projection of all features.

    The single handle strategies and dependent models receive.
    Mediates all access to data AND model outputs.

    Structural invariants enforced at construction:
    1. Every estimator's availability_time <= self.as_of
    2. Every data query is filtered by self.as_of
    3. Consumers cannot access self.as_of (not part of the interface)
    4. Consumers cannot access the underlying registry or database
    """

    def __init__(self, as_of: datetime, registry: FeatureRegistry,
                 costs=None):
        """Construct a View. Called by ViewFactory, not by consumers.

        Args:
            as_of: Temporal boundary. All queries filter to before this time.
            registry: Populated feature registry (base features + estimators).
            costs: CostModel instance. If None, cost() raises.
        """
        self._as_of = as_of
        self._registry = registry
        self._costs = costs
        self._validate()

    def _validate(self):
        """Verify all features respect the temporal boundary.

        Any feature with an availability_time attribute (e.g., BoundEstimator
        registered as a feature) must have availability_time <= as_of.
        """
        for name, feature in self._registry.items():
            avail = getattr(feature, 'availability_time', None)
            if avail is not None and avail > self._as_of:
                raise TemporalBoundaryError(
                    f"Feature '{name}' availability_time={avail} "
                    f"exceeds view as_of={self._as_of}"
                )

    # ── Strategy-facing interface ─────────────────────────────────

    def event_rate(self, series: str, hours: float,
                   **price_kwargs) -> tuple | None:
        """P(YES | series, hours_to_settlement, observed_prices).

        Returns (p_yes, standard_error, n_markets) or None if insufficient data.
        """
        return self._registry.query('event_rate', self._as_of,
                                    series=series, hours=hours, **price_kwargs)

    def fill_probability(self, side: str, limit_price: int, quantity: int,
                         market_state: dict) -> FillEstimate | None:
        """P(fill | side, limit_price, quantity, market_state).

        Returns FillEstimate (with .p_fill_won, .p_fill_lost), or None.

        market_state required keys:
            bid: int            YES-side best bid in cents
            ask: int            YES-side best ask in cents
            hours_to_settlement: float
            generating_process: str
            topic: str

        market_state optional keys (used by some fill model backends):
            trailing_volume: int
            open_interest: int
        """
        return self._registry.query('fill_model', self._as_of,
                                    side=side, limit_price=limit_price,
                                    quantity=quantity, **market_state)

    def classification(self, series: str) -> tuple[str, str] | None:
        """(generating_process, topic) for a series ticker, or None."""
        return self._registry.query('classification', self._as_of,
                                    series=series)

    def cost(self, price_cents: int, contracts: int,
             is_maker: bool = True) -> int:
        """Transaction cost in cents. Not temporally bounded (fee schedule is static)."""
        if self._costs is None:
            raise RuntimeError("No CostModel configured for this View")
        if is_maker:
            return self._costs.maker_fee(price_cents, contracts)
        return self._costs.taker_fee(price_cents, contracts)

    # ── Generic query (experimentation) ───────────────────────────

    def query(self, feature_name: str, **params) -> Any:
        """Generic feature query. Prefer typed methods for production use.

        Available for experimentation and for features that haven't yet
        earned a permanent View method.
        """
        return self._registry.query(feature_name, self._as_of, **params)

    # ── Diagnostics (for environment/logging, not for strategies) ─

    @property
    def stats(self) -> str:
        """Summary of what this view contains."""
        n_features = len(self._registry)
        n_bounded = sum(
            1 for _, f in self._registry.items()
            if hasattr(f, 'availability_time')
        )
        return (f"as_of={self._as_of.date()}, "
                f"{n_features} features ({n_bounded} temporally bounded)")

    def __repr__(self) -> str:
        return f"View(as_of={self._as_of.date()}, {len(self._registry)} features)"
