"""Active health alerting.

Runs the health check query and reports any datasets that are not healthy.
First implementation: prints to stdout/log. Later: webhook/Slack.

Usage:
    python -m data.health.alert           # check all, report unhealthy
    python -m data.health.alert --update  # also refresh the health cache first
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import psycopg2

from data.health.check import check_all, update_cache


def get_conn():
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    return psycopg2.connect(dsn)


def run_alert(conn, update_first: bool = False) -> list:
    """Check health and return list of unhealthy datasets."""
    if update_first:
        print(f"[{_now()}] Updating health cache...")
        n = update_cache(conn)
        print(f"[{_now()}] Updated {n} datasets")

    statuses = check_all(conn)
    unhealthy = [s for s in statuses if s.health_status not in ("healthy", "backfill_only")]

    print(f"[{_now()}] Health check: {len(statuses)} datasets, "
          f"{len(statuses) - len(unhealthy)} healthy, {len(unhealthy)} issues")

    for s in unhealthy:
        print(f"  ALERT [{s.health_status}] {s.dataset_id} "
              f"(source={s.source}, last_fresh={s.max_freshness}, "
              f"last_run={s.last_run_status} at {s.last_run_at})")

    # Also run per-dataset anomaly checks if check files exist
    _run_anomaly_checks(conn, statuses)

    return unhealthy


def _run_anomaly_checks(conn, statuses):
    """Discover and run per-dataset anomaly check files."""
    import importlib

    for s in statuses:
        module_name = f"data.health.checks.{s.dataset_id}"
        try:
            mod = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue  # No checks for this dataset — skip

        if not hasattr(mod, "run_checks"):
            continue

        try:
            results = mod.run_checks(conn, s.dataset_id)
            for r in results:
                if not r.passed:
                    print(f"  ANOMALY [{s.dataset_id}] {r.check_name}: {r.message}")
        except Exception as e:
            print(f"  Warning: anomaly check failed for {s.dataset_id}: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main():
    parser = argparse.ArgumentParser(description="Data system health alerting")
    parser.add_argument("--update", action="store_true",
                        help="Refresh health cache before checking")
    args = parser.parse_args()

    conn = get_conn()
    try:
        unhealthy = run_alert(conn, update_first=args.update)
        sys.exit(1 if unhealthy else 0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
