# prediction-markets

A source-agnostic data ingestion framework, a PostgreSQL warehouse, and a
quant consumer layer for prediction-market and macro-finance data. The
distinguishing property of this codebase is not any particular strategy or
result but the **temporal-integrity discipline** built into the framework:
at the estimator/View interface, look-ahead bias is structurally prevented,
not merely discouraged.

This is **infrastructure**, not "a trading system." Strategies and models plug
in through standard interfaces; the infrastructure enforces correctness
properties regardless of which specific component is connected.

## What it is

Three layers:

1. **Collectors** (`collectors/`) — source-specific ingestion scripts that pull
   from prediction-market exchanges and macro-finance data sources into a
   common warehouse schema. Sources covered:
   - **Kalshi** — trades, multi-resolution candles, continuous orderbook
     snapshots, event/market discovery, settlement outcomes (managed by the
     data-system framework under `collectors/kalshi/`)
   - **Polymarket** — continuous 5-min snapshots + market discovery
   - **PredictIt** — continuous 5-min price snapshots
   - **CBOE** — daily SPX options chains
   - **FRED** — daily economic series (Treasury yields, CPI, PCE, employment,
     GDP, commodities, breakevens, housing)
   - **CoinGecko** — daily crypto prices

2. **Data-system framework** (`data/`) — source-agnostic infrastructure for
   managing datasets over their full lifecycle: registration, ingestion,
   versioning, and health monitoring. See below.

3. **Quant consumer layer** (`framework/`, `trading/`, `research/`) —
   calibrated estimators, strategies, a replay/backtest engine, and a live
   trader. Consumers access data exclusively through a temporally-bounded
   `MarketView`, never directly.

Warehouse scale (rough, at time of snapshot): ~228M trades, ~62M candles
(multi-resolution), ~7.5M Kalshi snapshots, ~167K Polymarket markets, ~9.3M
Polymarket snapshots, ~203K FRED observations. Full table documentation in
`SCHEMA.md`; data universe and pipeline steps in `DATA_ARCHITECTURE.md`.

## The data-system framework

A registry-driven lifecycle for every dataset, so that adding a new source
follows an established pattern and requires no changes to downstream code
until a consumer explicitly opts in.

- **Dataset registry** (`data/registry.py`) — every dataset declares what it
  contains, its coverage window, its update cadence, and what depends on it.
  The registry is the single place to answer "what data do we have, how fresh
  is it, and is anything broken?"
- **Cross-process rate limiting** (`data/ingestion/rate_limiter.py`) —
  per-source QPS enforcement shared across collectors so concurrent processes
  don't blow past API limits.
- **Retry + run logging** (`data/ingestion/retry.py`, `run_logger.py`) —
  bounded retry with backoff and a structured run log per ingestion attempt.
- **Temporal versioning** — registry-managed tables carry `origin`,
  `recorded_at`, and `superseded_at` columns so that the *state of the world
  as it was known at time T* is recoverable, not just the latest row. Metadata
  tables are overwritten; observation tables are append-only.
- **Health monitoring** (`data/health/`) — per-dataset anomaly checks
  (freshness, volume, gap detection) with alerting. Runs on a timer.

## Temporal-integrity discipline

The core design invariant. Every calibrated component operates behind a
**temporal boundary** defined by an `as_of` date — the point in time up to
which data is visible.

`MarketView(as_of=T)` (`trading/market_view.py`) is the single handle through
which strategies and models access data. It provisions estimators using only
information available before `T`, and strategies receive data only through
the view — no database connections, no imports of calibration modules, no
side channels. Estimators receive **pre-filtered data**, not the full dataset
plus a cutoff date. An estimator cannot look ahead because it never sees
future data.

This makes look-ahead bias **structurally prevented at the estimator/View
interface**: there, the operation that would produce it is not available.
Enforcement is layered (see `framework/view.py`): feature availability times
are *validated* at View construction, while `as_of` privacy and the
no-direct-DB rule are *conventions* — Python has no access control, and code
paths outside the View boundary (preloaded features passed without a filter,
the live trader's direct calibration lookups) rely on that discipline rather
than on structure. The same boundary applies in backtesting (expanding-window
replay) and in production (`MarketView.from_db` with `as_of=now`).

The framework extends this discipline to other classes of error:

- **Cost realism** — every evaluation includes realistic transaction costs;
  the cost model is not optional in backtest or live paths.
- **Validation before deployment** — the framework defines a temporal-split
  validation interface (train pre-cutoff, test post-cutoff, record the gap
  metric), but `Runner.validate_estimator` is currently a stub raising
  `NotImplementedError` (implementation planned for Chunk 8+, pending an
  actuals-query interface). Strategies do produce a `TrackRecord` via
  out-of-sample replay.
- **Declared dependencies** — what data a model needs and what temporal
  constraints apply are declared and inspectable, not implicit in import
  chains.

See `docs/design/infrastructure-vision.md` for the full treatment and
`docs/design/feature-framework-spec.md` for the model/strategy protocol
specification.

## Repository layout

```
collectors/          Source-specific ingestion (Python)
  kalshi/            Kalshi data-system collectors (trades, candles,
                     snapshots, discovery, settled)
  services/          systemd unit + timer files
  market_scout.py    LLM-powered market classifier
  weekly_pipeline.py Orchestrates weekly tasks (classify, materialize)
data/                Source-agnostic data-system framework
  registry.py        Dataset registry
  ingestion/         RateLimiter, with_retry, RunLogger
  health/            Health checks, alerting, per-dataset anomaly checks
framework/           Quant consumer layer (protocol-driven)
  feature.py         Feature abstraction (stored/cached/computed)
  view.py            View — capability boundary for consumers
  estimator.py       Estimator protocol
  runner.py          Runner — generic strategy replay (validation stub)
  calibration_store.py  Calibration artifact storage
trading/             Strategies, replay engine, live trader, risk
research/            Calibration methodology + analysis scripts
scripts/             Utility + retention scripts
tests/               Unit + integration tests
docs/                Design specs + Kalshi mechanics primer
  design/            Specifications (data-system, feature-framework,
                     fill-model, infrastructure-vision)
  TRADING_MECHANICS.md  Kalshi market mechanics reference
SCHEMA.md            Warehouse schema documentation
DATA_ARCHITECTURE.md Data universe, pipeline steps, coverage
MARKET_TAXONOMY.md   4-dimension market classification
```

## Configuration

All secrets and connection parameters are read from environment variables —
no credentials are hardcoded. Key variables:

- `CLAUDE_HUB_PG_DSN` — PostgreSQL connection string for the warehouse
- `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` — Kalshi API credentials
- `FRED_API_KEY`, `COINGECKO_API_KEY`, `FINFEED_API_KEY` — source API keys

Systemd unit files in `collectors/services/` are provided as deployment
templates; paths are genericized and should be adjusted to the target host.

## Honest state

This is a **single-operator** research codebase. Development is
**spec-driven**: non-trivial components are specified before implementation
(see `docs/design/*-spec.md`) and reviewed through a **multi-model design
review** process where independent models critique each spec before it
advances to implementation. Reviewer attributions are recorded in each spec's
header.

The infrastructure is the part that compounding builds on. **Backtest P&L
figures produced by this system should not be treated as trustworthy
performance claims.** The owner's own analysis found that headline replay
returns were inflated by temporal leakage and fill-simulation bias in earlier
model iterations; the framework reforms documented in
`docs/design/infrastructure-vision.md` and the fill-model specs are the
response to those findings. The value on offer here is the data pipeline and
the temporal-integrity framework, not a validated edge.

## License

MIT — see `LICENSE`.
