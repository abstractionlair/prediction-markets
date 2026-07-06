# Kalshi Data Catalog

**Phase 1 Discovery** | 2026-04-05

Parent: `data-system-roadmap.md` (Phase 1)

## 1. API Overview

Base URL: `https://api.elections.kalshi.com/trade-api/v2`
Authentication: RSA-PSS signing (key_id + private_key.pem)
Data format: JSON, fixed-point dollars (strings like `"0.9300"`), fixed-point counts (strings like `"5.00"`)

### 1.1 Access Tiers

| Tier | Read QPS | Write QPS | Qualification |
|------|----------|-----------|---------------|
| Basic | 20 | 10 | Signup |
| Advanced | 30 | 30 | Application form |
| Premier | 100 | 100 | 3.75% monthly volume |
| Prime | 400 | 400 | 7.5% monthly volume |

We are on Advanced tier (30 QPS). Write limits apply only to order operations; batch cancels count as 0.2 each. Kalshi reserves the right to downgrade Premier/Prime for inactivity.

### 1.2 Historical / Live Data Split

Kalshi partitions data by a moving cutoff retrieved via `GET /historical/cutoff`:

| Cutoff Key | Meaning |
|------------|---------|
| `market_settled_ts` | Markets settled before this → historical endpoints only |
| `trades_created_ts` | Trades before this → historical endpoints only |
| `orders_updated_ts` | Orders before this → historical endpoints only |

The live window is ~2-3 months and advances over time. Events and series are always available via live endpoints regardless of cutoff. March 6, 2026 was the target date for removing historical data from live endpoints.

**Implication for us:** We must hit both historical and live endpoints to get complete coverage. Our `historical_downloader.py` and `kalshi_collector.py` already do this, but the splice point needs explicit management (R5).

### 1.3 Entity Hierarchy

```
Series (e.g., KXBTC)
  └── Event (e.g., KXBTC-26APR04)
        └── Market (e.g., KXBTC-26APR04-B83500)
```

- **Series**: A recurring contract type. Has a `series_ticker`. Contains events.
- **Event**: A specific instance of a series. Has an `event_ticker`, `category`, `mutually_exclusive` flag. Contains markets.
- **Market**: A single YES/NO contract at a specific strike. Has a `ticker`, `strike_type`, `floor_strike`, `cap_strike`. Terminal states: `finalized`.

Market `status` progression: `initialized` → `active` → `closed` → `determined` → (`disputed` → `amended` →) `finalized`.

Additional concepts:
- **Milestones**: Connect events to real-world occurrences (sports games, economic releases). Have `start_date`, `end_date`, related event tickers.
- **Structured Targets**: Real-world entities (teams, players) that markets reference. Markets with `strike_type: "structured"` have a `custom_strike` containing a structured target ID.
- **Multivariate Events (MVE)**: Dynamically-created combo events (ticker prefix `KXMVE*`). Separate endpoint: `GET /events/multivariate`.

## 2. Endpoint Catalog

### 2.1 Market Data (Public/Authenticated)

| Endpoint | Method | Description | Auth | Key Params |
|----------|--------|-------------|------|------------|
| `/markets` | GET | List markets | Optional | status, series_ticker, event_ticker, tickers (comma-sep), limit (1-1000), cursor |
| `/markets/{ticker}` | GET | Single market | Optional | — |
| `/markets/{ticker}/orderbook` | GET | Current orderbook | Required | depth (0-100) |
| `/markets/orderbooks` | GET | Batch orderbooks | Required | tickers (array, 1-100) |
| `/markets/trades` | GET | Recent trades | Optional | ticker, min_ts, max_ts, limit (1-1000), cursor |
| `/series/{s}/markets/{t}/candlesticks` | GET | Live candles | Optional | start_ts, end_ts, period_interval (1\|60\|1440), include_latest_before_start |
| `/markets/candlesticks` (batch) | GET | Batch candles | Optional | market_tickers (up to 50), start_ts, end_ts, period_interval |

### 2.2 Events & Series

