"""Anomaly checks for kalshi_settled_events dataset.

Each check queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_results_coverage(conn))
    results.append(_check_structure_coverage(conn))
    return results


def _check_results_coverage(conn) -> CheckResult:
    """Settled events should have child markets with results."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE num_markets IS NULL OR num_markets = 0),
                   count(*)
            FROM prediction_markets.kalshi_settled_events
        """)
        zero_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("results_coverage", False, "No settled events")
    pct = zero_count / total * 100
    if pct > 5:
        return CheckResult("results_coverage", False,
                           f"{zero_count}/{total} ({pct:.1f}%) events have 0 markets")
    return CheckResult("results_coverage", True,
                       f"{total - zero_count}/{total} events have markets")


def _check_structure_coverage(conn) -> CheckResult:
    """Settled events from the live collector should have market_structure."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE market_structure IS NULL),
                   count(*)
            FROM prediction_markets.kalshi_settled_events
            WHERE origin = 'live'
        """)
        null_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("structure_coverage", True,
                           "No live events yet (legacy-only)")
    pct = null_count / total * 100
    if pct > 10:
        return CheckResult("structure_coverage", False,
                           f"{null_count}/{total} ({pct:.1f}%) live events missing market_structure")
    return CheckResult("structure_coverage", True,
                       f"{total - null_count}/{total} live events have market_structure")
