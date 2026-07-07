# FLB Calibration Methodology

## What We Measure

For a given category of market, at a given time before settlement, at a given price level: how often does the favorite win, compared to what the price implies?

**Core quantity:** For each observation (a price quote on a settled market at some time before settlement), we record:
- `yes_price`: bid/ask midpoint from the YES perspective (0-1)
- `result`: whether the market resolved YES or NO
- `hours_to_settlement`: how far before settlement the price was observed
- `series`: which series the market belongs to
- `generating_process`, `topic`: taxonomy dimensions from `market_classifications`

**Calibration gap** (computed in YES space):
```
calibration_gap = avg(yes_price) - avg(result == 'yes')
```
Positive gap = YES overpriced at that level. Negative gap = YES underpriced.

**Favorite-space edge** (for our strategy):
We buy whichever side is priced 85-97¢. To combine observations from both tails:
- High tail (YES 85-97¢): we buy YES. fav_price = yes_price, fav_wins = (result == 'yes')
- Low tail (YES 3-15¢): we buy NO at (1 - yes_price). fav_price = 1 - yes_price, fav_wins = (result == 'no')

```
edge = avg(fav_wins) - avg(fav_price)
```
Positive edge = the favorite wins more than priced = our strategy profits.

This works because every observation is simultaneously a YES observation and a NO observation (Section 1.7 of TRADING_MECHANICS.md). Computing in YES space and converting to favorite space is algebraically equivalent to computing in NO space directly.

## Data Sources

Three sources, all providing YES-side bid/ask:

| Source | Resolution | Price | Coverage |
|--------|-----------|-------|----------|
| `kalshi_hourly_candles` | 1 hour | `(yes_bid_close + yes_ask_close) / 2` | Mar 2025 – now, 1.37M tickers |
| `kalshi_candlesticks` | 1 day | `(yes_bid_close + yes_ask_close) / 2` (stored as cents, divide by 100) | Mar 2025 – now, 30K tickers |
| `kalshi_snapshots` | 5 min | `(yes_bid + yes_ask) / 2` (stored as cents, divide by 100) | Feb 17, 2026 – now, ~35 days |

All three use bid/ask midpoints. Finfeed (external, last-trade price only) is excluded — no bid/ask means no spread filter and different price semantics.

Sources are merged with per-market deduplication by source priority (`_dedup_by_source` in `calibration.py`): for each ticker, only the highest-priority source that has data for it is kept — hourly candles (0) over snapshots (1) over daily candles (2) — and that ticker's observations from lower-priority sources are dropped. Each market therefore contributes observations from exactly one source, so overlapping source coverage cannot double-count a market.

## Spread Filter

Observations with spread > 10¢ are excluded before any analysis. This removes ghost quotes where the "midpoint" is meaningless.

**Justification:** Empirical analysis of spread vs. volume (2026-03-22) showed that ≤10¢ captures 94-99% of all trading volume across every generating_process category:

| Category | Volume at ≤10¢ spread |
|----------|----------------------|
| continuous_underlyer | 94.6% |
| convergent_binary | 98.2% |
| hazard_process | 99.8% |
| scheduled_release | 96.4% |
| counting_process | 98.3% |

Going beyond 10¢ captures almost no additional real trading activity.

## Grouping Dimensions

Observations are grouped along the taxonomy defined in MARKET_TAXONOMY.md:

**Generating process** — how probability evolves:
- `continuous_underlyer`: binary option on a traded asset (BTC, S&P, gold, crypto)
- `scheduled_release`: hidden value revealed at known time (CPI, Fed, GDP)
- `hazard_process`: discrete event by deadline (resignation, visit, announcement)
- `convergent_binary`: gradual information toward binary resolution (elections, sports)
- `counting_process`: cumulative count vs threshold (executive orders, posts)
- `explicit_randomization`: lottery/draw mechanisms (rare)

**Topic** — what the market is about:
- `financial`, `economic_data`, `politics_elections`, `government_policy`, `geopolitics`, `entertainment_sports`, `science_technology`, `weather_environment`, `other`

The primary analytical grouping is `generating_process` (determines price dynamics). `topic` is a secondary grouping useful for identifying domain-specific effects (e.g., sports FLB driven by Kalshi UI bias).

Series-level results are also computed for identifying individual series to add to or remove from the trading universe.

## Bucketing

Three dimensions of buckets:

**Price (YES-space):** 5¢ buckets from 0.00 to 1.00. For favorite-space analysis, both tails (0.03-0.15 and 0.85-0.97) contribute.

