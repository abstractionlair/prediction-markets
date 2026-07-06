# Data System: Specification

**Draft 2** | 2026-04-05

Parent: `data-system-requirements.md` (R1–R10)
Informed by: `kalshi-data-catalog.md` (Phase 1 discovery)

## Scope

This spec designs the data system itself — the generic infrastructure for managing datasets over their lifecycle. It covers registry, schema conventions, ingestion framework, splice, health monitoring, storage maintenance, and the onboarding process. The system is source-agnostic; it works for prediction markets, economic series, crypto prices, or anything else.

Kalshi trades is the first dataset onboarded through this system. It appears in this spec only where a concrete example clarifies a generic design decision. The Kalshi-specific details (schema, endpoints, quirks) live in the catalog (`kalshi-data-catalog.md`) and the dataset's registry entry, not here.

## 1. Registry (R1)

### 1.1 Storage

The registry is a table: `prediction_markets.dataset_registry`.

Why a table and not files or a separate system:
- It lives next to the data it describes. One connection, one schema, one backup.
- Structured expectations are queryable (health checks can `SELECT` across all datasets).
- Unstructured descriptions are just text columns — no special tooling needed.

### 1.2 Schema

```sql
CREATE TABLE prediction_markets.dataset_registry (
    -- Identity
    dataset_id          text PRIMARY KEY,           -- e.g. 'kalshi_trades', 'fred_observations'
    source              text NOT NULL,              -- e.g. 'kalshi', 'fred', 'polymarket'
    storage_table       text NOT NULL,              -- schema-qualified table name

    -- Human/AI-readable description
    description         text NOT NULL,              -- what this data is, how it behaves, known quirks
    provenance          text NOT NULL,              -- which endpoints, what processing, known source limitations

    -- Structured expectations (drive health checks)
    resolution          text NOT NULL,              -- 'tick', 'minute', 'hourly', 'daily', 'point_in_time'
    update_schedule     text,                       -- human-readable schedule description, e.g. 'continuous', 'daily 06:00 UTC', 'weekly Sunday 08:00 UTC'. NULL if backfill-only. For documentation; not parsed mechanically.
    max_stale_interval  interval,                   -- maximum acceptable time between now and the latest data point before flagging stale. NULL if backfill-only (no freshness expectation). E.g. '2 hours', '26 hours', '8 days'.
    expected_coverage   jsonb NOT NULL DEFAULT '{}', -- see Section 1.3 for schema
    natural_key         text[] NOT NULL,            -- column names forming the unique key, e.g. '{trade_id}' or '{ticker, period_end, resolution}'
    freshness_column    text NOT NULL,              -- column to check for freshness, e.g. 'created_time' or 'period_end'

    -- Splice configuration
    has_backfill        boolean NOT NULL DEFAULT false,
    has_collector       boolean NOT NULL DEFAULT false,
    splice_precedence   text DEFAULT 'historical',  -- 'historical', 'live', or 'none'
    multi_version       boolean NOT NULL DEFAULT false, -- whether corrections create new versions vs overwrite

    -- Maintenance
    analyze_schedule    text,                       -- cron for ANALYZE, e.g. 'daily', 'after_bulk_load'

    -- Metadata
    onboarded_at        timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
```

### 1.3 Health Expectations

**Mechanical health checks** use three registry fields:
- `max_stale_interval`: the staleness threshold. If `now() - max(freshness_column) > max_stale_interval`, the dataset is stale. This is the primary health signal.
- `freshness_column`: which column to check.
- `expected_coverage`: structured metadata for coverage validation.

**`expected_coverage` jsonb schema:**

| Key | Type | Required | Meaning |
|-----|------|----------|---------|
| `earliest` | ISO date string | Yes | Earliest expected data point (e.g., `"2021-06-30"`). Coverage check: does `min(freshness_column)` reach this far back? |
| `latest` | ISO date string | No | Latest expected data point for backfill-only datasets with a known end. NULL/absent for live datasets. |

Example: `{"earliest": "2021-06-30"}` for a live dataset with full history.
Example: `{"earliest": "2024-01-01", "latest": "2025-12-31"}` for a bounded historical dataset.

