# Data System: Requirements

**Draft 3** | 2026-04-05

Parent: `infrastructure-vision.md` (Data pillar)

## Problem

Our data was acquired ad-hoc: we knew roughly what we wanted, searched for it, downloaded what we found, and built collectors to keep it flowing. This worked well enough to get a trading system running, but the result is:

- **No inventory.** We can't answer "what data do we have?" without querying every table and reading collector source code. Coverage windows, freshness, and gaps are unknown without manual investigation.

- **Redundant and fragmented storage.** Three candle tables for the same exchange with different schemas, different scales (cents vs. decimals), and overlapping but incomplete coverage. A 62M-row table that's 95% NULL. Live and historical data coexisting without a defined splice.

- **Silent failures.** Collectors stop and nobody knows. Snapshots stopped flowing two weeks ago. The calibration pipeline is stale. Nothing alerts.

- **No standard for onboarding.** Each data source was a one-off project. Adding trade data meant writing a new downloader, designing a new table, and hoping it was consistent with the rest. The next data source will face the same problem.

- **No maintenance.** Tables that have never been analyzed. Useless indices on the two largest tables. Text timestamps where timestamptz is needed. No FK constraints where referential integrity matters.

## What the Data System Does

A data system that manages the lifecycle of datasets: onboarding, backfill, live collection, splice, health monitoring, and schema maintenance. Each new dataset plugs into this system rather than being wired up bespoke.

The system does NOT know about its consumers. It provides well-maintained, discoverable data. What consumers do with that data is their concern.

### Concepts

**Source**: An external provider of data (e.g., Kalshi, Polymarket, FRED). A source has API characteristics: endpoints, authentication, rate limits, historical availability.

**Dataset**: A specific type of data from a source (e.g., Kalshi trades, Kalshi hourly candles, Kalshi settled markets). A dataset has a schema, a resolution, a coverage window, and a natural-language description of what it represents and how it behaves. One source provides multiple datasets.

**Registry**: The catalog of all datasets — what exists, what it means, how it behaves, its coverage, its freshness, and its health. This is the answer to "what data do we have?"

**Backfill**: The process of downloading historical data for a dataset. Backfills are resumable, idempotent, and aware of what's already been downloaded.

**Collector**: A process that keeps a dataset current by fetching new data on a schedule. A collector writes to the same table as the backfill for that dataset.

**Splice**: When both backfill and collector produce data for the same dataset, the splice defines which version takes precedence. Historical data can correct or replace live-collected data, not just supplement it.

## Requirements

### R1: Dataset Registry

Every dataset in the system is registered. Registration includes:

**Identity and meaning:**
- Source, dataset name, storage location (currently a table name, but not necessarily always)
- Natural-language description: what this data represents, how it's produced, what it means. Not a schema comment — a description sufficient for an unfamiliar reader (human or AI) to understand the data's purpose, its quirks, and its appropriate uses. For example: "Hourly OHLC candles for Kalshi contracts. Prices are the yes-side bid/ask/last at each bar boundary. Volume is contract count, not dollar volume. OI is refreshed from market metadata every 2 hours, not continuously — it can lag intraday."
- Provenance: where does this data come from? Which API endpoints, what processing, what known limitations of the source.

**Behavior expectations (structured, machine-parseable):**
- Resolution: temporal granularity (tick, minute, hourly, daily, point-in-time)
- Expected update schedule: in a form that mechanical health checks can evaluate (e.g., a cron expression, an interval, or a structured rule like "weekdays only, by 10:00 EST")
- Expected coverage: earliest expected data point, latest expected lag (e.g., "T-7 to present", "full history back to 2021")

**Behavior expectations (unstructured, for human/AI readers):**
- Known gaps or caveats: "historical API doesn't return bid/ask for minute candles", "data before March 2026 has inverted bid/ask"
- Usage guidance: when this dataset is appropriate, when it's not, what its limitations are

The structured expectations drive mechanical health checks (R6). The unstructured descriptions provide context for intelligent diagnosis — an agent or person reading the registry can assess data quality and debug problems without reading source code. Both are required; they serve different purposes.

**Measured state** (computed, not declared):
- Actual coverage: earliest and latest data points, computed from table contents
- Actual freshness: when data was last written
- Health: whether measured state is consistent with behavior expectations

