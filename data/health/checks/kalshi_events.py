"""Anomaly checks for kalshi_events dataset.

Each check queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_structure_coverage(conn))
    results.append(_check_categories_present(conn))
    return results


def _check_structure_coverage(conn) -> CheckResult:
    """Live events should have market_structure populated."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE market_structure IS NULL),
                   count(*)
            FROM prediction_markets.kalshi_events
            WHERE origin = 'live'
        """)
        null_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("structure_coverage", False, "No live events")
    pct = null_count / total * 100
    if pct > 5:
        return CheckResult("structure_coverage", False,
                           f"{null_count}/{total} ({pct:.1f}%) live events missing market_structure")
    return CheckResult("structure_coverage", True,
                       f"{total - null_count}/{total} have market_structure")


def _check_categories_present(conn) -> CheckResult:
    """Events should span multiple categories (not all one category)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(DISTINCT category)
            FROM prediction_markets.kalshi_events
            WHERE origin = 'live' AND category IS NOT NULL
        """)
        n_cats = cur.fetchone()[0]
    if n_cats < 3:
        return CheckResult("categories_present", False,
                           f"Only {n_cats} categories in live events")
    return CheckResult("categories_present", True, f"{n_cats} categories")