**`update_schedule`** is human-readable documentation (e.g., `"continuous, every 5 minutes"`, `"weekdays by 10:00 EST"`). It is NOT parsed mechanically — `max_stale_interval` handles the mechanical staleness check. This separation means `update_schedule` can contain nuance ("weekdays only, often delayed on Fed announcement days") without breaking health checks.

**`description` and `provenance`** provide context for diagnosis — when a mechanical check fails, an agent or person reads these to understand whether the failure is expected (e.g., weekend gap for a weekday-only dataset) or a real problem.

### 1.4 Registry Operations

- **Register**: INSERT a row. Done once during onboarding (R8 step 3).
- **Update**: UPDATE description, provenance, expectations as understanding improves.
- **Query health**: JOIN registry to actual table metadata (see Section 5).
- **List all**: `SELECT * FROM dataset_registry ORDER BY source, dataset_id`.

No deletion. Datasets that are retired get a note in their description. The row stays for historical reference.

### 1.5 Multi-Resolution Datasets

When multiple resolutions of the same data share a single storage table (e.g., hourly, daily, and minute candles in `kalshi_candles` with a `resolution` column), each resolution gets its own `dataset_id` in the registry, all pointing to the same `storage_table`.

Example:
- `kalshi_candles_hourly` → `storage_table = 'prediction_markets.kalshi_candles'`, `resolution = 'hourly'`, `max_stale_interval = '2 hours'`
- `kalshi_candles_daily` → same `storage_table`, `resolution = 'daily'`, `max_stale_interval = '26 hours'`
- `kalshi_candles_minute` → same `storage_table`, `resolution = 'minute'`, `max_stale_interval = NULL` (backfill-only)

This works because health checks use the `freshness_column` and `max_stale_interval` per registry row, and the check queries can filter by resolution. One table, multiple logical datasets, independent health expectations.

## 2. Schema Conventions (R2)

These apply to every dataset table. They are checked at onboarding, not enforced by the registry mechanically (the registry describes; the table implements).

### 2.1 Universal Columns

Every dataset table includes:

```sql
-- Temporal versioning (R9)
recorded_at     timestamptz NOT NULL DEFAULT now(),   -- when this row was inserted
superseded_at   timestamptz DEFAULT NULL,             -- when this row was replaced; NULL = current

-- Provenance (where applicable)
origin          text,                                  -- 'historical', 'live', 'legacy', or NULL
-- 'historical' = from historical/backfill API
-- 'live'       = from live/streaming collection
-- 'legacy'     = pre-data-system data preserved during migration
-- NULL         = dataset has only one ingestion path
```

`recorded_at` and `superseded_at` are present on every table regardless of whether multi-version behavior is used. For most datasets, `superseded_at` will always be NULL — the column costs a few bytes per row and buys uniformity.

### 2.2 Timestamps

All temporal values are `timestamptz`. No text timestamps, no bare `timestamp` (no timezone). This applies to both data columns (e.g., `created_time`, `period_end`) and system columns (`recorded_at`, `superseded_at`).

### 2.3 Price Representation

Within a source, prices use a single consistent representation. Across sources, representations may differ.

For Kalshi: integer cents. Rationale: avoids floating-point arithmetic issues, matches how fees and P&L are calculated internally, and cents are the natural unit for a binary contract exchange (prices range 1–99). The API's fixed-point dollar strings are converted to cents on ingestion.

### 2.4 Natural Key and Uniqueness

Every table has a PRIMARY KEY or UNIQUE constraint on its natural key. The natural key columns are declared in the registry (`natural_key` field).

For datasets with multi-version behavior, the uniqueness constraint includes `recorded_at` (or uses a surrogate key) so that multiple versions of the same logical row can coexist.

For datasets without multi-version behavior, the natural key alone is the uniqueness constraint. Upserts use `ON CONFLICT DO UPDATE` or `DO NOTHING`.

### 2.5 Naming

Tables follow `{source}_{dataset}` convention: `kalshi_trades`, `kalshi_candles`, `fred_observations`, `polymarket_snapshots`.

Columns use `snake_case`. Timestamp columns end in `_at` or `_time` (per existing convention — both are acceptable, but a table should be internally consistent).

### 2.6 Indices

Every table has:
- A primary key or unique constraint on the natural key.
- An index on the freshness column (used by health checks).