| Endpoint | Method | Description | Auth | Key Params |
|----------|--------|-------------|------|------------|
| `/events` | GET | List events | Optional | status, series_ticker, with_nested_markets, with_milestones, limit (1-200), cursor, min_close_ts, min_updated_ts |
| `/events/multivariate` | GET | MVE events | Optional | series_ticker, collection_ticker, with_nested_markets, limit (1-200), cursor |
| `/events/{ticker}` | GET | Single event | Optional | with_nested_markets |
| `/events/{ticker}/metadata` | GET | Event metadata | Optional | — |
| `/series/{s}/events/{t}/candlesticks` | GET | Aggregated event candles | Optional | start_ts, end_ts, period_interval |
| `/series/{s}/events/{t}/forecast_percentile_history` | GET | Forecast history | Required | percentiles (array, max 10), start_ts, end_ts, period_interval |
| `/series/{ticker}` | GET | Single series | Optional | — |
| `/series` | GET | List all series | Optional | — |

### 2.3 Historical Data

| Endpoint | Method | Description | Auth | Key Params |
|----------|--------|-------------|------|------------|
| `/historical/cutoff` | GET | Current cutoff timestamps | Optional | — |
| `/historical/trades` | GET | All historical trades | Optional | ticker, min_ts, max_ts, limit, cursor |
| `/historical/markets` | GET | Archived markets | Optional | tickers, event_ticker, limit, cursor |
| `/historical/markets/{ticker}` | GET | Single archived market | Optional | — |
| `/historical/markets/{ticker}/candlesticks` | GET | Historical candles | Optional | start_ts, end_ts, period_interval |
| `/historical/fills` | GET | Our historical fills | Required | ticker, max_ts, limit, cursor |
| `/historical/orders` | GET | Our archived orders | Required | ticker, max_ts, limit, cursor |

### 2.4 Portfolio (Authenticated)

| Endpoint | Method | Description | Key Params |
|----------|--------|-------------|------------|
| `/portfolio/balance` | GET | Account balance | subaccount |
| `/portfolio/positions` | GET | Open positions | ticker, event_ticker, limit, cursor |
| `/portfolio/orders` | GET | Orders by status | status, ticker, event_ticker, limit, cursor |
| `/portfolio/orders` | POST | Create order | ticker, side, action, count, yes_price, type, post_only |
| `/portfolio/orders/batched` | POST | Batch create (up to 20) | orders array |
| `/portfolio/orders/{id}` | DELETE | Cancel order | — |
| `/portfolio/orders/batched` | DELETE | Batch cancel (up to 20) | order_ids array |
| `/portfolio/orders/{id}/amend` | POST | Modify price/count | — |
| `/portfolio/fills` | GET | Our fills | ticker, order_id, min_ts, max_ts, limit, cursor |
| `/portfolio/settlements` | GET | Settlement history | ticker, event_ticker, min_ts, max_ts, limit, cursor |

### 2.5 WebSocket Channels

| Channel | Data | Use Case |
|---------|------|----------|
| `orderbook_delta` | Incremental orderbook updates | Real-time book tracking |
| `ticker` | Market ticker data (price, volume, OI) | Live price feed |
| `trade` | Public trade executions | Trade tape |
| `fill` | Our fill notifications | Order monitoring |
| `user_orders` | Our order updates | Order state tracking |
| `market_lifecycle_v2` | Market status changes | Settlement/close detection |
| `multivariate_market_lifecycle` | MVE market changes | MVE tracking |
| `market_positions` | Position changes | Portfolio monitoring |
| `order_group_updates` | Order group state | Group monitoring |
| `communications` | RFQ/quote messages | Block trading |

### 2.6 Other Endpoints (Not Data Collection Targets)

- **Exchange**: `GET /exchange/status`, `/exchange/schedule`, `/exchange/announcements`
- **Account**: `GET /account/limits` (check our tier)
- **Search/Discovery**: `GET /search/tags_by_categories`, `/search/filters_by_sport`
- **Milestones**: `GET /milestones`, `/milestones/{id}`
- **Structured Targets**: `GET /structured_targets`, `/structured_targets/{id}`
- **Live Data**: `GET /live_data/milestone/{id}` (play-by-play sports stats)
- **Incentives**: `GET /incentive_programs`
- **FCM**: Futures commission merchant endpoints (not applicable)
- **Communications**: RFQ/quote system (block trading)
- **FIX Protocol**: Alternative protocol for order entry (not applicable)

## 3. Candlestick Field Analysis

This was a hard-won lesson. The candlestick schema has changed over time and fields are not always populated.

### 3.1 Current Schema (Fixed-Point Dollars)

