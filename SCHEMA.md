# Prediction Markets Database Schema

Schema: `prediction_markets` in PostgreSQL 17 (`claude_hub` database).

24 tables. Last updated: 2026-04-13.


---

## Kalshi Prediction Market Data

### kalshi_events

**Purpose:** Metadata for Kalshi events (groups of related markets). The canonical "what events are active" source of truth. Part of the data system framework.

**Owner:** `collectors/kalshi/discovery.py`

**Update frequency:** Every 30 minutes (via `kalshi-discovery.timer`)

**Primary key:** `event_ticker`

**Registry:** `kalshi_events`

| Column | Type | Meaning | Notes |
|--------|------|---------|-------|
| event_ticker | TEXT NOT NULL | Kalshi event identifier | e.g., `KXINX-26MAR14` |
| title | TEXT | Human-readable event title | Truncated to 200 chars |
| category | TEXT | Kalshi platform category | `Politics`, `Elections`, `Economics`, `Crypto`, `Financials`, etc. |
| series_ticker | TEXT | Parent series identifier | Primary join key to `market_classifications` |
| sub_title | TEXT | Event subtitle | |
| strike_period | TEXT | Time period for strike resolution | Sparse; mostly NULL |
| mutually_exclusive | BOOLEAN | Whether event markets form a partition | Used in market_structure derivation |
| market_structure | TEXT | How contracts are packaged | `standalone`, `monotone_threshold`, `exhaustive_partition`. Derived from strike_type patterns |
| recorded_at | TIMESTAMPTZ NOT NULL | When last refreshed | Freshness column for health checks. Events not seen in API have stale recorded_at |
| origin | TEXT NOT NULL | Data provenance | `'live'` (from discovery collector) or `'legacy'` (pre-data-system) |
| superseded_at | TIMESTAMPTZ | When superseded | Always NULL (metadata table, no versioning) |

**Notes:** Only open events from the API. Events that close/settle stop appearing and their `recorded_at` becomes stale. Legacy data (20K rows from pre-2026-04-06) preserved with `origin='legacy'`. Settled events go to `kalshi_settled_events`.


### kalshi_markets

**Purpose:** Metadata for Kalshi markets (individual contracts within events). The canonical "what markets are active" source — other collectors (snapshots, candles) should query this table for discovery instead of inline API calls. Part of the data system framework.

**Owner:** `collectors/kalshi/discovery.py`

**Update frequency:** Every 30 minutes (via `kalshi-discovery.timer`, same pass as events)

**Primary key:** `ticker`

**Registry:** `kalshi_markets`

| Column | Type | Meaning | Notes |
|--------|------|---------|-------|
| ticker | TEXT NOT NULL | Unique market identifier | e.g., `KXINX-26MAR14H1600-B5750` |
| event_ticker | TEXT | Parent event | |
| title | TEXT | Market title | Truncated to 200 chars |
| status | TEXT | Market lifecycle status | `active`, `closed`, `settled` |
| close_time | TIMESTAMPTZ | When market closes | Migrated from TEXT 2026-04-06 |
| volume | INTEGER | Cumulative contracts traded | From `volume_fp` API field (integer field zeroed in nested responses) |
| open_interest | INTEGER | Current open contracts | From `open_interest_fp` API field |
| rules_primary | TEXT | Primary settlement rule text | Only available for active markets |
| rules_secondary | TEXT | Secondary settlement rule text | |
| strike_type | TEXT | Direction of strike comparison | `greater`, `less`, `greater_or_equal`, `less_or_equal`, `between`. NULL for simple binaries |
| floor_strike | DOUBLE PRECISION | Strike value | |
| market_type | TEXT | Kalshi market type | `binary` |
| expected_expiration_time | TIMESTAMPTZ | Expected resolution time | Migrated from TEXT 2026-04-06 |
| open_time | TIMESTAMPTZ | When market opened for trading | Added 2026-04-06 |
| created_time | TIMESTAMPTZ | When market was created | Added 2026-04-06 |
| can_close_early | BOOLEAN | Whether early close is possible | Added 2026-04-06 |
| result | TEXT | Settlement result | Empty while open; filled on settle |
| yes_sub_title | TEXT | YES side label | Added 2026-04-06 |
| no_sub_title | TEXT | NO side label | Added 2026-04-06 |
| recorded_at | TIMESTAMPTZ NOT NULL | When last refreshed | Freshness column. Markets not seen in API have stale recorded_at |
| origin | TEXT NOT NULL | Data provenance | `'live'` or `'legacy'` |
| superseded_at | TIMESTAMPTZ | When superseded | Always NULL |

**Notes:** Legacy data (324K rows, all `status='active'`, pre-2026-04-06) preserved with `origin='legacy'`. FK to kalshi_events dropped during data system migration (no FKs between data tables). After settlement, rules_primary, strike_type, etc. are lost (not available from settled endpoint) — known limitation for historical analysis.


### kalshi_snapshots

**Purpose:** Orderbook snapshots for active Kalshi markets. Live-only — no historical API, so missed data is permanently lost. Part of the data system framework.

**Owner:** `collectors/kalshi/snapshots.py`

**Update frequency:** Continuous (via `kalshi-snapshots.service`). Each cycle snapshots all markets with open interest; cycle time depends on market count and QPS.

**Primary key:** `(ticker, timestamp)`

**Registry:** `kalshi_snapshots`

