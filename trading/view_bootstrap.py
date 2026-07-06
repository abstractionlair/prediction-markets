"""Build a ViewFactory from the database.

Bridges the database preloading functions (from market_view.py) to the
framework's ViewFactory. This is the replacement for MarketView.from_db().

Usage (live trading):
    factory = build_view_factory_from_db(conn)
    view = factory.build_live()

Usage (expanding-window replay):
    factory = build_view_factory_from_db(conn)
    for day in trading_days:
        view = factory.build(as_of=day)
        strategy = EVStrategy(view, params=params)
"""

from __future__ import annotations

from framework.factories import (
    ClassificationFactory, EventRateFactory, FillRateFactory,
)
from framework.feature import FeatureRegistry, PreloadedFeature
from framework.view_factory import ViewFactory


def build_view_factory(observations, classifications,
                       fill_data=None, costs=None,
                       price_method='mid') -> ViewFactory:
    """Build a ViewFactory from preloaded data.

    Args:
        observations: List of observation objects with .settled_at attribute.
        classifications: Dict of series -> (generating_process, topic).
        fill_data: Optional dict of ticker -> {settled_at, gp, topic, result,
                   candles: [...]}.  If None, fill_model is not registered
                   and View.fill_probability() will raise KeyError.
        costs: CostModel instance. If None, uses Kalshi defaults.
        price_method: Price method for EventRateEstimator ('mid', 'bid', etc).

    Returns:
        ViewFactory ready for build(as_of=...) or build_live().
    """
    if costs is None:
        from cost_model import KALSHI_COSTS
        costs = KALSHI_COSTS

    registry = FeatureRegistry()

    # Observations: temporal filter by settled_at
    registry.register(PreloadedFeature(
        'observations', observations,
        filter_fn=lambda data, as_of: [o for o in data if o.settled_at < as_of],
    ))

    # Classifications: no temporal filtering (static lookup)
    registry.register(PreloadedFeature('classifications', classifications))

    # Fill data: temporal filter by settled_at
    factories = [
        EventRateFactory(price_method=price_method),
        ClassificationFactory(),
    ]
    if fill_data is not None:
        registry.register(PreloadedFeature(
            'fill_data', fill_data,
            filter_fn=lambda data, as_of: {
                t: d for t, d in data.items() if d['settled_at'] < as_of
            },
        ))
        factories.append(FillRateFactory())

    return ViewFactory(
        registry=registry,
        factories=factories,
        costs=costs,
    )


def build_view_factory_from_db(conn, use_flow_model=False,
                               costs=None,
                               price_method='mid') -> ViewFactory:
    """Build a ViewFactory by preloading data from the database.

    This replaces MarketView.from_db() as the entry point for constructing
    temporally-bounded views.

    Args:
        conn: Database connection.
        use_flow_model: If True, preload trade tape for FlowModel (not yet
                        supported — raises NotImplementedError). The default
                        FillRateEstimator path is the production path.
        costs: CostModel instance. If None, uses Kalshi defaults.
        price_method: Price method for EventRateEstimator ('mid', etc).

    Returns:
        ViewFactory ready for build(as_of=...) or build_live().
    """
    if use_flow_model:
        raise NotImplementedError(
            "FlowModel integration with ViewFactory is not yet implemented. "
            "Use the FillRateEstimator path (use_flow_model=False)."
        )

    from market_view import preload_observations, preload_fill_data

    observations, classifications = preload_observations(conn)
    fill_data = preload_fill_data(conn)

    return build_view_factory(
        observations, classifications,
        fill_data=fill_data,
        costs=costs,
        price_method=price_method,
    )
