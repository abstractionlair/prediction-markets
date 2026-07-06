# Prediction Markets Data Architecture

## Purpose

This document defines the complete data universe for the prediction markets research project. It serves as the reference for what data exists, how it stays current, and what derived tables depend on it. Any new collector, analysis, or pipeline change should be evaluated against this document for closure.

## Entity Relationship

```
market_classifications ──── series_ticker ────┐
                                              │
kalshi_events ──── event_ticker ──── series_ticker
       │
       │ event_ticker
       │
kalshi_markets ──── ticker ──── event_ticker
       │                           │
       │ ticker                    │ event_ticker
       │                           │
kalshi_snapshots              kalshi_settled_events (category, settled_at)
(bid, ask, 5-min)                  │
       │                           │ event_ticker
       │ ticker                    │
       │                    kalshi_settled_markets (result, settled_at)
       │                           │
       ├───────── ticker ──────────┤
       │                           │
finfeed_ohlcv                      │ ticker
(OHLC, daily,                      │
 market_id = ticker + '_YES')      │
                                   │
                          settled_with_prices (DERIVED)
                          (last_price, bid, ask, process_category, result)
```

## Primary Tables

### Prediction Market Data

| Table | Source | Rows | Coverage | Key | Update Mechanism |
|---|---|---|---|---|---|
| `kalshi_trades` | Kalshi API (historical + live) | ~228M+ | Jul 2021 → ongoing | trade_id | Daily timer 06:00 UTC (data system) |
| `kalshi_candles` | Kalshi API (historical + batch) | ~62M+ | Jul 2021 → ongoing | (ticker, period_end, resolution) | Hourly timer :30 UTC (data system) |
| `kalshi_events` | Kalshi API (discovery) | ~20K | Active events + legacy | event_ticker | Every 30 min (data system) |
| `kalshi_markets` | Kalshi API (discovery) | ~327K | Active markets + legacy | ticker | Every 30 min (data system) |
| `kalshi_snapshots` | Kalshi API (orderbook) | ~7.5M+ | Feb 17, 2026 → ongoing | (ticker, timestamp) | Continuous (data system) |
| `kalshi_settled_markets` | Kalshi API (settled endpoint) | ~4.5M+ | All time (backfilled) | ticker | Weekly Sunday 06:00 UTC (data system) |
| `kalshi_settled_events` | Kalshi API (settled events endpoint) | ~62K+ | All time | event_ticker | Weekly Sunday 06:00 UTC (data system) |
| `finfeed_ohlcv` | FinFeed bulk import | ~9.5M | Sep 2025 – Mar 14, 2026 | (exchange, market_id, date) | Static archive, not updated |
| `polymarket_markets` | Polymarket API | ~167K | Feb 15, 2026 → ongoing | market_id | Collector refresh every 5-min |
| `polymarket_snapshots` | Polymarket API | ~9.3M+ | Feb 15, 2026 → ongoing | (id) | Continuous, 5-min |

Note: `kalshi_trades`, `kalshi_candles`, `kalshi_snapshots`, `kalshi_events`, `kalshi_markets`, `kalshi_settled_events`, and `kalshi_settled_markets` are managed by the data system framework (`collectors/kalshi/`, `data/`). They have `origin`, `recorded_at`, `superseded_at` columns, registry entries in `dataset_registry`, and health monitoring. `kalshi_candles` stores minute, hourly, and daily resolutions in one table with three registry entries. `kalshi_snapshots` has legacy data (Feb-Mar 2026, `origin='legacy'`). `kalshi_events` and `kalshi_markets` are metadata tables (overwritten, not appended) — the canonical "what's active" source for other collectors. `kalshi_settled_events` and `kalshi_settled_markets` are append-only settlement outcome tables.

### Benchmark Data

| Table | Source | Rows | Coverage | Key | Update Mechanism |
|---|---|---|---|---|---|
| `fred_observations` | FRED API | ~203K | 1947 – present (series-dependent) | (series_id, date) | Daily timer 06:00 UTC |
| `cboe_snapshots` + `cboe_options` | CBOE delayed quotes | ~26K opts/snapshot | Mar 15, 2026 → ongoing | (id) / (snapshot_id) | Daily timer 20:30 UTC |
| `coingecko_observations` | CoinGecko API | ~2.2K | 1 year history | (series_id, date) | Daily timer 07:00 UTC |

### Classification

| Table | Source | Rows | Coverage | Key | Update Mechanism |
|---|---|---|---|---|---|
| `market_classifications` | Market Scout (LLM) | ~1,247 | All known series | series_ticker | Weekly pipeline (new series only) |

### Derived Tables

| Table | Depends On | Purpose | Refresh Mechanism |
|---|---|---|---|
| `settled_with_prices` | settled_markets + snapshots + finfeed + classifications + settled_events | Pre-joined table for calibration analysis | Weekly pipeline (rebuild) |

## Ticker Mapping

Kalshi tickers have changed format over time:
- **Current format**: `KXINX-26MAR12H1600-B6625` (KX prefix)
- **Historical format**: `INX-22APR28-B6625` (no KX prefix)
- **FinFeed format**: `KXINX-26MAR12H1600-B6625_YES` (append `_YES` or `_NO`)

Classification matching uses fallback chain:
1. `kalshi_events.series_ticker` (exact, for active events)
2. `'KX' + split_part(event_ticker, '-', 1)` (add KX prefix for old tickers)
3. `split_part(event_ticker, '-', 1)` (direct prefix match)

FinFeed price matching: `finfeed_ohlcv.market_id = kalshi_settled_markets.ticker || '_YES'`