Each candle contains:
- `end_period_ts`: Period end as unix timestamp
- `yes_bid`: `{open_dollars, high_dollars, low_dollars, close_dollars}` — best YES buy
- `yes_ask`: `{open_dollars, high_dollars, low_dollars, close_dollars}` — best YES sell (= 100 - NO buy)
- `price`: `{open_dollars, high_dollars, low_dollars, close_dollars, mean_dollars, previous_dollars, min_dollars, max_dollars}` — last trade price. **All nullable** — null when no trades occurred in the period.
- `volume_fp`: Contracts traded
- `open_interest_fp`: Contracts held at period end

### 3.2 Resolution-Dependent Behavior

| Resolution | bid/ask | price | volume | OI | Notes |
|-----------|---------|-------|--------|-----|-------|
| 1 min | Populated | Often null (no trade in 1 min) | Populated | Populated | Most granular but sparse price data |
| 60 min | Populated | Usually populated | Populated | Populated | Best balance of coverage and resolution |
| 1440 (daily) | Populated | Usually populated | Populated | Populated | Coarsest; good for long-term analysis |

**Critical lesson from existing implementation:** The original `kalshi_candlesticks` table stored minute candles but only captured `yes_bid_open/close` and `yes_ask_open/close` — missing price OHLC entirely (62M rows with NULL price columns). The `kalshi_candles` table (newer) stores all fields correctly but only has 1-min historical data so far.

### 3.3 Legacy Format vs Current

The API migrated from integer cents to fixed-point dollar strings. Our `_parse_candle_batch` in `historical_downloader.py` handles both formats:
- Old: `yes_bid.close` (integer cents)
- New: `yes_bid.close_dollars` (string like `"0.9300"`)

The `volume` and `open_interest` fields similarly migrated to `volume_fp` and `open_interest_fp`.

## 4. Market Object Field Inventory

The market object is rich. Key fields grouped by use:

**Identity**: `ticker`, `event_ticker`, `market_type` (binary|scalar), `yes_sub_title`, `no_sub_title`

**Timing**: `created_time`, `open_time`, `close_time`, `expected_expiration_time` (nullable), `latest_expiration_time`, `settlement_timer_seconds`, `settlement_ts` (nullable)