Additional indices are added only when query patterns justify them. Index decisions are recorded in the registry description.

## 3. Ingestion Framework (R3, R4, R10)

### 3.1 Design Principle

The ingestion framework provides shared infrastructure that dataset-specific ingestors use. It does NOT define a base class or plugin system. Each dataset has its own ingestion script that imports and uses shared utilities. The shared utilities handle the generic parts (rate limiting, retry, progress tracking, logging); the script handles the source-specific parts (API calls, response parsing, row mapping).

Why not a plugin system: data sources are too heterogeneous. A Kalshi trade download paginating by cursor, a FRED series download hitting one URL per series, and a Polymarket subgraph query have almost nothing in common at the API level. Forcing them through an abstract interface creates more complexity than it saves. What they share is operational concerns — don't exceed rate limits, retry on transient failures, log what happened.

### 3.2 Shared Components

#### Rate Limiter

```python
class RateLimiter:
    """Per-source rate limiting with cross-process coordination.
    See Section 3.4 for the shared mechanism."""
    def __init__(self, source: str, qps: float, conn): ...
    def acquire(self) -> None:
        """Block until a request slot is available. Coordinates across processes."""
```

Rate limits are configured per source in the `rate_limit_state` table (Section 3.4). The rate limiter is passed to ingestion scripts, not imported as a global. Multiple processes for the same source coordinate through the shared table to stay within the aggregate QPS limit.

#### Retry with Backoff

```python
def with_retry(fn, max_retries=3, retryable=(429, 500, 502, 503, 504)):
    """Execute fn with exponential backoff + jitter on retryable errors.
    
    - Retries on HTTP status codes in retryable set, plus network errors.
    - Does NOT retry on 4xx (except 429) or authentication errors.
    - Returns the successful response or raises after max_retries.
    """
```

This extracts the pattern already implemented in `kalshi_collector.py:make_request` and `historical_downloader.py`. Existing code retries; the framework standardizes it.

#### Run Logger

```python
class RunLogger:
    """Records what an ingestion run did."""
    def __init__(self, dataset_id: str): ...
    def start(self) -> None: ...
    def record_progress(self, rows_fetched: int, rows_inserted: int, cursor: str = None): ...
    def record_error(self, error: str) -> None: ...
    def finish(self, status: str = 'completed') -> None: ...
```

Writes to an `ingestion_runs` table:

```sql
CREATE TABLE prediction_markets.ingestion_runs (
    run_id          bigserial PRIMARY KEY,
    dataset_id      text NOT NULL REFERENCES dataset_registry(dataset_id),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text NOT NULL DEFAULT 'running',     -- 'running', 'completed', 'failed', 'interrupted'
    rows_fetched    bigint DEFAULT 0,
    rows_inserted   bigint DEFAULT 0,
    last_cursor     text,                                -- for resumability
    error_message   text,
    metadata        jsonb DEFAULT '{}'                   -- source-specific details (e.g., cutoff timestamp, completed date ranges)
);

CREATE INDEX idx_ingestion_runs_dataset_started
    ON prediction_markets.ingestion_runs (dataset_id, started_at DESC);
```

This replaces the implicit logging currently done via print statements. Health monitoring (Section 5) queries this table to detect stale or failed ingestion.

#### Progress Tracker

For resumable backfills:

```python
class ProgressTracker:
    """Tracks backfill progress for resumability."""
    def __init__(self, dataset_id: str, run_id: int): ...
    def get_last_cursor(self) -> str | None: ...
    def save_cursor(self, cursor: str) -> None: ...
```

Uses the `last_cursor` field in `ingestion_runs` for simple cursor-based pagination. For backfills that operate on date ranges (e.g., downloading month-by-month windows), the `metadata` jsonb field on `ingestion_runs` tracks completed ranges:

```json
{"completed_ranges": [["2021-06", "2024-12"], ["2025-03", "2025-06"]], "pending_from": "2025-01"}
```

On resume, the backfill reads the metadata from the most recent incomplete run to identify remaining work. The `ProgressTracker` provides helpers for both cursor-based and range-based resume patterns. This replaces the ad-hoc resume logic in `historical_downloader.py` (which queries the target table to find where it left off).