| Column | Type | Meaning | Valid values | Notes |
|--------|------|---------|--------------|-------|
| ticker | TEXT NOT NULL | Market ticker | | |
| timestamp | TIMESTAMPTZ NOT NULL | When orderbook was observed | | Freshness column for health checks |
| yes_bid | INTEGER | Best yes bid price (cents) | 0-99 | From orderbook yes levels[0] |
| yes_ask | INTEGER | Best yes ask price (cents) | 1-100 | Derived: 100 - best no bid |
| yes_bid_depth | INTEGER | Total quantity on yes bid side | Non-negative | Sum of all yes orderbook levels |
| yes_ask_depth | INTEGER | Total quantity on yes ask side | Non-negative | Sum of all no orderbook levels |
| volume | INTEGER | Cumulative volume (from discovery) | Non-negative | Refreshed per discovery cycle, not per snapshot |
| open_interest | INTEGER | Open interest (from discovery) | Non-negative | Same: refreshed per discovery cycle |
| origin | TEXT NOT NULL | Data provenance | 'live', 'legacy' | Legacy = pre-data-system (Feb-Mar 2026) |
| recorded_at | TIMESTAMPTZ NOT NULL | When row was written to DB | | DEFAULT now() |
| superseded_at | TIMESTAMPTZ | Reserved for future splice | | Always NULL (live-only dataset) |
| yes_levels | JSONB | Full YES-side orderbook depth | `[[price_cents, qty], ...]` | NULL for legacy rows and after retention cleanup |
| no_levels | JSONB | Full NO-side orderbook depth | `[[price_cents, qty], ...]` | NULL for legacy rows and after retention cleanup |

**Notes:**
- Legacy data (7.56M rows, Feb 17 – Mar 21, 2026) preserved with `origin = 'legacy'`. Columns `no_bid`, `no_ask`, `spread_bps`, `id` were removed during migration (all derivable from yes_bid/yes_ask).
- API returns `orderbook_fp` with `yes_dollars`/`no_dollars` (dollar strings). The collector handles both old (integer cents) and new (dollar string) formats.
- The `unified_prices` view depends on this table for calibration price lookups.
- Depth columns (`yes_levels`, `no_levels`) collected since 2026-04-12 via batch orderbook API (`/markets/orderbooks`, 100 tickers/call). NULLed after 60 days by retention cleanup to bound storage. Collection frequency: every 15 minutes.


### kalshi_candles

**Purpose:** Multi-resolution candlestick data (bid/ask/price OHLC, volume, OI) for Kalshi markets. Stores minute, hourly, and daily candles in one table with a `resolution` column. Part of the data system framework.

**Owner:** `collectors/kalshi/candles.py`

**Update frequency:** Hourly (via `kalshi-candles-sync.timer` at :30 UTC). Minute candles are backfill-only.

**Primary key:** `(ticker, period_end, resolution)`

**Registry:** Three entries — `kalshi_candles_minute`, `kalshi_candles_hourly`, `kalshi_candles_daily`

| Column | Type | Meaning | Valid values | Notes |
|--------|------|---------|--------------|-------|
| ticker | TEXT NOT NULL | Market ticker | | |
| period_end | TIMESTAMPTZ NOT NULL | End of candlestick period | | Freshness column for health checks |
| resolution | SMALLINT NOT NULL | Period interval in minutes | 1, 60, 1440 | Part of PK |
| origin | TEXT NOT NULL | Data provenance | 'historical', 'live' | Historical replaces live via splice |
| yes_bid_open | INTEGER | Yes bid at period open (cents) | 0-99 | |
| yes_bid_close | INTEGER | Yes bid at period close (cents) | 0-99 | |
| yes_bid_high | INTEGER | Highest yes bid in period (cents) | 0-99 | |
| yes_bid_low | INTEGER | Lowest yes bid in period (cents) | 0-99 | |
| yes_ask_open | INTEGER | Yes ask at period open (cents) | 1-100 | |
| yes_ask_close | INTEGER | Yes ask at period close (cents) | 1-100 | |
| yes_ask_high | INTEGER | Highest yes ask in period (cents) | 1-100 | |
| yes_ask_low | INTEGER | Lowest yes ask in period (cents) | 1-100 | |
| price_open | INTEGER | Trade price at period open (cents) | 0-100 | Often NULL at minute resolution |
| price_close | INTEGER | Trade price at period close (cents) | 0-100 | Often NULL at minute resolution |
| price_high | INTEGER | Highest trade price in period (cents) | 0-100 | |
| price_low | INTEGER | Lowest trade price in period (cents) | 0-100 | |
| volume | INTEGER | Contracts traded in period | Non-negative | 173 rows with negative OI from source |
| open_interest | INTEGER | OI at period end | Non-negative | |
| recorded_at | TIMESTAMPTZ NOT NULL | When row was inserted | | DEFAULT now() |
| superseded_at | TIMESTAMPTZ | When row was replaced | | Always NULL (no multi-version) |

**Notes:** 62M+ rows (mostly minute resolution from historical backfill). Autovacuum tuned (scale_factor=0.01). Indexes: PK on (ticker, period_end, resolution), idx on (period_end, resolution). Views: `candles` (deduplicates by origin precedence), `candles_with_legacy` (unions with old `kalshi_hourly_candles` table).

### kalshi_candlesticks (legacy)

**Purpose:** Original minute-level candlestick table. Superseded by `kalshi_candles` but retained for reference.

**Owner:** Legacy (scripts deleted). No active collector — table is read-only.

**Notes:** 377K rows. Original 8 columns (bid/ask OHLC only), 9 columns added post-migration (price OHLC, etc.). Rows from the original backfill have NULL for the newer columns. No `origin`/`recorded_at` columns. Not part of the data system. Still queried by trading/research code; data also accessible via `kalshi_candles` + `candles_with_legacy` view.


