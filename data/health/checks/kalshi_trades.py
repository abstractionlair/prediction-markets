"""Anomaly checks for kalshi_trades dataset.

Each check is a function that queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_no_negative_prices(conn))
    results.append(_check_recent_inserts(conn))
    results.append(_check_no_monthly_gaps(conn))
    return results


def _check_no_negative_prices(conn) -> CheckResult:
    """Prices should never be negative."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_trades
            WHERE yes_price < 0 OR no_price < 0
        """)
        bad = cur.fetchone()[0]
    if bad > 0:
        return CheckResult("no_negative_prices", False, f"{bad} rows with negative prices")
    return CheckResult("no_negative_prices", True, "OK")


def _check_recent_inserts(conn) -> CheckResult:
    """Verify trades exist in the last 24 hours (collector health proxy)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_trades
            WHERE created_time > now() - interval '24 hours'
        """)
        recent = cur.fetchone()[0]
    if recent == 0:
        return CheckResult("recent_inserts", False, "No trades in the last 24 hours")
    return CheckResult("recent_inserts", True, f"{recent} trades in last 24h")


def _check_no_monthly_gaps(conn) -> CheckResult:
    """No calendar month between first and last trade should have zero rows.

    Catches gaps between historical backfill cutoff and live collection start.
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH months AS (
                SELECT date_trunc('month', created_time) AS m, COUNT(*) AS n
                FROM prediction_markets.kalshi_trades
                GROUP BY 1
            ),
            range AS (
                SELECT MIN(m) AS first_month, MAX(m) AS last_month FROM months
            )
            SELECT gs.month
            FROM range r,
                 generate_series(r.first_month, r.last_month, '1 month'::interval) AS gs(month)
            LEFT JOIN months ON months.m = gs.month
            WHERE months.m IS NULL
        """)
        gaps = [row[0].strftime("%Y-%m") for row in cur.fetchall()]
    if gaps:
        return CheckResult("no_monthly_gaps", False,
                           f"Missing months: {', '.join(gaps[:5])}"
                           + (f" (+{len(gaps)-5} more)" if len(gaps) > 5 else ""))
    return CheckResult("no_monthly_gaps", True, "No monthly gaps")