### 3.3 Dataset-Specific Scripts

Each dataset has its own script (or function) that:

1. Creates a `RateLimiter` for its source (or receives one).
2. Creates a `RunLogger` for this run.
3. Calls the source API, parses responses, maps to rows.
4. Inserts rows using upsert semantics (ON CONFLICT).
5. Periodically calls `run_logger.record_progress()`.
6. Handles resume via `ProgressTracker` if it's a backfill.

The script is responsible for API-specific logic (pagination, parameter construction, field mapping). The framework handles operational concerns.

### 3.4 Process Isolation and Concurrency (R10)

**Graceful degradation:** A failure in one dataset's ingestion must not block others. This is achieved by running each dataset's ingestion as an independent process (separate systemd unit or separate script invocation). A pipeline that sequences multiple datasets (like the existing `weekly_pipeline.py`) must catch exceptions per-dataset and continue to the next, logging the failure via `RunLogger`.

**Concurrent rate limiting:** Two ingestion processes for the same source must not independently consume the full rate limit (which would double the actual request rate and trigger 429s or bans). The rate limiter uses a shared mechanism:

```python
class RateLimiter:
    """Per-source rate limiting with cross-process coordination.
    
    Uses a PG advisory lock + timestamp table to coordinate across processes.
    Each acquire() checks the last request timestamp for this source and sleeps
    if needed to maintain the configured QPS across all processes.
    """
    def __init__(self, source: str, qps: float, conn): ...
    def acquire(self) -> None: ...
```

The coordination table:

```sql
CREATE TABLE prediction_markets.rate_limit_state (
    source          text PRIMARY KEY,
    last_request_at timestamptz NOT NULL DEFAULT now(),
    qps_limit       float NOT NULL
);
```

Each `acquire()` call does a `SELECT ... FOR UPDATE` on this row, checks elapsed time, sleeps if needed, then updates `last_request_at`. This is simple, uses no external dependencies, and correctly serializes across processes. The lock is held only for the duration of the timestamp check (~microseconds), not for the entire request.

For the common case (single process), this adds negligible overhead — one fast query per API call. For concurrent processes, it correctly throttles the aggregate rate.

### 3.5 Backfill vs Collector

Both use the same shared components. The differences:

| Aspect | Backfill | Collector |
|--------|----------|-----------|
| Trigger | Manual or scheduled (one-shot) | Continuous or cron-scheduled |
| Direction | Historical (fills past coverage; may paginate forward or backward through time) | Forward (extends to latest data) |
| Origin value | `'historical'` | `'live'` |
| Resume | Via cursor in `ingestion_runs` | Via freshness of existing data |
| Typical lifespan | Runs once (or re-runs for gap-filling) | Runs indefinitely |

## 4. Splice (R5)

### 4.1 Mechanism

The splice is not a separate process — it's an invariant maintained by ingestion and enforced by a view (or query convention).

The `splice_precedence` registry field has three valid values:

- **`'historical'`**: Historical data replaces live data on conflict. Used when a dataset has both backfill and live collection (e.g., kalshi_trades, kalshi_candles).
- **`'live'`**: Live data replaces historical data on conflict. Rare — used when the live source is more authoritative.
- **`'none'`**: Dataset has only one ingestion path. No splice logic needed. Used for live-only datasets (e.g., kalshi_snapshots) or backfill-only datasets.

When `splice_precedence = 'none'`, the collector uses simple `ON CONFLICT DO NOTHING` — dedup only, no origin-based replacement.

When a dataset has both historical and live data (`has_backfill = true` AND `has_collector = true`), both write to the same table with different `origin` values. The splice precedence determines which version consumers see.

**For datasets WITHOUT multi-version behavior** (most datasets):

When historical data arrives for a period already covered by live data:
- If `splice_precedence = 'historical'`: the historical row replaces the live row via a conditional upsert. The `DO UPDATE` clause only overwrites when the existing row has lower-precedence origin:

```sql
-- Backfill upsert: only replace if existing row is 'live' (lower precedence)
INSERT INTO kalshi_trades (trade_id, ..., origin, recorded_at)
VALUES ($1, ..., 'historical', now())
ON CONFLICT (trade_id) DO UPDATE SET
    origin = EXCLUDED.origin,
    recorded_at = EXCLUDED.recorded_at,
    -- ... other columns ...
WHERE kalshi_trades.origin = 'live';  -- only overwrite live data, never historical
```