**Pricing** (real-time): `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `last_price_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`

**Previous prices** (24h ago): `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars`

**Volume/OI**: `volume_fp`, `volume_24h_fp`, `open_interest_fp`

**Strike structure**: `strike_type` (greater|greater_or_equal|less|less_or_equal|between|functional|custom|structured), `floor_strike`, `cap_strike`, `functional_strike`, `custom_strike`

**Rules**: `rules_primary`, `rules_secondary`, `early_close_condition`

**Settlement**: `status`, `result` (yes|no|scalar|empty), `settlement_value_dollars`, `expiration_value`

**Other**: `can_close_early`, `fractional_trading_enabled`, `notional_value_dollars`, `is_provisional`, `mve_collection_ticker`, `mve_selected_legs`, `price_level_structure`, `price_ranges`

## 5. What We Learned From Existing Implementation

### 5.1 Things That Worked

1. **Dual-path settled download** (per-series + events endpoint) gives broad coverage. Per-series is deep for known series; events endpoint catches categories like Politics/Entertainment.
2. **Monthly-window trade download** with resume capability. 228M trades downloaded successfully.
3. **Volume/OI cache** in collector avoids per-ticker SELECTs. Good pattern.
4. **ON CONFLICT DO NOTHING** for idempotent inserts across all downloaders.
5. **Market discovery via events endpoint** with `with_nested_markets=true`. More efficient than N+1 per-event market calls.
6. **`market_structure` derivation** from `strike_type` patterns at discovery time.

### 5.2 Things That Went Wrong

1. **62M-row candle table with all-NULL price columns.** `kalshi_candlesticks` only stored bid/ask OHLC, never price OHLC. Discovered late. 95% of the data in the table is NULL.
2. **Three overlapping candle tables.** `kalshi_candlesticks` (legacy daily, 377K rows), `kalshi_hourly_candles` (legacy hourly, 2.8M rows), `kalshi_candles` (new unified, 62M rows 1-min only). Different schemas, different units (cents vs dollars), different column names.
3. **Snapshot collection gap.** Only 1 month of snapshots (2026-02-17 to 2026-03-21, 7.6M rows). The collector was presumably restarted or broken before/after this window.
4. **Text timestamps.** `settled_at` in `kalshi_settled_markets` stored as text, requiring `::timestamptz` casts everywhere. Should be native `timestamptz`.
5. **`kalshi_trades` never analyzed.** PostgreSQL planner estimated 17.8M rows vs actual 228M, causing poor query plans.
6. **14-day data gaps** in snapshot and candle tables from collector outages, with no alerting.
7. **Title mismatch between live and settled events.** 66% of event titles differ between `kalshi_events` and `kalshi_settled_events` because the live table captures titles at discovery time while settled captures them at settlement.
8. **FinFeed as an unnecessary intermediary.** 9.6M rows of FinFeed OHLCV data that duplicates what the Kalshi API provides directly. Was added before we had the historical API.
9. **SNAPSHOT_SERIES hardcoded list.** Old collector only snapshotted ~60 curated series. Fixed: new collector snapshots all markets with OI > 0 (~24K markets).
10. **No source/origin tracking on snapshots.** Fixed: `kalshi_snapshots` now has `origin` column ('legacy' for pre-data-system, 'live' for new collection).

### 5.3 Known API Quirks

1. **OI refresh lag.** Open interest values from the API can lag behind actual state.
2. **Inverted bid/ask.** Polymarket (not Kalshi) had inverted bid/ask before March 2026. Kalshi's orderbook returns `yes` (YES bids) and `no` (NO bids); `yes_ask = 100 - no_bid`. Easy to confuse.
3. **Pagination via cursor.** All list endpoints use cursor-based pagination. Empty cursor or empty results array = last page.
4. **Rate limiting at 429.** Returns HTTP 429. Our code uses exponential backoff with jitter (good).
5. **Historical candle 404s.** Many tickers return 404 on the historical candles endpoint — the market may have had no trades, or the data isn't available.
6. **Batch candle endpoint quirk.** `GET /markets/candlesticks` accepts `market_tickers` as comma-separated in query string, not as array. Signing path must strip query params.
7. **Fixed-point migration.** API transitioned from integer cents to fixed-point dollar strings. Both formats may appear depending on when data was fetched. Our code handles both via `_parse_candle_batch`.
8. **`volume_fp` vs `volume`.** Older responses use `volume`; newer use `volume_fp`. Same for `open_interest_fp`. In nested event responses (`/events?with_nested_markets=true`), the integer `volume` and `open_interest` fields return 0 for all markets — only the `_fp` string variants have real values. This was the root cause of the old snapshot collector breaking in March 2026.
9. **`orderbook_fp` vs `orderbook`.** The orderbook endpoint (`GET /markets/{ticker}/orderbook`) now returns `orderbook_fp` with `yes_dollars`/`no_dollars` (arrays of `[price_dollars_string, quantity_string]`) instead of `orderbook` with `yes`/`no` (arrays of `[price_cents_int, quantity_int]`). The snapshot collector handles both formats.

## 6. Target Dataset List

Based on what the API offers, what our consumers need, and lessons from the existing implementation.

### 6.1 Primary Datasets (High Priority)

| Dataset | Description | Historical Path | Live Path | Natural Key | Resolution | Est. Scale |
|---------|-------------|-----------------|-----------|-------------|------------|------------|
| **Trades** | Every trade on the exchange | `GET /historical/trades` | `GET /markets/trades` | `trade_id` | Per-trade | 228M+ rows, growing |
| **Hourly Candles** | OHLC + volume + OI per market per hour | `GET /historical/markets/{t}/candlesticks` (60) | `GET /series/{s}/markets/{t}/candlesticks` (60) | `(ticker, period_end, resolution)` | Hourly | ~3M rows (settled tickers) |
| **Market Metadata** | Full market object at rest | `GET /historical/markets` | `GET /markets` with `with_nested_markets` via events | `ticker` | Snapshot (at discovery + at settlement) | ~5M markets total |
| **Event Metadata** | Event + nested market list | Always via `/events` endpoint | Same | `event_ticker` | Snapshot | ~80K events |
| **Settlement Outcomes** | Result (yes/no), settlement time, value | Via market object (`result`, `settlement_ts`) | Same | `ticker` | One-time per market | ~4.5M settled |
| **Portfolio Fills** | Our own trade fills | `GET /historical/fills` | `GET /portfolio/fills` | `fill_id` | Per-fill | ~100 rows (growing with trading) |

### 6.2 Secondary Datasets (Useful, Lower Priority)

| Dataset | Description | Historical Path | Live Path | Natural Key | Resolution | Est. Scale |
|---------|-------------|-----------------|-----------|-------------|------------|------------|
| **Daily Candles** | OHLC + volume + OI per market per day | Historical candle endpoint (1440) | Live candle endpoint (1440) | `(ticker, period_end, resolution)` | Daily | ~500K rows |
| **Orderbook Snapshots** | Depth-of-book at collection time | None (no historical orderbook) | `GET /markets/{t}/orderbook` | `(ticker, timestamp)` | Per-collection-cycle (5 min) | 7.6M+ (growing) |
| **Minute Candles** | OHLC + volume + OI per market per minute | Historical candle endpoint (1) | Live candle endpoint (1) | `(ticker, period_end, resolution)` | Minute | 62M+ (very large) |
| **Portfolio Orders** | Our order history | `GET /historical/orders` | `GET /portfolio/orders` | `order_id` | Per-order | Small |
| **Portfolio Settlements** | Our settlement payouts | None | `GET /portfolio/settlements` | `(ticker, settlement_ts)` | Per-settlement | Small |

### 6.3 Datasets We Won't Collect

| Dataset | Why Not |
|---------|---------|
| **Event-level candles** | Aggregated across markets in an event. Less useful than per-market candles. |
| **Forecast percentile history** | Derived metric, not raw data. Can be computed from candles. |
| **Live sports data** | Play-by-play from Sportradar. Interesting but separate domain. |
| **RFQ/Quotes** | Block trading system. Not relevant to our strategies. |
| **Incentive programs** | Informational only. |
| **Multivariate events** | Combo events. Monitor but don't actively collect. |

### 6.4 Resolution Decisions

**Candle resolution — hourly is the default, minute and daily are secondary.**

Rationale:
- Minute candles are massive (62M for historical alone) and price fields are usually NULL (no trade within 1 min on most markets). bid/ask OHLC is populated but at high storage cost.
- Hourly candles balance coverage and cost. Price fields are usually populated. ~50x smaller than minute.
- Daily candles are useful for long-term analysis but too coarse for fill modeling.
- The existing `kalshi_hourly_candles` table (2.8M rows) was our most-used candle data for calibration.

**Decision:** Store all three resolutions in one table with a `resolution` column (as `kalshi_candles` already does). Backfill hourly first, then daily. Minute candles only for specific analysis — not backfilled by default.

**Orderbook snapshots — live-only, no historical API.**

The orderbook endpoint has no historical equivalent. Snapshots only exist from when we collect them. The current 1-month gap (2026-02-17 to 2026-03-21) is permanent for that window. Going forward, continuous collection is critical.

**Trades — complete historical + live.**

Trade data is the most important dataset. Both historical and live paths exist. We have 228M rows with complete coverage from 2021-06 to present. The splice point (historical cutoff) must be tracked.

## 7. Splice Points and Coverage Gaps

### 7.1 Current State

| Dataset | Historical Range | Live Range | Gap |
|---------|-----------------|------------|-----|
| Trades | 2021-06 to cutoff (~2026-01) | cutoff to now | Covered (splice needed at cutoff) |
| Candles (1-min) | Historical: 2.6M tickers | None (no live 1-min yet) | Post-cutoff markets have no minute candles |
| Candles (hourly, legacy) | — | 1.4M tickers | No historical hourly in new format |
| Snapshots | — | 2026-02-17 to 2026-03-21 (legacy), 2026-04-06 onward (live) | 2026-03-21 to 2026-04-06 permanently lost |
| Events/Markets | — | Ongoing (metadata, overwritten) | Legacy data preserved with origin='legacy'; live refresh every 30 min |
| Settled events/markets | Ongoing (all time) | Same | Full coverage. Weekly refresh via data system (Sun 06:00 UTC) |
| Portfolio fills | All time | All time | 83 fills total |

### 7.2 Known Irrecoverable Gaps

1. **Snapshots before 2026-02-17.** No historical orderbook API. Gone forever.
2. **Snapshots 2026-03-21 to 2026-04-06.** ~16 days of snapshots permanently lost. Root cause: the old `kalshi_collector.py` filtered on integer `volume`/`open_interest` fields which the API zeroed in nested event responses (only `_fp` string variants have real values). Fixed 2026-04-06 by new `collectors/kalshi/snapshots.py` using `open_interest_fp`.
3. **Minute candles for post-cutoff markets.** Historical candle API only covers pre-cutoff markets. Live candle API works but we haven't been collecting minute candles live.

### 7.3 Recoverable Gaps

1. **Hourly candles for historical tickers.** Can re-download via historical endpoint at resolution=60.
2. **Daily candles for historical tickers.** Can re-download via historical endpoint at resolution=1440.
3. **Live trades since cutoff.** `GET /markets/trades` with `min_ts` after cutoff. Already collected but worth verifying completeness.
4. **Live candles for post-cutoff settled markets.** Batch candle endpoint works. `historical_downloader.py` `download_live_candles` already does this.

## 8. Schema and Conventions Observations

These inform Phase 2 schema design decisions.

### 8.1 Natural Keys

| Entity | Natural Key | Notes |
|--------|-------------|-------|
| Trade | `trade_id` (string UUID) | Globally unique |
| Market | `ticker` (string) | Globally unique, human-readable |
| Event | `event_ticker` (string) | Globally unique |
| Series | `series_ticker` (string) | Globally unique, prefix of event ticker |
| Candle | `(ticker, period_end, resolution)` | Composite |
| Snapshot | `(ticker, timestamp)` | Composite; timestamp is our collection time |
| Fill | `fill_id` (string UUID) | Globally unique |
| Order | `order_id` (string UUID) | Globally unique |

### 8.2 Timestamp Formats

The API returns timestamps in RFC3339 / ISO-8601 format (e.g., `"2026-04-05T12:00:00Z"`). Candle endpoints use Unix timestamps for parameters (`start_ts`, `end_ts`) and `end_period_ts` in responses.

**Convention for new tables:** All timestamps should be `timestamptz`. No text timestamps.

### 8.3 Price Representation

The API now uses fixed-point dollar strings (e.g., `"0.9300"` for 93 cents). Older responses used integer cents. Internal consistency matters more than matching the API format.

**Decision to make in Phase 2:** Store prices as integer cents (simpler math, no floating-point issues) or as `numeric(6,4)` dollars (matches API format). The existing codebase is split: `kalshi_candlesticks` uses integer cents, `kalshi_hourly_candles` uses `numeric(6,4)`, `kalshi_candles` uses integer cents.

### 8.4 Origin Tracking

Per R9, every row needs `recorded_at` and `superseded_at`. Per the requirements doc, "origin" (not "source") tracks provenance.

For Kalshi data, origin values would be:
- `historical` — from `/historical/*` endpoints
- `live` — from live endpoints or live collector
- `snapshot` — from our periodic orderbook collection

The existing `kalshi_trades` and `kalshi_candles` tables already have a `source` column with values `historical` and `live`. Rename to `origin` for consistency with the new system.

## 9. What This Discovery Tells Us

### 9.1 Key Design Inputs for Phase 2

1. **Trades are the anchor dataset.** Largest, most complete, most consumed. Natural first pilot for Phase 2.
2. **Candle consolidation is the hardest problem.** Three legacy tables, three resolutions, two unit systems, incomplete field population. Must be solved but not first.
3. **Orderbook snapshots are live-only and fragile.** No historical API, so collection continuity is critical. Must have alerting.
4. **The market object is richer than our schema captures.** We store ~12 fields; the API provides ~40. Worth capturing more metadata (strike structure, timing fields, rules) for analysis.
5. **The splice point (historical cutoff) is a moving target.** Must be tracked as metadata in the registry, not hardcoded.
6. **FinFeed data is redundant.** Historical candle API provides the same (better) data. FinFeed can be deprecated in Phase 4.

### 9.2 Risks and Open Questions

1. **Rate limits constrain backfill speed.** At 30 QPS, downloading hourly candles for 2.6M tickers takes ~24 hours per pass. Historical endpoint may have tighter limits.
2. **Snapshot collector status.** The snapshot data ends 2026-03-21. Is the collector still running? If not, every day of gap is irrecoverable.
3. **Live trade completeness.** 128M live trades seems high relative to 100M historical. Need to verify there isn't double-counting or that the live window hasn't been re-downloaded.
4. **Minute candle value proposition.** 62M rows, mostly NULL price fields. Is the bid/ask OHLC at 1-min resolution worth the storage? Probably yes for fill modeling, but should be opt-in per analysis.
5. **`kalshi_trades` table needs ANALYZE.** Planner thinks 17.8M rows; actual is 228M. Causes terrible query plans.
