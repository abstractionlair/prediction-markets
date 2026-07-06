"""CalibrationStore: persistent storage for calibration artifacts.

Backed by filesystem (serialized artifacts) + optional database metadata
(for querying by name and temporal boundary).

Two modes:
- File-only (conn_factory=None): artifacts stored as pickle files,
  availability_time parsed from filenames. Suitable for testing and
  single-machine use.
- File + DB (conn_factory provided): artifacts stored as pickle files,
  metadata in prediction_markets.calibration_artifacts table.
  Suitable for production with shared storage.

Directory layout:
    {base_dir}/
        {estimator_name}/
            {YYYYMMDDTHHMMSSZ}.pkl
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

from framework.estimator import BoundEstimator


class CalibrationStore:
    """Persistent storage for calibration artifacts.

    Stores serialized estimators on the filesystem and (optionally)
    records metadata in a database table for efficient querying.
    """

    def __init__(self, base_dir: str | Path, conn_factory=None):
        """
        Args:
            base_dir: Root directory for artifact storage.
            conn_factory: Optional callable returning a DB connection.
                          If provided, metadata is stored in the
                          calibration_artifacts table.
        """
        self._base_dir = Path(base_dir)
        self._conn_factory = conn_factory
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if conn_factory:
            self._ensure_table()

    def _ensure_table(self):
        """Create the metadata table if it doesn't exist."""
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS
                prediction_markets.calibration_artifacts (
                    estimator_name    TEXT NOT NULL,
                    availability_time TIMESTAMPTZ NOT NULL,
                    artifact_path     TEXT NOT NULL,
                    config            JSONB,
                    metrics           JSONB,
                    data_hash         TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (estimator_name, availability_time)
                )
            """)
            conn.commit()
        finally:
            cur.close()

    # ── Public API ───────────────────────────────────────────────

    def store(self, estimator_name: str, bound_estimator: BoundEstimator,
              config: dict = None, metrics: dict = None,
              data_hash: str = None):
        """Persist a calibrated estimator.

        The availability_time is read from the BoundEstimator wrapper
        (framework-assigned, not estimator-declared).
        Overwrites if an artifact with the same (name, availability_time) exists.
        """
        avail = bound_estimator.availability_time

        # Write artifact file
        est_dir = self._base_dir / estimator_name
        est_dir.mkdir(exist_ok=True)
        filename = self._time_to_filename(avail)
        artifact_path = est_dir / filename

        with open(artifact_path, 'wb') as f:
            pickle.dump(bound_estimator.inner, f)  # noqa: S301

        # Write metadata to DB if available
        if self._conn_factory:
            self._store_metadata(estimator_name, avail, str(artifact_path),
                                 config, metrics, data_hash)

    def load(self, estimator_name: str,
             as_of: datetime) -> BoundEstimator | None:
        """Load the best artifact valid for a given as_of.

        Returns the BoundEstimator with the highest availability_time <= as_of,
        or None if no valid artifact exists.

        Defense in depth: validates availability_time <= as_of on the loaded
        artifact even though the query/scan already filters for this.
        """
        if self._conn_factory:
            result = self._load_from_db(estimator_name, as_of)
        else:
            result = self._load_from_fs(estimator_name, as_of)

        # Post-load validation (spec Section 4.3: defense in depth)
        if result is not None:
            as_of_utc = self._normalize_utc(as_of)
            avail_utc = self._normalize_utc(result.availability_time)
            if avail_utc > as_of_utc:
                raise ValueError(
                    f"CalibrationStore integrity error: loaded artifact "
                    f"'{estimator_name}' has availability_time="
                    f"{result.availability_time} which exceeds "
                    f"as_of={as_of}"
                )

        return result

    def latest_boundary(self, estimator_name: str) -> datetime | None:
        """The availability_time of the most recent artifact for this estimator."""
        if self._conn_factory:
            return self._latest_boundary_db(estimator_name)
        return self._latest_boundary_fs(estimator_name)

    def list_boundaries(self, estimator_name: str) -> list[datetime]:
        """All available availability_time values, sorted ascending."""
        if self._conn_factory:
            return self._list_boundaries_db(estimator_name)
        return self._list_boundaries_fs(estimator_name)

    # ── Filename encoding ────────────────────────────────────────

    @staticmethod
    def _normalize_utc(dt: datetime) -> datetime:
        """Normalize to UTC. Naive datetimes assumed UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _time_to_filename(dt: datetime) -> str:
        utc = CalibrationStore._normalize_utc(dt)
        return utc.strftime('%Y%m%dT%H%M%SZ') + '.pkl'

    @staticmethod
    def _filename_to_time(filename: str) -> datetime:
        stem = Path(filename).stem
        return datetime.strptime(stem, '%Y%m%dT%H%M%SZ').replace(
            tzinfo=timezone.utc)

    # ── File-only backend ────────────────────────────────────────

    def _scan_artifacts(self, estimator_name: str):
        """Yield (path, availability_time) for all artifacts of an estimator."""
        est_dir = self._base_dir / estimator_name
        if not est_dir.exists():
            return
        for pkl in est_dir.glob('*.pkl'):
            try:
                avail = self._filename_to_time(pkl.name)
            except ValueError:
                continue
            yield pkl, avail

    def _load_from_fs(self, estimator_name, as_of):
        as_of_utc = self._normalize_utc(as_of)
        best_path = None
        best_time = None

        for pkl, avail in self._scan_artifacts(estimator_name):
            if avail <= as_of_utc and (best_time is None or avail > best_time):
                best_path = pkl
                best_time = avail

        if best_path is None:
            return None
        return self._load_artifact(best_path, best_time)

    def _latest_boundary_fs(self, estimator_name):
        best = None
        for _, avail in self._scan_artifacts(estimator_name):
            if best is None or avail > best:
                best = avail
        return best

    def _list_boundaries_fs(self, estimator_name):
        return sorted(avail for _, avail in self._scan_artifacts(estimator_name))

    # ── DB backend ───────────────────────────────────────────────

    def _store_metadata(self, estimator_name, avail, artifact_path,
                        config, metrics, data_hash):
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO prediction_markets.calibration_artifacts
                    (estimator_name, availability_time, artifact_path,
                     config, metrics, data_hash)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (estimator_name, availability_time)
                DO UPDATE SET
                    artifact_path = EXCLUDED.artifact_path,
                    config = EXCLUDED.config,
                    metrics = EXCLUDED.metrics,
                    data_hash = EXCLUDED.data_hash,
                    created_at = now()
            """, (estimator_name, avail, artifact_path,
                  json.dumps(config) if config else None,
                  json.dumps(metrics) if metrics else None,
                  data_hash))
            conn.commit()
        finally:
            cur.close()

    def _load_from_db(self, estimator_name, as_of):
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT artifact_path, availability_time
                FROM prediction_markets.calibration_artifacts
                WHERE estimator_name = %s
                  AND availability_time <= %s
                ORDER BY availability_time DESC
                LIMIT 1
            """, (estimator_name, as_of))
            row = cur.fetchone()
            if not row:
                return None
            path, avail = row
            return self._load_artifact(path, avail)
        finally:
            cur.close()

    def _latest_boundary_db(self, estimator_name):
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT MAX(availability_time)
                FROM prediction_markets.calibration_artifacts
                WHERE estimator_name = %s
            """, (estimator_name,))
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None
        finally:
            cur.close()

    def _list_boundaries_db(self, estimator_name):
        conn = self._conn_factory()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT availability_time
                FROM prediction_markets.calibration_artifacts
                WHERE estimator_name = %s
                ORDER BY availability_time ASC
            """, (estimator_name,))
            return [row[0] for row in cur.fetchall()]
        finally:
            cur.close()

    # ── Shared ───────────────────────────────────────────────────

    def _load_artifact(self, path, availability_time):
        with open(path, 'rb') as f:
            inner = pickle.load(f)  # noqa: S301 — trusted local artifacts only
        return BoundEstimator(inner, availability_time)

    def __repr__(self) -> str:
        return f"CalibrationStore({self._base_dir})"
