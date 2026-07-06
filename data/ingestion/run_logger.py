"""Run logging and progress tracking for ingestion runs.

RunLogger records what an ingestion run did (rows fetched, inserted, errors).
ProgressTracker provides cursor-based and range-based resume for backfills.

Usage:
    logger = RunLogger("kalshi_trades", conn)
    logger.start()
    try:
        for batch in pages:
            rows = process(batch)
            logger.record_progress(rows_fetched=len(batch), rows_inserted=len(rows))
        logger.finish("completed")
    except Exception as e:
        logger.record_error(str(e))
        logger.finish("failed")
        raise
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class RunLogger:
    """Records what an ingestion run did."""

    def __init__(self, dataset_id: str, conn):
        self.dataset_id = dataset_id
        self.conn = conn
        self.run_id: int | None = None
        self._rows_fetched = 0
        self._rows_inserted = 0

    def start(self) -> int:
        """Start a new run. Returns the run_id."""
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO prediction_markets.ingestion_runs
                   (dataset_id, status)
                   VALUES (%s, 'running')
                   RETURNING run_id""",
                (self.dataset_id,),
            )
            self.run_id = cur.fetchone()[0]
        self.conn.commit()
        return self.run_id

    def record_progress(
        self,
        rows_fetched: int = 0,
        rows_inserted: int = 0,
        cursor: str | None = None,
    ) -> None:
        """Update progress counters. Call periodically during ingestion."""
        self._rows_fetched += rows_fetched
        self._rows_inserted += rows_inserted
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE prediction_markets.ingestion_runs
                   SET rows_fetched = %s, rows_inserted = %s,
                       last_cursor = COALESCE(%s, last_cursor)
                   WHERE run_id = %s""",
                (self._rows_fetched, self._rows_inserted, cursor, self.run_id),
            )
        self.conn.commit()

    def record_error(self, error: str) -> None:
        """Record an error message."""
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE prediction_markets.ingestion_runs
                   SET error_message = %s
                   WHERE run_id = %s""",
                (error[:4000], self.run_id),  # truncate to prevent bloat
            )
        self.conn.commit()

    def finish(self, status: str = "completed") -> None:
        """Mark the run as finished."""
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE prediction_markets.ingestion_runs
                   SET status = %s, finished_at = now(),
                       rows_fetched = %s, rows_inserted = %s
                   WHERE run_id = %s""",
                (status, self._rows_fetched, self._rows_inserted, self.run_id),
            )
        self.conn.commit()

    def set_metadata(self, metadata: dict[str, Any]) -> None:
        """Store arbitrary metadata (e.g., completed date ranges)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE prediction_markets.ingestion_runs
                   SET metadata = %s
                   WHERE run_id = %s""",
                (json.dumps(metadata), self.run_id),
            )
        self.conn.commit()


class ProgressTracker:
    """Tracks backfill progress for resumability.

    Supports two resume patterns:
    - Cursor-based: simple pagination (last_cursor field)
    - Range-based: date range tracking (metadata jsonb field)
    """

    def __init__(self, dataset_id: str, conn):
        self.dataset_id = dataset_id
        self.conn = conn

    def get_last_cursor(self) -> str | None:
        """Get the cursor from the most recent incomplete or completed run."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT last_cursor FROM prediction_markets.ingestion_runs
                   WHERE dataset_id = %s AND last_cursor IS NOT NULL
                   ORDER BY started_at DESC LIMIT 1""",
                (self.dataset_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_last_metadata(self) -> dict | None:
        """Get metadata from the most recent run (for range-based resume)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT metadata FROM prediction_markets.ingestion_runs
                   WHERE dataset_id = %s AND metadata != '{}'::jsonb
                   ORDER BY started_at DESC LIMIT 1""",
                (self.dataset_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_completed_ranges(self) -> list[list[str]]:
        """Get completed date ranges from the most recent run's metadata."""
        meta = self.get_last_metadata()
        if meta and "completed_ranges" in meta:
            return meta["completed_ranges"]
        return []


@dataclass
class RunSummary:
    """Summary of a completed ingestion run."""

    run_id: int
    dataset_id: str
    status: str
    rows_fetched: int
    rows_inserted: int
    started_at: Any
    finished_at: Any
    error_message: str | None


def get_last_run(dataset_id: str, conn) -> RunSummary | None:
    """Get the most recent run for a dataset."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_id, dataset_id, status, rows_fetched, rows_inserted,
                      started_at, finished_at, error_message
               FROM prediction_markets.ingestion_runs
               WHERE dataset_id = %s
               ORDER BY started_at DESC LIMIT 1""",
            (dataset_id,),
        )
        row = cur.fetchone()
        if row:
            return RunSummary(*row)
        return None