### kalshi_settled_events

**Purpose:** Metadata for settled (resolved) Kalshi events.

**Owner:** `collectors/kalshi/settled.py`

**Registry:** `kalshi_settled_events`

**Update frequency:** Weekly (Sunday 06:00 UTC via kalshi-settled-sync.timer)

**Primary key:** `event_ticker`

| Column | Type | Meaning | Valid values | Notes |
|--------|------|---------|--------------|-------|
| event_ticker | TEXT | Kalshi event identifier | e.g., `INX-22APR28`, `KXINX-26MAR14` | Mix of old-format (no KX prefix) and new-format tickers |
| title | TEXT | Event title | Free text | Truncated to 500 chars |
| category | TEXT | Kalshi platform category | `Financials`, `Economics`, `Elections`, `Politics`, etc. | From events API. Legacy rows may have NULL (backfilled from classifications by weekly_pipeline) |
| settled_at | TIMESTAMPTZ | When event settled | | Derived as max(close_time) of child markets |
| num_markets | INTEGER | Number of markets in event | Positive integer | |
| market_structure | TEXT | How contracts are packaged | `standalone`, `monotone_threshold`, `exhaustive_partition` | Derived from strike_types + mutually_exclusive. Legacy rows have 47% NULL coverage |
| series_ticker | TEXT | Series for classification joins | e.g., `KXINX`, `KXBTC` | Added 2026-04-06. NULL for legacy rows |
| mutually_exclusive | BOOLEAN | Whether markets in event are mutually exclusive | | Added 2026-04-06. NULL for legacy rows |
| recorded_at | TIMESTAMPTZ | When this row was last refreshed | | `now()` default |
| origin | TEXT | Data provenance | `legacy`, `live` | Legacy = pre-data-system rows |
| superseded_at | TIMESTAMPTZ | Reserved for future use | | Always NULL |

**Notes:** Append-only — new settlements accumulate. Legacy data (62K rows from pre-data-system era) preserved with `origin='legacy'`. Live collector re-fetches all settled events and upgrades matching rows to `origin='live'`.


### kalshi_settled_markets

**Purpose:** Resolution outcomes for settled Kalshi markets.

**Owner:** `collectors/kalshi/settled.py`

**Registry:** `kalshi_settled_markets`

**Update frequency:** Weekly (Sunday 06:00 UTC via kalshi-settled-sync.timer)

**Primary key:** `ticker`

| Column | Type | Meaning | Valid values | Notes |
|--------|------|---------|--------------|-------|
| ticker | TEXT | Market ticker | | |
| event_ticker | TEXT | Parent event ticker | | No FK (dropped per spec) |
| title | TEXT | Market title | Free text | Truncated to 500 chars |
| result | TEXT | Settlement outcome | `yes`, `no`, `scalar`, NULL | Lowercased on insert |
| volume | INTEGER | Cumulative contracts traded | Non-negative | Often 0 — Kalshi zeroes volume after settlement. Historical volume via kalshi_candles |
| settled_at | TIMESTAMPTZ | When market settled | | Uses close_time from API |
| close_time | TIMESTAMPTZ | Market close time | | Added 2026-04-06. NULL for legacy rows |
| strike_type | TEXT | Strike structure | `between`, `greater`, `less`, etc. | Added 2026-04-06. NULL for legacy rows |
| floor_strike | DOUBLE PRECISION | Strike price value | | Added 2026-04-06. NULL for legacy rows |
| recorded_at | TIMESTAMPTZ | When this row was last refreshed | | `now()` default |
| origin | TEXT | Data provenance | `legacy`, `live` | Legacy = pre-data-system rows |
| superseded_at | TIMESTAMPTZ | Reserved for future use | | Always NULL |


---

## Kalshi Portfolio Tracking

### kalshi_portfolio_fills

**Purpose:** Records of our actual trade fills from Kalshi portfolio API. Synced each trading cycle.

**Owner:** `trading/trader.py` (`_sync_portfolio_fills`)

**Primary key:** `fill_id`

| Column | Type | Meaning | Notes |
|--------|------|---------|-------|
| fill_id | TEXT NOT NULL | Kalshi fill identifier | PK |
| order_id | TEXT NOT NULL | Parent order ID | FK to order_log |
| trade_id | TEXT | Kalshi trade ID | |
| ticker | TEXT NOT NULL | Market ticker | |
| side | TEXT NOT NULL | `yes` or `no` | |
| action | TEXT NOT NULL | `buy` or `sell` | |
| count | NUMERIC(12,2) NOT NULL | Contracts filled | |
| yes_price | NUMERIC(8,6) NOT NULL | YES price in dollars | |
| no_price | NUMERIC(8,6) NOT NULL | NO price in dollars | |
| fee_cost | NUMERIC(10,4) NOT NULL | Fee in dollars | |
| is_taker | BOOLEAN NOT NULL | Whether we were the taker | |
| created_time | TIMESTAMPTZ NOT NULL | Fill timestamp | |

### kalshi_order_log

**Purpose:** Every order we place, with market state at placement time. For fill model training — captures both fills and non-fills (cancels, expirations). Added 2026-04-13.

**Owner:** `trading/trader.py` (`_log_order_to_db`, `_sync_order_statuses`)

**Primary key:** `order_id`