The registry is the single place to answer: what data do we have, what does it mean, how current is it, and is anything broken?

### R2: Schema Conventions

All datasets follow common conventions:

- Timestamps are stored in a native timestamp type with timezone information, not as strings.
- Prices use a consistent, documented representation within a source. Different scales across sources are acceptable; different scales within the same source's datasets are not.
- Every dataset has an explicit uniqueness constraint on its natural key.
- Where both backfill and live data coexist, provenance is tracked via an origin indicator (e.g., 'historical' vs. 'live') distinguishing how the row was acquired. (Note: "origin" for row provenance, "source" for the external provider — these are different concepts.)
- Naming follows a `{source}_{dataset}` convention.

For example, if using PostgreSQL: timestamps are `timestamptz`; uniqueness is enforced via PRIMARY KEY or UNIQUE constraint; provenance is an `origin` column (text); upserts use `ON CONFLICT ... DO UPDATE` or `DO NOTHING`.

These conventions are documented once and enforced at onboarding, not discovered after the fact.

### R3: Backfill

Each dataset that has historical data available defines a backfill process. Backfills are:

- **Resumable**: can be interrupted and restarted without data loss or duplication. Progress (e.g., last downloaded cursor, date range completed) is tracked in persistent state.
- **Idempotent**: running the same backfill twice produces the same result. Uses upsert semantics.
- **Incremental by default**: routine runs download data newer than what's already stored, extending coverage forward. However, backfills must also support re-downloading older ranges when the source has corrected or enriched historical data (see R5, R9). Re-backfill of older ranges is an explicit operation, not the default mode.

Resilience (throttling, retry, graceful degradation) follows R10.