**Time-to-settlement (report mode):** [0-1h, 1-3h, 3-6h, 6-12h, 12-24h, 24h+]. Fixed buckets used for human-readable reports.

**Time-to-settlement (adaptive, for trader):** Per `(generating_process, topic, price_bucket)` cell, observations are sorted by `hours_to_settlement` and equipartitioned into `k` buckets where `k = min(n_markets // 30, 20)` (minimum 1), with `n_markets` the number of distinct markets in the cell. Cells with < 50 distinct markets are excluded. Thresholds are counted in **markets**, not raw observations, because each market contributes one averaged data point per bucket (avoiding pseudo-replication), making market count the effective N: SE ≈ sqrt(p(1-p)/n_markets), so ~30 markets per bucket gives SE ≈ 5.5% at p = 0.90. Constants: `MIN_MARKETS_PER_BUCKET = 30`, `MIN_TOTAL_MARKETS = 50`, `MAX_BUCKETS = 20` (`calibration.py`, `compute_event_rates`).

Adaptive bucketing avoids the fixed-grid problem where dense cells (convergent_binary × entertainment_sports, 49K obs) waste resolution while sparse cells (hazard_process × other, 5 obs) have empty buckets.

After computing raw per-bucket event rates, a **5-bucket market-count-weighted moving average** smooths single-bucket outliers. This tames noise (e.g., an outlier in one bucket surrounded by stable neighbors) while preserving real trends (e.g., categories where the edge sign flips across horizons).

Results are stored in `prediction_markets.calibration_rates` with both raw `event_rate` and `smoothed_event_rate` columns. The trader uses `smoothed_event_rate`.

**Category:** `generating_process`, `topic`, or `generating_process × topic` depending on the analysis. Also by individual series.

Every (market, timestamp) observation falls into exactly one cell per dimension. A single market contributes to multiple time buckets (as its price evolves toward settlement) and potentially multiple price buckets (if the price moves across bucket boundaries).

## Data Quality Lessons (from prior review rounds)

These are hard-won findings from the March 2026 multi-model review process (Opus, Gemini, GPT-5.4) applied to earlier calibration work:

1. **Tournament contamination.** Events with many markets (>5) are often tournament brackets (March Madness, awards) where the contracts are structurally different from typical binary markets. At horizons >7 days, these dominate and distort FLB estimates. Filter: exclude events with >5 markets for long-horizon analysis, or report separately.

2. **Source-specific artifacts.** Finfeed daily prices have Roll-model bounce that inflates autocorrelation and distorts calibration at short horizons. This is why we exclude finfeed and use only bid/ask midpoints from candle/snapshot sources.

3. **Horizon precision matters.** Date-only timestamps (finfeed `date` field) create ~24h fuzz that contaminates narrow time buckets. All three candle/snapshot sources have proper timestamps, so this is resolved by excluding finfeed.

4. **Liquidity gates everything.** Only ~4.3% of markets pass a strict two-sided liquidity filter. The 10¢ spread filter is a pragmatic middle ground — it excludes ghost quotes while retaining enough data for statistical power. Results should be interpreted as "calibration of markets where someone is willing to post a ≤10¢ spread," not "calibration of all markets."

5. **D-1 is the solid cohort.** Observations 6-48 hours before settlement are the most reliable for calibration — close enough that prices reflect current information, far enough to avoid terminal mechanics (prices mechanically converge to 0/100 in the final minutes).

6. **Behavioral, not microstructure.** The FLB signal (and mean-reversion) survives liquidity filtering, bid/ask decomposition, and temporal sub-sampling. It is not an artifact of bid-ask bounce. The Roll-model regression goes the wrong direction (wider spreads = less mean reversion), and ask-only ACF is near zero while bid-only ACF is strongly negative.

## Relation to Prior Analyses

| Analysis | Script | What it measured | Date |
|----------|--------|-----------------|------|
| FLB calibration (this) | `research/calibration.py` | Per-series/category FLB with hourly candle data | Mar 22 |
| Adaptive edges (this) | `research/calibration.py --store` | Per (process, topic) adaptive time-bucketed edges, stored in DB | Mar 22 |

The current analysis supersedes the earlier FLB estimates by using hourly candle data (1h resolution, 1-year lookback) instead of daily finfeed or 35-day snapshots. The adaptive edge computation (`--store`) produces the `calibration_edges` table used by the trader at runtime. The methodology lessons from the earlier review rounds are incorporated.