## Weekly Pipeline

Runs as a single orchestrated job. Each step is idempotent.

### Step 1: Classify new series

```
market_scout.py (on unclassified series only)
```
- Checks `kalshi_events` for series_tickers not in `market_classifications`
- Runs LLM classification with mechanical hints as priors
- Writes to `market_classifications`

### Step 2: Settled outcomes (data system)

Handled by `collectors/kalshi/settled.py` via `kalshi-settled-sync.timer` (Sunday 06:00 UTC, before pipeline).
Full re-download of all settled events with nested markets (~240K events, ~2.7M markets).
The weekly pipeline reports current counts but no longer downloads.

### Step 3: Fill categories for uncategorized settled events

```sql
-- Map old-format tickers to classifications
UPDATE kalshi_settled_events se
SET category = CASE
    WHEN mc.process_category = 'financial_settlement' THEN 'Financials'
    WHEN mc.process_category = 'economic_data_release' THEN 'Economics'
    WHEN mc.process_category = 'convergent_binary' THEN 'Elections'
    WHEN mc.process_category = 'policy_decision' THEN 'Politics'
    WHEN mc.process_category = 'entertainment' THEN 'Entertainment'
    WHEN mc.process_category = 'hazard_decay' THEN 'Politics'
    ELSE mc.process_category
END
FROM market_classifications mc
WHERE (se.category IS NULL OR se.category = '')
  AND mc.series_ticker = 'KX' || split_part(se.event_ticker, '-', 1);
```

### Step 4: Rebuild `settled_with_prices`

```sql
DROP TABLE IF EXISTS settled_with_prices;
CREATE TABLE settled_with_prices AS
-- (see build script for full query)
-- Joins: settled_markets + snapshots/finfeed + classifications + settled_events
-- Applies spread filter (≤20c) for snapshot data
-- FinFeed has no spread info, included as-is
```

## Price Sources and Selection

For a given settled market, the best available price is selected:

1. **Kalshi snapshots** (preferred): 5-min bid/ask. Mid = (bid + ask) / 2. Spread filter ≤ 20c. Available Feb 17, 2026 onward.
2. **FinFeed OHLCV** (fallback): Daily close price. No bid/ask or spread. Available Sep 2025 – Mar 14, 2026.

The "last price before settlement" is: the most recent observation from either source where `observed_at < settled_at`.

For snapshot data, we also retain `bid` and `ask` separately for robustness analysis (calibration using bids vs asks vs mids).

## Scope Decisions

**Included in analysis:**
- Financial settlement (equities, commodities, FX, rates)
- Economic data releases (CPI, GDP, payrolls, etc.)
- Crypto (BTC, ETH, SOL, etc.) — for completeness
- Politics and elections — important for cross-domain comparison
- Climate and weather — if sufficient data

**Included but flagged:**
- Entertainment — some markets have pre-determined outcomes (reality TV filmed in advance). Analysis should note this caveat.
- Sports — large dataset but not central to the paper's argument. Available for completeness.

**Excluded from primary analysis:**
- Mentions (social media mention counts)
- Education, Health, Social (tiny samples)

## Known Limitations

1. **No snapshot data before Feb 17, 2026** — cannot be backfilled
2. **No CBOE options data before Mar 15, 2026** — collecting going forward
3. **FinFeed is a static archive** — Sep 2025 to Mar 14, 2026, not updating
4. **kalshi_markets only has metadata for currently active markets** — rules_primary, strike_type, floor_strike are not available for historical settled markets
5. **Entertainment market integrity** — some outcomes are pre-determined (e.g., Survivor filmed months before airing). Classification doesn't distinguish these.
6. **Settled market volume from API is 0** — Kalshi zeroes volume after settlement. We have no historical volume for settled markets except via FinFeed's volume_traded.
7. **Old ticker formats** — pre-KX tickers (INX-, INXU-) require prefix mapping for classification joins

## Systemd Timers

| Timer | Schedule | Runs |
|---|---|---|
| `kalshi-snapshots` | Continuous | Orderbook snapshots for all active markets (data system) |
| `kalshi-discovery.timer` | Every 30 min | Event + market metadata refresh (data system) |
| `kalshi-trades-sync.timer` | Daily 06:00 UTC | Live trade ingestion (data system) |
| `kalshi-candles-sync.timer` | Hourly :30 UTC | Hourly + daily candle collection (data system) |
| `kalshi-settled-sync.timer` | Weekly Sun 06:00 UTC | Settlement outcome downloads (data system) |
| `data-health-alert.timer` | Every 30 min | Dataset health checks + alerting |
| `polymarket-collector` | Continuous | Snapshots + market discovery |
| `predictit-collector` | Continuous | Snapshots |
| `cboe-collector.timer` | Daily 20:30 UTC | SPX options chain |
| `fred-collector.timer` | Daily 06:00 UTC | 34 FRED series |
| `coingecko-collector.timer` | Daily 07:00 UTC | 6 crypto assets |
| `settled-downloader.timer` | Weekly Sun 08:00 UTC | Settled outcomes + pipeline |

## Memory Limits

All collectors have systemd memory limits to prevent OOM:
- Continuous: MemoryHigh=400M, MemoryMax=512M
- One-shot: MemoryHigh=128-256M, MemoryMax=256-384M

## File Locations

- Collector scripts: `collectors/`
- Systemd service files: `collectors/services/`
- Analysis scripts: `research/`
- This document: `DATA_ARCHITECTURE.md`

## Database

- PostgreSQL 17
- Schema: `prediction_markets`
- Connection: `CLAUDE_HUB_PG_DSN` env var