| Column | Type | Meaning | Notes |
|--------|------|---------|-------|
| order_id | TEXT NOT NULL | Kalshi order ID | PK |
| client_order_id | TEXT | Our tracking ID | |
| ticker | TEXT NOT NULL | Market ticker | |
| event_ticker | TEXT NOT NULL | Parent event | |
| side | TEXT NOT NULL | `yes` or `no` | |
| action | TEXT NOT NULL | Default `buy` | |
| price_cents | INTEGER NOT NULL | Limit price in cents | |
| quantity | INTEGER NOT NULL | Contracts requested | |
| yes_bid | INTEGER | Best YES bid at placement | Market state snapshot |
| yes_ask | INTEGER | Best YES ask at placement | Market state snapshot |
| spread | INTEGER | Ask - bid at placement | |
| volume | INTEGER | Market volume at placement | |
| open_interest | INTEGER | OI at placement | |
| placed_at | TIMESTAMPTZ NOT NULL | Order placement time | |
| filled_at | TIMESTAMPTZ | First fill timestamp | From portfolio_fills |
| cancelled_at | TIMESTAMPTZ | Cancellation time | |
| expired_at | TIMESTAMPTZ | Expiration time | |
| settled_at | TIMESTAMPTZ | Market settlement time | |
| status | TEXT NOT NULL | `resting`, `filled`, `partial`, `cancelled`, `expired` | Updated by sync |
| filled_quantity | INTEGER NOT NULL | Contracts filled so far | Default 0 |
| edge_estimate | REAL | P(fill) at placement | |
| ev_estimate | REAL | EV per contract at placement | |
| generating_process | TEXT | Classification | |
| topic | TEXT | Classification | |
| created_at | TIMESTAMPTZ NOT NULL | Row creation time | Default now() |

### kalshi_queue_positions

**Purpose:** Periodic queue depth observations for our resting orders. Polled each sync cycle via `GET /portfolio/orders/queue_positions`. For fill model calibration of Q_ahead. Added 2026-04-13.

**Owner:** `trading/trader.py` (`_poll_queue_positions`)

**Primary key:** `(order_id, observed_at)`

| Column | Type | Meaning | Notes |
|--------|------|---------|-------|
| order_id | TEXT NOT NULL | Kalshi order ID | FK to order_log |
| observed_at | TIMESTAMPTZ NOT NULL | Observation time | |
| queue_position | INTEGER NOT NULL | Contracts ahead of us | From Kalshi API |


---

## Polymarket Prediction Market Data

### polymarket_markets

**Purpose:** Market metadata for Polymarket binary outcome markets.

**Owner:** `polymarket_collector.py` (discover_markets). Resolution columns (result, resolved_at, category) were populated by deleted `settled_downloader.py` — no active writer.

**Update frequency:** Continuous (5-min collector cycle) for active markets. Resolution data is static (no active collector).

**Primary key:** `market_id`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| market_id | TEXT | Polymarket condition ID | UUID-like string | Polymarket API | |
| token_id | TEXT | CLOB token ID for orderbook queries | Numeric string | Polymarket API | First token from clobTokenIds array |
| question | TEXT | Market question text | Free text | Polymarket API | Truncated to 500 chars |
| outcome | TEXT | Which outcome this token represents | `Yes`, or outcome name from API | Polymarket API | |
| end_date | TEXT | Market expiration date | ISO 8601 timestamp | Polymarket API | TEXT not TIMESTAMPTZ |
| volume | DOUBLE PRECISION | Cumulative USD volume | Non-negative | Polymarket API | Updated to max(existing, new) on conflict |
| liquidity | DOUBLE PRECISION | Current liquidity in USD | Non-negative | Polymarket API | |
| added_at | TIMESTAMPTZ | When row was first inserted | Defaults to NOW() | Auto | |
| is_active | BOOLEAN | Whether market is still tradeable | true/false | polymarket_collector | Set to FALSE when ended or resolved |
| result | TEXT | Settlement outcome | Winning outcome name, or NULL | Legacy (no active writer) | Added post-migration. NULL for active/unresolved markets |
| resolved_at | TEXT | When market was resolved | ISO 8601 timestamp | Legacy (no active writer) | Added post-migration. TEXT not TIMESTAMPTZ. Uses closedTime from API |
| category | TEXT | Market category | Free text from Polymarket API | Legacy (no active writer) | Added post-migration. Only populated for resolved markets |

**Notes:** Sports markets are excluded during discovery (keyword filter). The collector only inserts active markets. Resolution data (result, resolved_at, category) is no longer actively populated.


### polymarket_snapshots

**Purpose:** 5-minute orderbook snapshots for active Polymarket markets.

**Owner:** `polymarket_collector.py` (collect_snapshot)

**Update frequency:** Continuous, every 5 minutes

**Primary key:** `id` (BIGSERIAL)

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| id | BIGSERIAL | Auto-increment row ID | | Auto | |
| market_id | TEXT | FK to polymarket_markets | | polymarket_collector | |
| timestamp | TIMESTAMPTZ | When snapshot was taken | | polymarket_collector | |
| best_bid | DOUBLE PRECISION | Best bid price | 0.0-1.0 | polymarket_collector | **Data before Mar 17, 2026 is wrong.** Collector bug: used `bids[0]` (worst bid) instead of `bids[-1]` (best bid). Fixed Mar 17. |
| best_ask | DOUBLE PRECISION | Best ask price | 0.0-1.0 | polymarket_collector | **Data before Mar 17, 2026 is wrong.** Same bug: used `asks[0]` instead of `asks[-1]`. |
| mid_price | DOUBLE PRECISION | Midpoint of bid and ask | 0.0-1.0 | polymarket_collector | Derived from best_bid and best_ask. Also wrong before Mar 17. |
| spread_bps | DOUBLE PRECISION | Bid-ask spread in basis points | Non-negative | polymarket_collector | Derived from best_bid and best_ask |
| bid_depth | DOUBLE PRECISION | Total quantity on bid side | Non-negative | polymarket_collector | Sum of all bid levels. This was always correct. |
| ask_depth | DOUBLE PRECISION | Total quantity on ask side | Non-negative | polymarket_collector | Sum of all ask levels. This was always correct. |
| volume_24h | DOUBLE PRECISION | 24-hour rolling volume from discovery cache | Non-negative | polymarket_collector | Actually total volume from market metadata, not true 24h. Refreshed every 30 min. |

