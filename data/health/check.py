"""Health check query and cache updater.

The health check joins the registry to actual table state (via the health
cache) and ingestion run history to produce a per-dataset health status.

Usage:
    from data.health.check import check_all, update_cache, HealthStatus

    statuses = check_all(conn)
    for s in statuses:
        print(f"{s.dataset_id}: {s.health_status}")

    update_cache(conn)  # refresh the health cache for all datasets
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data.registry import get_dataset


@dataclass
class HealthStatus:
    """Result of a health check for one dataset."""

    dataset_id: str
    source: str
    update_schedule: str | None
    max_stale_interval: Any
    max_freshness: Any  # timestamptz or None
    row_count: int | None
    last_run_at: Any
    last_run_status: str | None
    health_status: str  # 'healthy', 'stale', 'no_data', 'last_run_failed', 'backfill_only'


def check_all(conn) -> list[HealthStatus]:
    """Run the health check query across all registered datasets."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                r.dataset_id,
                r.source,
                r.update_schedule,
                r.max_stale_interval,
                hc.max_freshness,
                hc.row_count,
                last_run.finished_at,
                last_run.status,
                CASE
                    WHEN r.max_stale_interval IS NULL THEN 'backfill_only'
                    WHEN hc.max_freshness IS NULL THEN 'no_data'
                    WHEN last_run.status = 'failed' THEN 'last_run_failed'
                    WHEN hc.max_freshness < now() - r.max_stale_interval THEN 'stale'
                    ELSE 'healthy'
                END AS health_status
            FROM prediction_markets.dataset_registry r
            LEFT JOIN prediction_markets.dataset_health_cache hc
                ON hc.dataset_id = r.dataset_id
            LEFT JOIN LATERAL (
                SELECT finished_at, status
                FROM prediction_markets.ingestion_runs ir
                WHERE ir.dataset_id = r.dataset_id
                ORDER BY ir.started_at DESC
                LIMIT 1
            ) last_run ON true
            ORDER BY r.source, r.dataset_id
        """)
        return [HealthStatus(*row) for row in cur.fetchall()]


def check_one(dataset_id: str, conn) -> HealthStatus | None:
    """Run the health check for a single dataset."""
    results = check_all(conn)
    for r in results:
        if r.dataset_id == dataset_id:
            return r
    return None


def update_cache(conn) -> int:
    """Refresh the health cache for all registered datasets.

    For each dataset, computes max/min of the freshness column and gets
    an approximate row count from pg_class.reltuples. Returns the number
    of datasets updated.
    """
    from data.registry import list_datasets

    datasets = list_datasets(conn)
    updated = 0

    for ds in datasets:
        try:
            _update_cache_one(ds, conn)
            updated += 1
        except Exception as e:
            print(f"  Warning: cache update failed for {ds.dataset_id}: {e}")
            conn.rollback()

    return updated


def _update_cache_one(ds, conn) -> None:
    """Update the health cache for a single dataset."""
    table = ds.storage_table
    col = ds.freshness_column

    # Validate identifiers to prevent SQL injection (these come from our registry)
    _validate_identifier(table)
    _validate_identifier(col)

    # Multi-resolution support: expected_coverage may specify a filter
    # e.g. {"filter_column": "resolution", "filter_value": 60}
    coverage = ds.expected_coverage or {}
    filter_col = coverage.get("filter_column")
    filter_val = coverage.get("filter_value")
    where = ""
    params = []
    if filter_col and filter_val is not None:
        _validate_identifier(filter_col)
        where = f" WHERE {filter_col} = %s"
        params = [filter_val]

    with conn.cursor() as cur:
        # Get freshness bounds (fast with an index on freshness_column)
        cur.execute(
            f"SELECT max({col}), min({col}) FROM {table}{where}",
            params,
        )
        max_fresh, min_fresh = cur.fetchone()

        # Approximate row count from pg_class (never COUNT(*) on large tables)
        schema, tbl = _split_table(table)
        cur.execute(
            """SELECT reltuples::bigint FROM pg_class
               WHERE relname = %s
               AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)""",
            (tbl, schema),
        )
        row = cur.fetchone()
        row_count = row[0] if row else None

        # Upsert into cache
        cur.execute(
            """INSERT INTO prediction_markets.dataset_health_cache
               (dataset_id, max_freshness, min_freshness, row_count, last_computed)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (dataset_id) DO UPDATE SET
                   max_freshness = EXCLUDED.max_freshness,
                   min_freshness = EXCLUDED.min_freshness,
                   row_count = EXCLUDED.row_count,
                   last_computed = now()""",
            (ds.dataset_id, max_fresh, min_fresh, row_count),
        )
    conn.commit()


def _split_table(qualified_name: str) -> tuple[str, str]:
    """Split 'schema.table' into (schema, table)."""
    parts = qualified_name.split(".")
    if len(parts) == 2:
        return parts[0], parts[1]
    return "prediction_markets", parts[0]


def _validate_identifier(name: str) -> None:
    """Basic check that a table/column name is a valid SQL identifier."""
    import re
    # Allow schema.table format
    if not re.match(r'^[a-z_][a-z0-9_.]*$', name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