The `WHERE` clause on the `DO UPDATE` is critical: without it, a collector re-fetching data that was already backfilled would overwrite historical with live. With it, the higher-precedence origin is always preserved.

**For datasets WITH multi-version behavior**:

When historical data arrives for a period already covered by live data:
- The existing live row gets `superseded_at = now()`.
- The historical row is inserted as a new row with `recorded_at = now()`.
- A view (`{table}_current`) filters to `WHERE superseded_at IS NULL` for consumers who want the latest version.
- The full table retains history for temporal reconstruction.

### 4.2 Default View

For multi-version datasets, create a view:

```sql
CREATE VIEW kalshi_trades_current AS
SELECT * FROM kalshi_trades WHERE superseded_at IS NULL;
```

Consumers use the view by default. Only temporal-reconstruction queries hit the base table with explicit `recorded_at`/`superseded_at` filters.

For non-multi-version datasets, no view is needed — the table itself only contains current data.

### 4.3 Design Decision: Multi-Version Posture per Dataset

Most datasets should NOT use multi-version behavior. The overhead (double rows during correction, view indirection) is only justified when:
- Backtesting needs to reconstruct past-state (e.g., "what calibration data was available on March 1?")
- The source regularly revises data (e.g., preliminary vs revised economic indicators)
- Audit trail matters

Trades: no multi-version. A trade happened or it didn't. Historical data corrects live data in place.
Candles: no multi-version. Same reasoning.
Economic indicators (FRED): potentially yes — preliminary vs revised releases are meaningful.

## 5. Health Monitoring (R6)

### 5.1 Health Check Query

A single query that joins the registry to actual table state and ingestion run history:

```sql
SELECT
    r.dataset_id,
    r.source,
    r.update_schedule,
    r.max_stale_interval,
    latest.max_freshness,
    latest.row_count,
    last_run.finished_at AS last_run_at,
    last_run.status AS last_run_status,
    CASE
        WHEN r.max_stale_interval IS NULL THEN 'backfill_only'
        WHEN latest.max_freshness IS NULL THEN 'no_data'
        WHEN last_run.status = 'failed' THEN 'last_run_failed'
        WHEN latest.max_freshness < now() - r.max_stale_interval THEN 'stale'
        ELSE 'healthy'
    END AS health_status
FROM dataset_registry r
LEFT JOIN LATERAL (
    SELECT max_freshness, row_count
    FROM dataset_health_cache
    WHERE dataset_id = r.dataset_id
) latest ON true
LEFT JOIN LATERAL (
    SELECT finished_at, status
    FROM ingestion_runs ir
    WHERE ir.dataset_id = r.dataset_id
    ORDER BY ir.started_at DESC
    LIMIT 1
) last_run ON true;
```

This requires a `dataset_health_cache` table (see 5.2) and an index on `ingestion_runs(dataset_id, started_at DESC)` (see 3.2). Computing `max(freshness_column)` on 228M-row tables on every health check is too expensive, so the cache is refreshed periodically.

### 5.2 Health Cache

```sql
CREATE TABLE prediction_markets.dataset_health_cache (
    dataset_id      text PRIMARY KEY REFERENCES dataset_registry(dataset_id),
    max_freshness   timestamptz,        -- max of the freshness_column
    min_freshness   timestamptz,        -- min of the freshness_column (earliest data)
    row_count       bigint,             -- approximate; always from pg_class.reltuples (never COUNT(*) on large tables)
    last_computed   timestamptz NOT NULL DEFAULT now()
);
```

Updated by a lightweight job that runs periodically (e.g., every 15 minutes). For each dataset, it runs `SELECT max(freshness_col), min(freshness_col) FROM table` — which is fast if there's an index on the freshness column.

### 5.3 Active Alerting

Health monitoring is not just queryable — it pushes alerts when things go wrong. The alert mechanism is a simple script that:

1. Runs the health check query.
2. For any dataset with `health_status != 'healthy'` and `health_status != 'backfill_only'`:
   - Reads the registry description for context.
   - Sends a notification (initially: writes to a log file and/or sends a desktop notification; later: Slack/email webhook).