**Notes:** Data begins Feb 15, 2026. ~9.3M+ rows. Indexed on `(market_id, timestamp)`.


---

## PredictIt Prediction Market Data

### predictit_markets

**Purpose:** Market metadata for PredictIt markets.

**Owner:** `predictit_collector.py`

**Update frequency:** Continuous (5-min collector cycle, upserted each cycle)

**Primary key:** `market_id`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| market_id | INTEGER | PredictIt market ID | Positive integer | PredictIt API | |
| name | TEXT | Full market name | Free text | PredictIt API | |
| short_name | TEXT | Abbreviated market name | Free text | PredictIt API | |
| url | TEXT | URL to market page | URL string | PredictIt API | |
| added_at | TIMESTAMPTZ | When row was first inserted | Defaults to NOW() | Auto | |


### predictit_contracts

**Purpose:** Individual contracts within PredictIt markets.

**Owner:** `predictit_collector.py`

**Update frequency:** Continuous (upserted each snapshot cycle)

**Primary key:** `contract_id`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| contract_id | INTEGER | PredictIt contract ID | Positive integer | PredictIt API | |
| market_id | INTEGER | FK to predictit_markets | | PredictIt API | |
| name | TEXT | Contract name | Free text | PredictIt API | |
| short_name | TEXT | Abbreviated contract name | Free text | PredictIt API | |


### predictit_snapshots

**Purpose:** Price snapshots for PredictIt contracts.

**Owner:** `predictit_collector.py` (collect_snapshot)

**Update frequency:** Continuous, every 5 minutes

**Primary key:** `id` (BIGSERIAL)

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| id | BIGSERIAL | Auto-increment row ID | | Auto | |
| contract_id | INTEGER | FK to predictit_contracts | | predictit_collector | |
| timestamp | TIMESTAMPTZ | When snapshot was taken | | predictit_collector | Stored as `datetime.now().isoformat()` |
| last_trade_price | DOUBLE PRECISION | Last trade price | 0.0-1.0 (PredictIt scale) | PredictIt API | |
| best_buy_yes | DOUBLE PRECISION | Cost to buy a Yes share | 0.0-1.0 | PredictIt API | What you pay |
| best_sell_yes | DOUBLE PRECISION | Cost to sell a Yes share | 0.0-1.0 | PredictIt API | What you receive |
| best_buy_no | DOUBLE PRECISION | Cost to buy a No share | 0.0-1.0 | PredictIt API | |
| best_sell_no | DOUBLE PRECISION | Cost to sell a No share | 0.0-1.0 | PredictIt API | |
| last_close_price | DOUBLE PRECISION | Previous day close price | 0.0-1.0 | PredictIt API | |
| spread_bps | DOUBLE PRECISION | Bid-ask spread in basis points | Non-negative | predictit_collector | `(best_buy_yes - best_sell_yes) / mid * 10000` |


---

## Benchmark / Reference Data

### fred_series

**Purpose:** Metadata for FRED (Federal Reserve Economic Data) time series.

**Owner:** `fred_collector.py`

**Update frequency:** Daily (06:00 UTC timer)

**Primary key:** `series_id`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_id | TEXT | FRED series identifier | e.g., `SP500`, `DGS10`, `CPIAUCSL` | FRED API | 34 configured series |
| description | TEXT | Series description | Free text | FRED API | |
| frequency | TEXT | Data frequency | `Daily`, `Weekly`, `Monthly`, `Quarterly` | FRED API | |
| units | TEXT | Data units | e.g., `Percent`, `Index`, `Thousands of Persons` | FRED API | |
| last_updated | TEXT | When FRED last updated the series | ISO date or datetime | FRED API | TEXT not TIMESTAMPTZ |
| category | TEXT | Local grouping category | `equities`, `energy`, `rates`, `inflation`, `employment`, `gdp`, `fx`, `metals`, `fed`, `housing`, `other` | fred_collector config | Not from FRED API; assigned in CONFIGURED_SERIES |


### fred_observations

**Purpose:** Time series observations (date, value) for FRED series.

**Owner:** `fred_collector.py`

**Update frequency:** Daily (06:00 UTC timer, incremental)

**Primary key:** `(series_id, date)`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_id | TEXT | FK to fred_series | | FRED API | |
| date | DATE | Observation date | | FRED API | |
| value | DOUBLE PRECISION | Observation value | Numeric | FRED API | Missing values (`.` in FRED) are excluded |

**Notes:** ~203K rows. Coverage varies by series (some back to 1947). Incremental updates fetch from last known date to catch revisions.


### cboe_snapshots

**Purpose:** Daily SPX options chain snapshot metadata from CBOE delayed quotes.

**Owner:** `cboe_collector.py`

**Update frequency:** Daily (20:30 UTC timer)

