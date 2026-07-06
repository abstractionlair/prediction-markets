# Data System: Roadmap

**Draft 4** | 2026-04-05

Parent: `data-system-requirements.md`

## Approach

We treat Kalshi as if it were a new data source being onboarded for the first time. The existing collectors, tables, and scripts are reference material — they tell us what the API offers, what we've learned about its quirks, and what consumers need. But we're not patching or incrementally fixing the existing implementation. We're going through the full onboarding process (R8) from discovery to verification, making design decisions with the benefit of hindsight.

Existing data and infrastructure continue operating as-is during this process. Migration happens when the new system is ready and verified, not by emergency fixes to the old one.

## Sequencing Principles

- **Understand the data before designing the system.** Discovery of what Kalshi offers comes first. The data system's design should be informed by real knowledge of the data it will manage, not built in the abstract.
- **Build infrastructure alongside real data, not in a vacuum.** Shared infrastructure (registry, resilience, health monitoring) is built while onboarding the first real dataset. This grounds abstractions in real use and avoids speculative design.
- **Prove the pattern on one dataset before scaling.** Onboard one Kalshi dataset end-to-end, then apply the same pattern to the rest.
- **Each phase delivers usable value.** No phase exists solely to enable a later phase.

## Cross-Cutting: Onboarding Procedure (R8)

The onboarding procedure is not a phase — it's a document that accumulates across phases. Each phase contributes to it:

- **Phase 1** produces the discovery template: what questions to ask about a new source, what to document, what to look for.
- **Phase 2** is the first real exercise of the procedure. We write down what we actually did, step by step, noting what was Kalshi-specific vs. generic.
- **Phase 3** exercises the procedure repeatedly on remaining datasets and refines it.
- **Phase 5** validates the procedure on different sources and refines what doesn't generalize.

By the end, the onboarding procedure is a tested, concrete document — not a theoretical process designed up front, but one forged by actually doing it multiple times.

## Phases

### Phase 1: Kalshi Discovery

**Goal:** Complete understanding of what Kalshi offers as a data source — endpoints, resolutions, historical availability, rate limits, quirks — and what datasets we want from it.

**Work:**

Catalog the Kalshi API:
- Every data endpoint: what it returns, at what resolution, how far back, with what rate limits
- Authentication and access tiers (standard vs. advanced)
- Historical API: what's available, what the cutoff is, how it advances, what fields are actually populated at each resolution (e.g., the lesson that minute candles don't include bid/ask/price)

Document what we learned from the existing implementation:
- What worked, what didn't, what surprised us
- Known data quirks (OI refresh lag, inverted bid/ask before March 2026, etc.)
- Where the existing implementation made choices we'd make differently

Identify the target dataset list:
- What datasets do we want? Trades, candles (at what resolution?), market metadata, settlement outcomes, orderbook snapshots, portfolio activity, classifications
- For each: does it have a historical API path, a live collection path, or both?
- What's the natural key, what's the resolution, what's the expected coverage?

**Delivers:** A complete Kalshi data catalog. Every design decision in later phases is informed by this. No code written, no schemas designed — just understanding.

### Phase 2: Infrastructure + Pilot Dataset

**Goal:** The data system infrastructure exists and is proven on one real dataset (Kalshi trades) end-to-end.

**Why Kalshi trades as the pilot:** Largest table (224M rows), has both historical and live data, has an overlap region (and a 2-month gap to close), heavily used by downstream consumers. Getting it right has immediate value. It exercises every requirement: registry, conventions, backfill, collection, splice, temporal columns, health monitoring, resilience.

**Work:**

Schema conventions (R2) — documented and applied to the pilot:
- Timestamp types, naming, natural keys, origin tracking, temporal columns (R9)
- Conventions are informed by Phase 1 discovery; validated against real Kalshi data

Registry (R1) — created with the pilot as its first entry:
- Design and create the registry store
- Define structured metadata schema (for health checks) and unstructured description format (for human/AI readers)
- Write the Kalshi trades registry entry: meaning, provenance, structured expectations, known caveats

Ingestion resilience (R10) — built while implementing the pilot's backfill and collector:
- Shared throttling/retry infrastructure: per-source rate limits, exponential backoff with jitter, graceful degradation, run logging
- Kalshi rate limits from Phase 1 inform the configuration

