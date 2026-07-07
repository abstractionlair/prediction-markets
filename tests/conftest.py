"""Shared test fixtures for prediction-markets tests.

Tests import the project packages directly (``trading.*``, ``framework.*``,
...), so the package must be importable — ``pip install -e .`` — rather than
relying on sys.path manipulation here.
"""

import os

import pytest

from trading.strategy import Opportunity, TradePair, edge_per_day


def pytest_collection_modifyitems(config, items):
    """Skip db-marked tests when no database DSN is configured.

    Some DB fixtures connect unconditionally; without this hook a missing
    CLAUDE_HUB_PG_DSN surfaces as setup ERRORs instead of skips, so a
    default ``pytest`` run is red on any machine without the database.
    """
    if os.environ.get("CLAUDE_HUB_PG_DSN"):
        return
    skip_db = pytest.mark.skip(reason="CLAUDE_HUB_PG_DSN not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


def make_opportunity(
    ticker="KXBTCD-26MAR2717-T60399.99",
    event_ticker="KXBTCD-26MAR2717",
    series="KXBTCD",
    side="yes",
    bid_price=90,
    best_bid=89,
    best_ask=92,
    spread=3,
    edge=0.025,
    days_to_settle=1.0,
    edge_per_day=0.025,
    generating_process="continuous_underlyer",
    topic="financial",
    title="BTC above $60,399.99?",
) -> Opportunity:
    return Opportunity(
        ticker=ticker, event_ticker=event_ticker, series=series,
        side=side, bid_price=bid_price,
        best_bid=best_bid, best_ask=best_ask, spread=spread,
        edge=edge, days_to_settle=days_to_settle, edge_per_day=edge_per_day,
        generating_process=generating_process, topic=topic, title=title,
    )


def make_pair(
    yes_ticker="KXBTCD-26MAR2717-T60399.99",
    no_ticker="KXBTCD-26MAR2717-T68299.99",
    event_ticker="KXBTCD-26MAR2717",
    series="KXBTCD",
    edge=0.025,
    days_to_settle=1.0,
    is_chain=True,
) -> TradePair:
    yes_opp = make_opportunity(
        ticker=yes_ticker, event_ticker=event_ticker, series=series,
        side="yes", bid_price=90, edge=edge, days_to_settle=days_to_settle,
        edge_per_day=edge_per_day(edge, days_to_settle),
    )
    no_opp = make_opportunity(
        ticker=no_ticker, event_ticker=event_ticker, series=series,
        side="no", bid_price=96, edge=edge, days_to_settle=days_to_settle,
        edge_per_day=edge_per_day(edge, days_to_settle),
    )
    avg_epd = (yes_opp.edge_per_day + no_opp.edge_per_day) / 2
    return TradePair(yes_opp=yes_opp, no_opp=no_opp,
                     edge_per_day=avg_epd, is_chain=is_chain)