Backfill state (what's been downloaded, what remains, when it last ran, any errors) is tracked and inspectable.

### R4: Live Collection

Each dataset that needs to stay current defines a collector. Collectors:

- Write to the same storage as the backfill, with provenance tracking to distinguish live from historical data.
- Run on a declared schedule (continuous, hourly, daily, weekly).
- Are idempotent per run (safe to restart, safe to overlap with a concurrent run via upsert).

Resilience (throttling, retry, graceful degradation, logging) follows R10.

### R5: Splice and Correction

When a dataset has both backfill and live data, the splice defines precedence:

- **Default precedence**: historical (backfill) data is preferred over live-collected data, because historical APIs are typically authoritative and may include corrections.
- **Overlap handling**: when historical data covers a period previously only covered by live data, the system supports replacing the live data. The mechanism depends on the dataset's temporal versioning posture (R9):
  - **Datasets using multi-version behavior**: superseded live rows are marked (`superseded_at` set) and the corrected historical row is inserted alongside. Both versions are retained for past-state reconstruction.
  - **Datasets not using multi-version behavior**: superseded live rows may be deleted or overwritten. The temporal columns still exist (R9) but only the current version is retained.
  The choice is made per-dataset at onboarding (R8).
- **Consumers see one coherent timeline** through the splice (current rows only by default). Origin and version history are available but not the default view.

### R6: Health Monitoring

The system detects when things go wrong:

- **Freshness checks**: compare actual freshness to the expected update schedule declared in the registry. A dataset expected to update every weekday morning that hasn't updated since Thursday is flagged.
- **Gap detection**: identify periods where data is expected but missing (e.g., a collector was down for 3 hours on a dataset with 5-minute resolution).
- **Coverage tracking**: actual earliest and latest data points, updated as data arrives.
- **Anomaly flags**: optional per-dataset checks (e.g., "volume should never be negative", "row count should increase monotonically").

Health status is both **queryable** (a single query answers "which datasets are healthy, which are stale, and which have gaps?") and **actively surfaced** (failures and staleness are pushed to a notification channel, not just recorded for someone to discover later). The problem this system exists to solve is "collectors stop and nobody knows" — passive queryability alone doesn't solve that.

The registry descriptions (R1) should be rich enough that an intelligent agent can go beyond mechanical checks — reading the description, examining the data, and assessing whether something looks wrong even if no specific check was pre-configured.

### R7: Storage Maintenance

The system maintains its storage in a state where queries perform well.

- Statistics are current: the query planner has accurate information about table sizes and value distributions.
- Indices justify their cost: every index has sufficient selectivity. Indices with very low cardinality are not created.
- Storage bloat is monitored and addressed.
- Maintenance runs on a schedule appropriate to each dataset's update frequency.

For example, if using PostgreSQL (which is likely): ANALYZE after bulk loads and periodically for continuously-written tables; autovacuum thresholds tuned for large tables; index usage statistics reviewed periodically with unused indices as removal candidates.

### R8: Onboarding Process

Adding a new dataset is a defined process, not a project:

1. **Discover**: what does the source offer? Document endpoints, resolution, rate limits, historical availability, known quirks.
2. **Design**: schema following conventions (R2) including temporal columns (R9). Choose natural key, decide on indices, define splice rules if both backfill and live apply. Decide whether multi-version behavior (R9) is needed.
3. **Describe**: write the registry entry (R1) — meaning, provenance, behavior expectations, known caveats.
4. **Backfill**: implement and run the backfill (R3) if historical data is available.
5. **Collect**: implement and enable the collector (R4) if live data is needed.
6. **Verify**: confirm coverage, freshness, and health (R6) match expectations.

Steps 1-3 are inherently per-source (every API is different). Steps 4-6 use shared infrastructure — throttling, retry, health checks, maintenance.

### R9: Temporal Versioning

Some datasets may need to answer the question: "what did we know at time T?" — not just "what happened at time T?" This matters when:

- Backtesting needs to reconstruct the data that was available at a past evaluation point (temporal discipline depends on this).
- Historical data corrects or replaces live data, and we need to know what the live data said before correction.
- Source data is revised after initial publication (e.g., economic indicators with preliminary and revised releases).

**The temporal columns are universal.** Every dataset includes a "recorded" timestamp (when the row was inserted) and a "superseded" timestamp (when it was replaced; null if current). The cost is negligible — a few bytes per row. The benefit is uniformity: no schema divergence, no code branches for "does this table have temporal columns?", and the option to use multi-version behavior later without migration.

For example, if using PostgreSQL (which is likely): `recorded_at timestamptz DEFAULT now()` and `superseded_at timestamptz DEFAULT NULL`.

**Multi-version behavior is per-dataset.** Most datasets will never have more than one version of a fact — the recorded timestamp gets set on insert and the superseded timestamp stays null forever. For datasets where corrections happen (live data replaced by historical, revised economic releases), the ingestion process marks old rows as superseded and inserts corrected versions. The choice is made at onboarding (R8); the schema supports it uniformly.

### R10: Ingestion Resilience

All processes that fetch data from external sources (backfills and collectors) share common resilience patterns:

- **Rate limiting**: respect source API limits. Throttling parameters (requests per second, concurrent connections) are configured per source, not hardcoded per script.
- **Retry with backoff**: transient failures (HTTP 429, 5xx, network timeouts) trigger exponential backoff with jitter. Permanent failures (4xx, authentication errors) do not retry.
- **Graceful degradation**: a failure in one dataset's ingestion does not block others. Partial progress is saved; the next run resumes from where it left off.
- **Logging**: every ingestion run records what it did, what failed, and why. This feeds health monitoring (R6).

## Non-Requirements

- **The system does not know about its consumers.** It doesn't track who reads what. Consumers discover available datasets and decide what to use.
- **The system does not transform data.** It stores what the source provides. Derived tables (like `settled_with_prices`) are downstream concerns, not part of the data system.
- **The system is not a query abstraction layer.** Consumers query storage directly (or through splice views). There is no ORM, no API wrapper, no middleware.
- **The system does not enforce a single schema across sources.** Kalshi and Polymarket have different data shapes. Conventions (R2) provide consistency within a source; cross-source uniformity is not a goal.

## What This Means for Existing Data

The existing 33 tables were not built to these requirements. Bringing them into compliance is part of the work, but the requirements are designed for steady-state operation, not as a one-time cleanup spec. The cleanup is a separate concern — it uses the system but isn't part of it.

Specific issues the audit identified (three redundant candle tables, all-NULL columns, text timestamps, missing ANALYZE, stale collections) will be addressed as part of onboarding the existing datasets into the new system. The expectation is not that existing tables will be rewritten overnight, but that each dataset, as it's touched, gets brought into compliance.
