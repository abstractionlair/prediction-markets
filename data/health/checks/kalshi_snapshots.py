"""Anomaly checks for kalshi_snapshots dataset.

Each check queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_no_inverted_spreads(conn))
    results.append(_check_recent_inserts(conn))
    return results


def _check_no_inverted_spreads(conn) -> CheckResult:
    """yes_bid should never exceed yes_ask (inverted spread)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_snapshots
            WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL
              AND yes_bid > yes_ask
              AND timestamp > now() - interval '7 days'
        """)
        bad = cur.fetchone()[0]
    if bad > 0:
        return CheckResult("no_inverted_spreads", False,
                           f"{bad} rows with yes_bid > yes_ask in last 7 days")
    return CheckResult("no_inverted_spreads", True, "OK")


def _check_recent_inserts(conn) -> CheckResult:
    """Verify snapshots exist in the last hour (collector health proxy)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_snapshots
            WHERE timestamp > now() - interval '1 hour'
        """)
        recent = cur.fetchone()[0]
    if recent == 0:
        return CheckResult("recent_inserts", False,
                           "No snapshots in the last hour")
    return CheckResult("recent_inserts", True, f"{recent} snapshots in last hour")