3. Runs on a cron schedule (e.g., every 30 minutes).

The first implementation is simple. The key requirement is that it runs and it's not silent — the entire point is to solve "collectors stop and nobody knows."

### 5.4 Gap Detection

For time-series datasets with a declared resolution, gap detection compares expected data points to actual:

```sql
-- Find gaps: periods where data is expected but missing
-- (Dataset-specific; this is the pattern, not a universal query)
SELECT
    expected_period,
    CASE WHEN actual.period_end IS NULL THEN 'missing' ELSE 'present' END
FROM generate_series(
    (SELECT min_freshness FROM dataset_health_cache WHERE dataset_id = 'kalshi_candles_hourly'),
    now(),
    interval '1 hour'
) expected_period
LEFT JOIN kalshi_candles actual
    ON actual.period_end = expected_period
    AND actual.ticker = 'some_ticker'
    AND actual.resolution = 60;
```

Gap detection is opt-in per dataset — not every dataset has a regular cadence (e.g., trades arrive irregularly).

### 5.5 Anomaly Checks (R6)

R6 requires optional per-dataset invariant checks (e.g., "volume should never be negative", "row count should increase monotonically"). These are dataset-specific — the generic system provides the execution mechanism, not the checks themselves.

**Mechanism:** Each dataset may define check functions in a `data/health/checks/` directory, following a naming convention:

```
data/health/checks/kalshi_trades.py      # checks for kalshi_trades dataset
data/health/checks/fred_observations.py  # checks for fred_observations dataset
```

Each file exports a `run_checks(conn, dataset_id) -> list[CheckResult]` function. `CheckResult` is a simple dataclass:

```python
@dataclass
class CheckResult:
    check_name: str       # e.g. 'volume_non_negative'
    passed: bool
    message: str          # e.g. '3 rows with negative volume found'
```

The health alerting script (5.3) discovers check files by convention (`checks/{dataset_id}.py`), imports and runs them, and includes failures in alerts. Datasets without a check file simply skip this step.

This is deliberately lightweight — no DSL, no SQL-in-registry, no framework. A check is a Python function that queries the table and returns pass/fail. If the check logic is a single SQL statement, the function is 5 lines. If it needs complex logic, it has the full language available.

### 5.6 Gap Detection Execution