Pilot dataset (Kalshi trades) — full onboarding:
- Design: schema following conventions, natural key, origin tracking, temporal columns
- Backfill (R3): resumable, idempotent, incremental, using shared resilience. Close the 2-month gap.
- Collector (R4): on declared schedule, idempotent, using shared resilience
- Splice (R5): historical preferred, define overlap handling, decide multi-version posture
- Temporal versioning (R9): add recorded/superseded columns, validate the pattern works

Health monitoring (R6) — built while monitoring the pilot:
- Freshness checks, gap detection, coverage tracking driven by registry entry
- Active alerting: failures pushed to a notification channel
- Verified by simulating a collector outage

Storage maintenance (R7) — applied to the pilot:
- Maintenance scheduling for the new table
- Validate that statistics stay current as data flows

**Delivers:** A working data system with one real dataset flowing through it. Registry, resilience, health monitoring, and maintenance all proven on real data — not built speculatively. The onboarding procedure's first real exercise is documented.

### Phase 3: Remaining Kalshi Datasets

**Goal:** All Kalshi datasets onboarded through the proven system.

**Work:**

For each remaining dataset, follow the pattern from Phase 2:

Design and describe:
- Schema following conventions, registry entry with full metadata
- Splice rules where both backfill and live paths exist
- Multi-version posture decided per dataset

Candle consolidation (the hardest piece):
- Three legacy tables → one coherent candle dataset
- Design informed by Phase 1 discovery (what resolutions are actually useful, what fields are actually populated)
- Migrate the best-quality data from each legacy source

Remaining datasets:
- Market metadata (events + markets — currently split into live and settled hierarchies)
- Settlement outcomes (settled events + settled markets)
- Orderbook snapshots
- Portfolio activity (our own fills)
- Market classifications

Verify each:
- Coverage, freshness, health match registry expectations
- Health monitoring catches real problems

**Delivers:** All Kalshi data flowing through the new system, monitored and well-described. The onboarding procedure has been exercised multiple times and is now mature.

### Phase 4: Migration

**Goal:** Downstream consumers transitioned from old tables to new ones. Old infrastructure retired.

**Why a separate phase:** Migration is a different risk profile from building. It touches the live trader, replay engine, and calibration pipeline — systems that must not break silently. It deserves its own verification and rollback plan.

**Work:**

Coexistence strategy:
- New and old tables coexist during transition (different table names under the same schema, or a new schema — decided at spec time)
- Old collectors continue running until new ones are verified

Parity verification:
- For each dataset, compare new tables against old: row counts, value distributions, coverage windows
- Verify that downstream consumers produce equivalent results when pointed at new tables (e.g., replay produces the same TrackRecord)

Consumer cutover:
- Update MarketView, replay, weekly_pipeline, and trader to use new tables
- Run consumers in parallel against old and new during transition where feasible

Retirement:
- Drop old tables only after consumers are fully transitioned and verified
- Remove old collectors and scripts

**Delivers:** One coherent set of Kalshi tables. Old infrastructure retired. No dangling references, no redundant data.

### Phase 5: Remaining Sources

**Goal:** Non-Kalshi sources onboarded through the same system, validating that it's genuinely source-agnostic.

Ordered by complexity (simplest first to build confidence):

**5a: Benchmark data (FRED, CoinGecko, CBOE)**
- Simple time series, daily resolution, REST APIs
- Straightforward onboarding — validates the pattern on non-prediction-market data
- FRED and CoinGecko are the simplest (single-endpoint, daily); CBOE is slightly more complex (options chain structure)

**5b: PredictIt**
- Similar structure to Kalshi (markets, contracts, snapshots) but smaller and less actively maintained
- Tests whether the system handles a lower-activity source gracefully

**5c: Polymarket**
- Complex: 167K markets, 9.3M snapshots, decentralized data sources (subgraph)
- Most different from Kalshi in data access patterns
- The true test of source-agnosticism

**Delivers:** The full data estate managed by one system. The onboarding procedure has been validated across diverse sources. Adding the next source is a known process.

## What This Roadmap Does Not Cover

- **Derived tables** (settled_with_prices, calibration tables). These are downstream consumers. They'll benefit from better source data but their redesign is separate.
- **New data sources** not yet collected. The system makes onboarding easy; identifying and prioritizing new sources is a separate conversation.
- **Existing infrastructure fixes.** The old collectors and tables continue as-is until replaced in Phase 4. We don't invest in patching them.
