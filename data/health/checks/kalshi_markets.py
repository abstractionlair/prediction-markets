"""Anomaly checks for kalshi_markets dataset.

Each check queries the table and returns pass/fail.
run_checks() is the entry point discovered by the alert system.
"""

from __future__ import annotations

from data.health.checks import CheckResult


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    results = []
    results.append(_check_timestamps_valid(conn))
    results.append(_check_volume_oi_populated(conn))
    results.append(_check_stale_rows_superseded(conn))
    return results


def _check_timestamps_valid(conn) -> CheckResult:
    """close_time should be a valid timestamptz for live markets."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE close_time IS NULL),
                   count(*)
            FROM prediction_markets.kalshi_markets
            WHERE origin = 'live'
        """)
        null_count, total = cur.fetchone()
    if total == 0:
        return CheckResult("timestamps_valid", False, "No live markets")
    if null_count > 0:
        return CheckResult("timestamps_valid", False,
                           f"{null_count}/{total} live markets missing close_time")
    return CheckResult("timestamps_valid", True, f"All {total} live markets have close_time")


def _check_volume_oi_populated(conn) -> CheckResult:
    """Live markets should have non-zero volume/OI from _fp fields."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE volume > 0 OR open_interest > 0),
                   count(*)
            FROM prediction_markets.kalshi_markets
            WHERE origin = 'live'
        """)
        nonzero, total = cur.fetchone()
    if total == 0:
        return CheckResult("volume_oi_populated", False, "No live markets")
    pct = nonzero / total * 100
    if pct < 50:
        return CheckResult("volume_oi_populated", False,
                           f"Only {pct:.0f}% of live markets have volume or OI")
    return CheckResult("volume_oi_populated", True,
                       f"{nonzero}/{total} ({pct:.0f}%) have volume or OI")


def _check_stale_rows_superseded(conn) -> CheckResult:
    """Live rows with stale recorded_at should have superseded_at set.

    Catches the case where the discovery collector stops marking
    disappeared markets as superseded.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_markets
            WHERE origin = 'live'
              AND superseded_at IS NULL
              AND recorded_at < now() - interval '2 hours'
        """)
        stale = cur.fetchone()[0]
    if stale > 100:
        return CheckResult("stale_rows_superseded", False,
                           f"{stale} live markets with stale recorded_at but no superseded_at "
                           f"— discovery may not be marking disappeared markets")
    return CheckResult("stale_rows_superseded", True,
                       f"OK ({stale} stale unsuperseded rows)")