Gap detection (5.4) is a specific type of anomaly check. For time-series datasets, the check function in `data/health/checks/{dataset_id}.py` implements gap detection using the pattern from 5.4, parameterized with:
- The expected interval (from the dataset's `resolution`)
- The entity columns to partition by (e.g., `ticker` for candles — gaps are per-ticker, not per-table)
- The time column to check

The check function knows these details because it's dataset-specific. The generic system just runs it and reports results.

## 6. Storage Maintenance (R7)

### 6.1 ANALYZE Scheduling

Tables that receive bulk loads need `ANALYZE` after the load completes. Tables that receive continuous writes need periodic `ANALYZE`.

The `analyze_schedule` field in the registry declares when ANALYZE should run:
- `'after_bulk_load'` — the backfill script calls ANALYZE when it finishes.
- `'daily'` — a cron job runs ANALYZE on all tables with this schedule.
- `NULL` — no scheduled ANALYZE (small tables where autovacuum is sufficient).

### 6.2 Autovacuum Tuning

For large tables (>10M rows), autovacuum's default thresholds may be too aggressive or too passive. The onboarding process (R8) should set appropriate thresholds:

```sql
-- Example for a large, continuously-written table
ALTER TABLE kalshi_trades SET (
    autovacuum_vacuum_scale_factor = 0.01,     -- vacuum after 1% of rows change (vs default 20%)
    autovacuum_analyze_scale_factor = 0.005    -- analyze after 0.5% of rows change
);
```

These settings are documented in the registry description, not hidden in migration scripts.

### 6.3 Index Maintenance

- Every index has a purpose documented in the registry.
- Unused indices are candidates for removal (check `pg_stat_user_indexes`).
- After large bulk loads, `REINDEX CONCURRENTLY` may be needed if index bloat is significant.

## 7. Onboarding Process (R8)

This is the step-by-step procedure for adding a new dataset. Each step produces a concrete artifact.

### Step 1: Discover

**What:** Understand what the source offers.
**Artifact:** A data catalog document (like `kalshi-data-catalog.md`).
**Contains:** Endpoint inventory, rate limits, historical availability, known quirks, field schemas, resolution options.

This step is done once per source, not per dataset. Multiple datasets from the same source share one catalog.

### Step 2: Design

**What:** Design the table schema.
**Artifact:** A `CREATE TABLE` statement (applied to the database).
**Constraints:**
- Follows schema conventions (Section 2): `timestamptz`, consistent price representation, natural key with uniqueness constraint, `recorded_at`/`superseded_at`, `origin` column (always present; 'historical'/'live'/'legacy' as applicable).
- Index on freshness column at minimum.
- Autovacuum tuning for expected table size.

### Step 3: Describe

**What:** Write the registry entry.
**Artifact:** An `INSERT INTO dataset_registry` statement (applied to the database).
**Contains:** Everything from R1 — description, provenance, resolution, update schedule, expected coverage, natural key, freshness column, splice configuration, maintenance schedule.

### Step 4: Backfill

**What:** Implement and run the historical data download.
**Artifact:** A backfill script that uses the ingestion framework (Section 3).
**Properties:** Resumable (via `ProgressTracker`), idempotent (via upsert), logged (via `RunLogger`), rate-limited (via `RateLimiter`).

### Step 5: Collect

**What:** Implement and enable the live data collector.
**Artifact:** A collector script + systemd unit (timer or continuous service).
**Properties:** Idempotent, logged, rate-limited. Writes with `origin = 'live'`.

Two service models:
- **Timer-based (Type=oneshot):** For datasets collected periodically (e.g., daily trades sync, hourly candle collection). A systemd timer triggers a one-shot script that runs `sync` (collect + health cache update).
- **Continuous (Type=simple):** For datasets requiring near-real-time collection (e.g., orderbook snapshots). A long-running service loops: discover → collect → brief pause → repeat. Handles SIGTERM for graceful shutdown. The `run` command is the service entry point; `collect` runs a single cycle for testing.

### Step 6: Verify

**What:** Confirm the dataset meets its declared expectations.
**Checklist:**
- [ ] Health check reports `healthy`
- [ ] Coverage matches `expected_coverage` in registry
- [ ] Freshness is within declared schedule
- [ ] Row count is plausible
- [ ] Splice works: if both backfill and collector ran, data is coherent
- [ ] Temporal columns present: `recorded_at` populated, `superseded_at` is NULL for all current rows
- [ ] ANALYZE has been run; planner estimates are accurate

## 8. File and Module Layout

```
data/                           # New directory for data system code
  registry.py                   # Registry query helpers
  ingestion/
    rate_limiter.py             # RateLimiter class
    retry.py                    # with_retry utility
    run_logger.py               # RunLogger + ProgressTracker
  health/
    check.py                    # Health check query + cache update
    alert.py                    # Active alerting script
    checks/                     # Per-dataset anomaly check functions
      kalshi_trades.py          # e.g. volume_non_negative, count_monotonic
      ...
  maintenance/
    analyze.py                  # Scheduled ANALYZE runner

collectors/                     # Existing directory — dataset-specific scripts stay here
  kalshi/                       # Source-specific subdirectory (Phase 3+ refinement)
    trades.py                   # Kalshi trades backfill + collector
    ...
```

The `data/` directory contains the generic system. The `collectors/` directory contains source-specific scripts that import from `data/`. This separation is the physical manifestation of "the system is source-agnostic."

## 9. Validation

### 9.1 Kalshi Trades (Pilot — 2026-04-05)

The first dataset onboarded through this system. Validates the core design end-to-end.

What was validated:
- Registry can describe a real dataset with real expectations
- Ingestion framework handles a 228M-row backfill at 30 QPS
- Run logging captures progress and supports resume
- Splice between historical and live trade data works
- Health monitoring detects freshness and reports status
- ANALYZE and maintenance keep query plans accurate
- The onboarding procedure (Steps 1–6) is complete and followable

### 9.2 Kalshi Candles (Second Dataset — 2026-04-06)

Validates multi-resolution support (Section 1.5) — the main capability the trades pilot deferred.

What was validated:
- Three registry entries sharing one storage table with independent health expectations
- Resolution-aware health cache via `filter_column`/`filter_value` in `expected_coverage`
- Per-ticker historical backfill + batch live collection (different API patterns, same framework)
- Both API response formats handled (historical: plain field names; batch: `_dollars`/`_fp` suffixes)
- Per-resolution anomaly checks via shared check module with thin entry points

Framework extension required: `_update_cache_one` needed a WHERE clause for multi-resolution freshness queries. Solved generically via `expected_coverage` fields — any future multi-resolution dataset gets this for free.

### 9.3 Kalshi Snapshots (Third Dataset — 2026-04-06)

Validates live-only dataset pattern — no historical API, no backfill, no splice.

What was validated:
- Live-only registry entry (no backfill, no splice precedence) with continuous update schedule
- Schema migration preserving 7.56M legacy rows with `origin = 'legacy'` flag
- API format change handling: `orderbook_fp` with `yes_dollars`/`no_dollars` (dollar strings) vs old `orderbook` with integer cents — same pattern as `volume_fp` change that broke the old collector
- Discovery uses `open_interest_fp` (not integer `open_interest`) to find active markets — the integer field returns 0 for all markets in nested event responses, which was the root cause of the old collector breaking
- Continuous service model (Type=simple) vs one-shot timer model used by trades/candles
- Cross-process rate limiting via PG advisory lock (shared QPS budget with other Kalshi collectors)

### 9.4 Kalshi Events + Markets (Fourth/Fifth Datasets — 2026-04-06)

Validates metadata table pattern — overwritten (not appended), shared discovery source.

What was validated:
- Metadata tables as first-class data system citizens (registry, health checks, anomaly checks)
- Schema migration: TEXT timestamps → timestamptz, added spec columns (recorded_at, origin, superseded_at)
- Timer-based oneshot service model for periodic metadata refresh (every 30 min)
- market_structure derivation from constituent markets' strike_types + mutually_exclusive flag
- volume/open_interest from `_fp` API fields (reuses the same parsing pattern as snapshots)
- Both tables refreshed from a single API pass (events with nested markets), run-logged under kalshi_events
- Legacy data preserved with `origin = 'legacy'` (20K events, 324K markets)
- FK between kalshi_markets and kalshi_events dropped (no FKs between data tables)

Design direction validated: these tables become the canonical "what's active" source — other collectors should query kalshi_markets for discovery instead of inline API calls.

### 9.5 Kalshi Settled Events + Markets (Sixth/Seventh Datasets — 2026-04-06)

What was validated:
- Append-only settlement tables as data system citizens (different pattern from overwrite metadata)
- Schema migration: settled_at TEXT → timestamptz, added spec columns (recorded_at, origin, superseded_at), dropped FK
- Additional columns added: series_ticker, mutually_exclusive (events); close_time, strike_type, floor_strike (markets)
- Weekly timer-based oneshot service (Sunday 06:00 UTC)
- market_structure derivation reused from discovery — but without status filter (all settled markets count)
- volume from `volume_fp` field (integer volume often zeroed after settlement)
- Full re-download each week (62K+ events, 4.5M+ markets); ON CONFLICT upserts are idempotent
- Legacy data preserved with `origin = 'legacy'` (62K events, 4.5M markets); re-fetched rows upgraded to `origin = 'live'`
- Both tables refreshed from a single API pass (`GET /events?status=settled&with_nested_markets=true`)

Design direction validated: append-only tables with weekly full refresh work within the same framework as overwrite and continuous patterns.

### 9.6 Not Yet Validated

- Multi-version behavior (no dataset needs it yet)
- Sources with very different API patterns (FRED, Polymarket)
- Gap detection for time-series data

## 10. What This Spec Does Not Cover

- **Migration of existing tables.** All Kalshi tables have been migrated to the data system framework. Remaining tables (Polymarket, FRED, etc.) continue as-is until onboarded.
- **Consumer-side changes.** MarketView, replay, trader — these are downstream. They'll use the new tables when migration happens.
- **Derived tables.** `settled_with_prices` and similar are consumer concerns, not data system concerns.
- **Alerting channel selection.** The first implementation logs alerts to a file. Slack/email integration is a refinement.