**Primary key:** `id` (BIGSERIAL)

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| id | BIGSERIAL | Auto-increment row ID | | Auto | |
| symbol | TEXT | Underlying symbol | `SPX` | cboe_collector | The _SPX endpoint returns both SPX and SPXW options |
| fetched_at | TIMESTAMPTZ | When data was fetched | | cboe_collector | |
| spot_price | DOUBLE PRECISION | SPX spot price at fetch time | | CBOE data | |
| data_json | TEXT | Compressed raw JSON response | zlib-compressed, base64 | cboe_collector | Full response preserved for reprocessing |

**Notes:** Data begins Mar 15, 2026. One row per daily fetch.


### cboe_options

**Purpose:** Individual option contracts parsed from CBOE snapshot data.

**Owner:** `cboe_collector.py`

**Update frequency:** Daily (created alongside cboe_snapshots)

**Primary key:** `id` (BIGSERIAL)

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| id | BIGSERIAL | Auto-increment row ID | | Auto | |
| snapshot_id | BIGINT | FK to cboe_snapshots | | cboe_collector | |
| option_symbol | TEXT | CBOE option symbol | e.g., `SPX260320C06650000`, `SPXW260313P05500000` | CBOE data | Encodes root, expiry, type, strike |
| expiry | DATE | Option expiration date | | cboe_collector | Parsed from option_symbol |
| strike | DOUBLE PRECISION | Strike price | | cboe_collector | Parsed from option_symbol (raw value / 1000) |
| option_type | TEXT | Call or put | `C`, `P` | cboe_collector | |
| bid | DOUBLE PRECISION | Bid price | | CBOE data | |
| ask | DOUBLE PRECISION | Ask price | | CBOE data | |
| last_price | DOUBLE PRECISION | Last trade price | | CBOE data | |
| volume | INTEGER | Daily volume | | CBOE data | |
| open_interest | INTEGER | Open interest | | CBOE data | |
| iv | DOUBLE PRECISION | Implied volatility | 0.0-N.N | CBOE data | |
| delta | DOUBLE PRECISION | Option delta | -1.0 to 1.0 | CBOE data | |
| gamma | DOUBLE PRECISION | Option gamma | | CBOE data | |
| theta | DOUBLE PRECISION | Option theta | | CBOE data | |
| vega | DOUBLE PRECISION | Option vega | | CBOE data | |

**Notes:** ~26K options per snapshot. Indexed on `(expiry, strike)` and `(snapshot_id)`. Includes both SPX (monthly) and SPXW (weekly/0DTE) options.


### coingecko_series

**Purpose:** Metadata for CoinGecko cryptocurrency price series.

**Owner:** `coingecko_collector.py`

**Update frequency:** Daily (07:00 UTC timer)

**Primary key:** `series_id`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_id | TEXT | CoinGecko coin ID | `bitcoin`, `ethereum`, `solana`, `dogecoin`, `ripple`, `shiba-inu` | coingecko_collector config | 6 configured coins |
| description | TEXT | Coin description | Free text | coingecko_collector config | |
| frequency | TEXT | Data frequency | `D` (daily) | coingecko_collector | Always daily |
| units | TEXT | Price currency | `USD` | coingecko_collector | Always USD |
| last_updated | TEXT | When last fetched | ISO 8601 timestamp | coingecko_collector | TEXT not TIMESTAMPTZ |
| symbol | TEXT | Ticker symbol | `BTC`, `ETH`, `SOL`, `DOGE`, `XRP`, `SHIB` | coingecko_collector config | |
| kalshi_series | TEXT | Corresponding Kalshi series | e.g., `KXBTC/KXBTCD` | coingecko_collector config | Slash-separated if multiple |


### coingecko_observations

**Purpose:** Daily cryptocurrency price observations.

**Owner:** `coingecko_collector.py`

**Update frequency:** Daily (07:00 UTC timer, incremental)

**Primary key:** `(series_id, date)`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_id | TEXT | FK to coingecko_series | | coingecko_collector | |
| date | DATE | Observation date | | CoinGecko API | |
| value | DOUBLE PRECISION | Price in USD | | CoinGecko API | Same as price in coingecko_market_data |

**Notes:** ~2.2K rows. 1 year of history per coin.


### coingecko_market_data

**Purpose:** Extended daily cryptocurrency market data (price, market cap, volume).

**Owner:** `coingecko_collector.py`

**Update frequency:** Daily (07:00 UTC timer, alongside observations)

**Primary key:** `(series_id, date)`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_id | TEXT | FK to coingecko_series | | coingecko_collector | |
| date | DATE | Observation date | | CoinGecko API | |
| price | DOUBLE PRECISION | Price in USD | | CoinGecko API | |
| market_cap | DOUBLE PRECISION | Market capitalization in USD | | CoinGecko API | |
| total_volume | DOUBLE PRECISION | 24h trading volume in USD | | CoinGecko API | |

**Notes:** Parallel to coingecko_observations but with additional fields. Both are updated in the same insert loop.


### finfeed_ohlcv

**Purpose:** Daily OHLCV data for Kalshi markets from the FinFeed bulk data archive.

**Owner:** `migrate_to_postgres.py` (one-time migration from SQLite)

**Update frequency:** Static archive. No ongoing writer.

