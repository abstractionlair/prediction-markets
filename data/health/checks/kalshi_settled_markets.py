"""Anomaly checks for kalshi_settled_markets dataset.

Each check queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_results_populated(conn))
    results.append(_check_settled_at_populated(conn))
    return results


def _check_results_populated(conn) -> CheckResult:
    """Settled markets should have a result (yes/no)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE result IS NULL),
                   count(*)
            FROM prediction_markets.kalshi_settled_markets
        """)
        null_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("results_populated", False, "No settled markets")
    pct = null_count / total * 100
    if pct > 1:
        return CheckResult("results_populated", False,
                           f"{null_count}/{total} ({pct:.1f}%) markets missing result")
    return CheckResult("results_populated", True,
                       f"{total - null_count}/{total} markets have result")


def _check_settled_at_populated(conn) -> CheckResult:
    """Settled markets should have a settled_at timestamp."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE settled_at IS NULL),
                   count(*)
            FROM prediction_markets.kalshi_settled_markets
        """)
        null_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("settled_at_populated", False, "No settled markets")
    pct = null_count / total * 100
    if pct > 1:
        return CheckResult("settled_at_populated", False,
                           f"{null_count}/{total} ({pct:.1f}%) markets missing settled_at")
    return CheckResult("settled_at_populated", True,
                       f"{total - null_count}/{total} markets have settled_at")
