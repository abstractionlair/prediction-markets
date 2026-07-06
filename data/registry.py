"""Dataset registry query helpers.

The registry is the dataset_registry table — this module provides
convenient Python access for common operations.

Usage:
    from data.registry import get_dataset, list_datasets, register_dataset

    ds = get_dataset("kalshi_trades", conn)
    print(ds.max_stale_interval)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DatasetInfo:
    """A row from the dataset_registry table."""

    dataset_id: str
    source: str
    storage_table: str
    description: str
    provenance: str
    resolution: str
    update_schedule: str | None
    max_stale_interval: Any  # timedelta or None
    expected_coverage: dict
    natural_key: list[str]
    freshness_column: str
    has_backfill: bool
    has_collector: bool
    splice_precedence: str
    multi_version: bool
    analyze_schedule: str | None
    onboarded_at: Any
    updated_at: Any


_COLUMNS = (
    "dataset_id, source, storage_table, description, provenance, "
    "resolution, update_schedule, max_stale_interval, expected_coverage, "
    "natural_key, freshness_column, has_backfill, has_collector, "
    "splice_precedence, multi_version, analyze_schedule, onboarded_at, updated_at"
)


def _row_to_info(row) -> DatasetInfo:
    return DatasetInfo(*row)


def get_dataset(dataset_id: str, conn) -> DatasetInfo | None:
    """Fetch a single dataset's registry entry."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM prediction_markets.dataset_registry "
            "WHERE dataset_id = %s",
            (dataset_id,),
        )
        row = cur.fetchone()
        return _row_to_info(row) if row else None


def list_datasets(conn, source: str | None = None) -> list[DatasetInfo]:
    """List all registered datasets, optionally filtered by source."""
    with conn.cursor() as cur:
        if source:
            cur.execute(
                f"SELECT {_COLUMNS} FROM prediction_markets.dataset_registry "
                "WHERE source = %s ORDER BY dataset_id",
                (source,),
            )
        else:
            cur.execute(
                f"SELECT {_COLUMNS} FROM prediction_markets.dataset_registry "
                "ORDER BY source, dataset_id"
            )
        return [_row_to_info(row) for row in cur.fetchall()]


def register_dataset(
    dataset_id: str,
    source: str,
    storage_table: str,
    description: str,
    provenance: str,
    resolution: str,
    natural_key: list[str],
    freshness_column: str,
    *,
    update_schedule: str | None = None,
    max_stale_interval: str | None = None,
    expected_coverage: dict | None = None,
    has_backfill: bool = False,
    has_collector: bool = False,
    splice_precedence: str = "historical",
    multi_version: bool = False,
    analyze_schedule: str | None = None,
    conn,
) -> None:
    """Register a new dataset (or update an existing one).

    Args:
        max_stale_interval: Interval string like '2 hours', '26 hours'.
            NULL for backfill-only datasets.
        expected_coverage: Dict with 'earliest' key (ISO date string),
            optional 'latest' key.
    """
    import json

    coverage = json.dumps(expected_coverage or {})

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO prediction_markets.dataset_registry
               (dataset_id, source, storage_table, description, provenance,
                resolution, update_schedule, max_stale_interval, expected_coverage,
                natural_key, freshness_column, has_backfill, has_collector,
                splice_precedence, multi_version, analyze_schedule)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::interval, %s::jsonb,
                       %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (dataset_id) DO UPDATE SET
                   source = EXCLUDED.source,
                   storage_table = EXCLUDED.storage_table,
                   description = EXCLUDED.description,
                   provenance = EXCLUDED.provenance,
                   resolution = EXCLUDED.resolution,
                   update_schedule = EXCLUDED.update_schedule,
                   max_stale_interval = EXCLUDED.max_stale_interval,
                   expected_coverage = EXCLUDED.expected_coverage,
                   natural_key = EXCLUDED.natural_key,
                   freshness_column = EXCLUDED.freshness_column,
                   has_backfill = EXCLUDED.has_backfill,
                   has_collector = EXCLUDED.has_collector,
                   splice_precedence = EXCLUDED.splice_precedence,
                   multi_version = EXCLUDED.multi_version,
                   analyze_schedule = EXCLUDED.analyze_schedule,
                   updated_at = now()""",
            (
                dataset_id, source, storage_table, description, provenance,
                resolution, update_schedule, max_stale_interval, coverage,
                natural_key, freshness_column, has_backfill, has_collector,
                splice_precedence, multi_version, analyze_schedule,
            ),
        )
    conn.commit()