**Primary key:** `(exchange, market_id, date)`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| exchange | TEXT | Exchange identifier | `kalshi` | FinFeed data | |
| market_id | TEXT | Market identifier (FinFeed format) | e.g., `KXINX-26MAR12H1600-B6625_YES` | FinFeed data | Kalshi ticker + `_YES` or `_NO` suffix |
| date | DATE | Trading date | | FinFeed data | |
| time_period_start | TEXT | Period start timestamp | ISO timestamp | FinFeed data | TEXT not TIMESTAMPTZ |
| time_period_end | TEXT | Period end timestamp | ISO timestamp | FinFeed data | TEXT not TIMESTAMPTZ |
| time_open | TEXT | First trade timestamp | ISO timestamp | FinFeed data | TEXT not TIMESTAMPTZ |
| time_close | TEXT | Last trade timestamp | ISO timestamp | FinFeed data | TEXT not TIMESTAMPTZ |
| price_open | DOUBLE PRECISION | Opening price | 0.0-1.0 | FinFeed data | Dollar scale |
| price_high | DOUBLE PRECISION | High price | 0.0-1.0 | FinFeed data | |
| price_low | DOUBLE PRECISION | Low price | 0.0-1.0 | FinFeed data | |
| price_close | DOUBLE PRECISION | Closing price | 0.0-1.0 | FinFeed data | Used as fallback price source in settled_with_prices |
| volume_traded | DOUBLE PRECISION | Contracts traded | Non-negative | FinFeed data | Only source of historical volume for settled markets |
| trades_count | INTEGER | Number of trades | Non-negative | FinFeed data | |
| fetched_at | TIMESTAMPTZ | When data was originally fetched | Defaults to NOW() | FinFeed data | |

**Notes:** ~9.5M rows. Covers Sep 2025 to Mar 14, 2026. Not being updated. Price matching to settled markets: `finfeed_ohlcv.market_id = ticker || '_YES'`. No bid/ask or spread data — only trade-based OHLCV.


### finfeed_download_log

**Purpose:** Tracks which FinFeed bulk data files have been downloaded.

**Owner:** `migrate_to_postgres.py` (one-time migration from SQLite)

**Update frequency:** Static. No ongoing writer.

**Primary key:** `(exchange, date)`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| exchange | TEXT | Exchange identifier | `kalshi` | FinFeed data | |
| date | DATE | Date of the data file | | FinFeed data | |
| records_count | INTEGER | Number of OHLCV records in the file | Non-negative | FinFeed data | |
| fetched_at | TIMESTAMPTZ | When file was downloaded | Defaults to NOW() | FinFeed data | |


---

## Classification and Derived Tables

### market_classifications

**Purpose:** LLM-generated classification of Kalshi series along generating process, payoff type, and topic dimensions. Primary analytical taxonomy for the prediction markets research project.

**Owner:** `market_scout.py` (via `weekly_pipeline.py` step 1)

**Update frequency:** Weekly (new series only). Existing classifications are not re-run unless explicitly requested.

**Primary key:** `series_ticker`

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| series_ticker | TEXT | Kalshi series identifier | e.g., `KXBTC`, `KXCPI` | market_scout.py | |
| confidence | DOUBLE PRECISION | Classification confidence | 0.0-1.0. Mapped from LLM: high=0.9, medium=0.7, low=0.4 | LLM prompt (mapped by code) | |
| has_external_benchmark | BOOLEAN | Whether an external reference price exists | true/false | LLM prompt | |
| benchmark_source | TEXT | Which external source provides the benchmark | e.g., `FRED DGS10`, `CoinGecko BTC` | LLM prompt | |
| reasoning | TEXT | LLM explanation for classification | Free text | LLM prompt | May include appended taxonomy_feedback |
| classified_by | TEXT | Model name used for classification | e.g., `sonnet`, `opus` | market_scout.py code | |
| classified_at | TIMESTAMPTZ | When classification was generated | | market_scout.py code | |
| needs_review | BOOLEAN | Whether classification needs human review | true/false | LLM prompt + code (auto-set if confidence < 0.6) | |
| description | TEXT | Brief human-readable description | Free text | LLM prompt | |
| generating_process | TEXT | Stochastic mechanism driving probability evolution | `continuous_underlyer`, `scheduled_release`, `hazard_process`, `convergent_binary`, `counting_process`, `explicit_randomization`, `other` | LLM prompt | Primary analytical dimension |
| topic | TEXT | Subject matter | `financial`, `economic_data`, `politics_elections`, `government_policy`, `geopolitics`, `entertainment_sports`, `science_technology`, `weather_environment`, `other` | LLM prompt | 1,066 rows had NULL topic (being filled incrementally) |
| payoff_type | TEXT | Contract payoff structure | `terminal`, `barrier`, `extremum`, `binary_event`, `other` | LLM prompt | |

**Column ownership summary:**
- **LLM prompt (via market_scout.py):** series_ticker, generating_process, payoff_type, topic, description, confidence, has_external_benchmark, benchmark_source, reasoning, needs_review
- **Derived by market_scout.py code:** classified_by, classified_at

**Notes:** 12 columns. ~1,247 rows covering all known Kalshi series. The table was created outside the migration script (not in migrate_to_postgres.py). Columns were added incrementally as the taxonomy evolved — generating_process, topic, and payoff_type are newer than the original schema. Legacy/orphan columns (`process_category`, `market_structure`, `benchmark_series_id`, `strike_chain_depth`, `liquidity_tier`) were dropped Mar 18, 2026.


### settled_with_prices

**Purpose:** Pre-joined derived table for calibration analysis. Combines settled market outcomes with their last observed price before settlement, classification data, and event metadata.

**Owner:** `weekly_pipeline.py` (step 4: step_materialize)

**Update frequency:** Weekly (rebuilt from scratch via atomic swap)

**Primary key:** None (no explicit PK; ticker is unique in practice)

