"""Anomaly checks for kalshi_candles dataset (all resolutions).

Discovered by the alert system via checks/{dataset_id}.py convention.
Since all three candle datasets (minute, hourly, daily) share one table,
this module handles all three — symlinked or imported by resolution-specific
entry points.
"""

from __future__ import annotations

from data.health.checks import CheckResult


# Resolution map for dataset_id → integer
_RESOLUTION = {
    "kalshi_candles_minute": 1,
    "kalshi_candles_hourly": 60,
    "kalshi_candles_daily": 1440,
}


def run_checks(conn, dataset_id: str) -> list[CheckResult]:
    resolution = _RESOLUTION.get(dataset_id)
    results = []
    if resolution:
        results.append(_check_no_negative_prices(conn, resolution))
        results.append(_check_no_negative_volume(conn, resolution))
        results.append(_check_bid_ask_sanity(conn, resolution))
        results.append(_check_bid_ask_populated(conn, resolution))
    return results


def _check_no_negative_prices(conn, resolution: int) -> CheckResult:
    """Bid/ask/price values should never be negative."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
              AND (yes_bid_close < 0 OR yes_ask_close < 0
                   OR price_close < 0 OR price_open < 0)
        """, (resolution,))
        bad = cur.fetchone()[0]
    if bad > 0:
        return CheckResult("no_negative_prices", False, f"{bad} rows with negative prices")
    return CheckResult("no_negative_prices", True, "OK")


def _check_no_negative_volume(conn, resolution: int) -> CheckResult:
    """Volume and open interest should never be negative."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
              AND (volume < 0 OR open_interest < 0)
        """, (resolution,))
        bad = cur.fetchone()[0]
    if bad > 0:
        return CheckResult("no_negative_volume", False, f"{bad} rows with negative volume/OI")
    return CheckResult("no_negative_volume", True, "OK")


def _check_bid_ask_sanity(conn, resolution: int) -> CheckResult:
    """Ask should be >= bid (in cents). Check on recent data only."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
              AND yes_ask_close IS NOT NULL AND yes_bid_close IS NOT NULL
              AND yes_ask_close < yes_bid_close
              AND period_end > now() - interval '7 days'
        """, (resolution,))
        bad = cur.fetchone()[0]
    if bad > 0:
        return CheckResult("bid_ask_sanity", False, f"{bad} recent rows with ask < bid")
    return CheckResult("bid_ask_sanity", True, "OK")


def _check_bid_ask_populated(conn, resolution: int) -> CheckResult:
    """Recent candles should have bid/ask data populated (not all NULL).

    Catches the bug where a parser regression produces rows with volume/OI
    but all bid/ask/price columns NULL. Checks last 7 days of data only.
    Threshold: at least 10% of rows with volume > 0 should have bid data.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                count(*) FILTER (WHERE volume > 0) AS has_volume,
                count(*) FILTER (WHERE volume > 0 AND yes_bid_close IS NOT NULL) AS has_bid
            FROM prediction_markets.kalshi_candles
            WHERE resolution = %s
              AND period_end > now() - interval '7 days'
        """, (resolution,))
        row = cur.fetchone()
        has_volume, has_bid = row[0] or 0, row[1] or 0

    if has_volume == 0:
        return CheckResult("bid_ask_populated", True, "No recent candles with volume")
    pct = has_bid * 100 / has_volume
    if pct < 10:
        return CheckResult("bid_ask_populated", False,
                           f"Only {pct:.0f}% of candles with volume have bid data "
                           f"({has_bid}/{has_volume}) — possible parser bug")
    return CheckResult("bid_ask_populated", True,
                       f"{pct:.0f}% of candles with volume have bid data")