| Column | Type | Meaning | Valid values | Owner | Known issues |
|--------|------|---------|--------------|-------|--------------|
| ticker | TEXT | Settled market ticker | | From kalshi_settled_markets | |
| event_ticker | TEXT | Parent event ticker | | From kalshi_settled_markets | |
| title | TEXT | Market title | | From kalshi_settled_markets | |
| result | TEXT | Settlement outcome | `yes`, `no`, NULL | From kalshi_settled_markets | |
| settled_at | TIMESTAMPTZ | When market settled | | From kalshi_settled_markets | Migrated from TEXT 2026-04-06 |
| bid | NUMERIC | Last bid price before settlement (0-1 scale) | 0.0-1.0 | Derived from kalshi_snapshots | `yes_bid / 100.0`. NULL for finfeed rows |
| ask | NUMERIC | Last ask price before settlement (0-1 scale) | 0.0-1.0 | Derived from kalshi_snapshots | `yes_ask / 100.0`. NULL for finfeed rows |
| last_price | NUMERIC | Last observed price before settlement | 0.0-1.0 | Derived | Mid price for snapshots `(bid + ask) / 2`, close price for finfeed |
| price_observed_at | TIMESTAMP | When the price was observed | | Derived | From snapshot timestamp or finfeed date |
| price_source | TEXT | Which data source provided the price | `snapshot`, `finfeed` | Derived | Prefer most recent observation regardless of source |
| generating_process | TEXT | Stochastic mechanism classification | Same as market_classifications.generating_process | 3-level fallback join | |
| topic | TEXT | Subject matter classification | Same as market_classifications.topic | 3-level fallback join | |
| payoff_type | TEXT | Contract payoff structure | Same as market_classifications.payoff_type | 3-level fallback join | |
| has_external_benchmark | BOOLEAN | Whether external benchmark exists | true/false | 3-level fallback join | |
| benchmark_source | TEXT | External benchmark identifier | | 3-level fallback join | |
| classification_description | TEXT | Human-readable classification description | | 3-level fallback join | |
| event_category | TEXT | Kalshi platform category from settled events | `Financials`, `Economics`, etc. | From kalshi_settled_events | |
| event_title | TEXT | Event-level title | | From kalshi_settled_events | |

**Classification 3-level fallback:** For each settled market, classification is joined via:
1. `kalshi_events.series_ticker` (exact match, for events that still exist in active events)
2. `'KX' || split_part(event_ticker, '-', 1)` (add KX prefix for old-format tickers)
3. `split_part(event_ticker, '-', 1)` (direct prefix match)

**Price selection:** For each ticker, the most recent price observation from either source (snapshot or finfeed) before settlement is selected. Snapshot prices require spread <= 20 cents.

**Indexes:** `generating_process`, `topic`, `event_category`, `result`, `price_source`.


---

## Proposed Changes

### 1. ~~Move market_structure to per-event level~~ DONE (Mar 18, 2026)

Added `market_structure` column to `kalshi_events` and `kalshi_settled_events`, derived from `strike_type` per market + `mutually_exclusive` per event during discovery. Dropped the legacy per-series `market_structure` column from `market_classifications`. KXINX bracket misclassification fixed by the per-event derivation logic.

### 2. ~~Drop or populate orphan columns~~ DONE (Mar 18, 2026)

Dropped `benchmark_series_id`, `strike_chain_depth`, and `liquidity_tier` from `market_classifications`. All three were always NULL with no writer.

### 3. ~~Mark process_category as legacy; update settled_with_prices~~ DONE (Mar 18, 2026)

Dropped `process_category` from `market_classifications`. Code migrated: `weekly_pipeline.py` step 3 uses `topic` for category backfill, `settled_with_prices` uses `generating_process`/`topic`/`payoff_type`. The `process_category` column will be absent from `settled_with_prices` on next weekly pipeline rebuild.

### 4. Fix TEXT timestamp columns (partially done)

`kalshi_markets.close_time` and `kalshi_markets.expected_expiration_time` migrated to TIMESTAMPTZ (2026-04-06, data system onboarding).
`kalshi_settled_events.settled_at` and `kalshi_settled_markets.settled_at` migrated to TIMESTAMPTZ (2026-04-06, settled onboarding).

Remaining TEXT timestamp columns:

| Table | Column | Current type |
|-------|--------|-------------|
| polymarket_markets | end_date | TEXT |
| polymarket_markets | resolved_at | TEXT |
| fred_series | last_updated | TEXT |
| coingecko_series | last_updated | TEXT |
| finfeed_ohlcv | time_period_start | TEXT |
| finfeed_ohlcv | time_period_end | TEXT |
| finfeed_ohlcv | time_open | TEXT |
| finfeed_ohlcv | time_close | TEXT |

**Impact:** Migration improves type safety and enables native timestamp operations. `settled_with_prices` no longer needs `settled_at::timestamptz` casts now that settled tables use TIMESTAMPTZ.

**Priority:** Low for static/legacy tables (finfeed). Medium for polymarket_markets.

### 5. ~~Determine status of kalshi_historical_candles~~ DONE (Mar 18, 2026)

Table dropped. It was empty (0 rows) — the SQLite-to-Postgres migration never populated it. All historical candlestick data lives in `kalshi_candlesticks`.


---

## Changelog

| Date | Change |
|------|--------|
| 2026-03-18 | Schema cleanup: dropped `kalshi_historical_candles` (empty table, 23→22 tables). Added `market_structure` column to `kalshi_events` and `kalshi_settled_events`. Dropped 5 legacy/orphan columns from `market_classifications` (`process_category`, `market_structure`, `benchmark_series_id`, `strike_chain_depth`, `liquidity_tier`; 17→12 columns). Removed `process_category` from `settled_with_prices` materialization. |
