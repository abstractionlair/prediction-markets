# Fill Model V2: Trade-Tape-Based Fill Prediction

**Draft 6** | 2026-04-01

## Change Log

- Draft 6: Address review findings from job f0835e26. Fixes: fully specified estimate() lookup with explicit fallback walk, FlowCDF gains n_outcome field, coarsen() replaced with COARSE_MAP + pre-built keys, p_lost fallback rule (both sides must pass threshold or fall through), pending order dict includes all required fields, fill_schedule_idx cursor replaces last_period_end, fast mode specifies P(fill) = p_win×p_fill_won + (1-p_win)×p_fill_lost with trailing_vol from compute_trailing_volume().
- Draft 5: Address review findings from job 76d16a21. Fixes: estimate() lookup path pseudocode, price-averaging ambiguity (each placement×limit is one observation), replay integration (precompute-at-placement), caching path (pass empty dict to skip calibrate), order dict field names, _quantity_steps examples, use_flow_model requires expanding, preload_trades scope clarification.
- Draft 4: Address review findings from job 51eb06fd. Fixes: expanding-window replay path, tail-price boundary enforcement, calibrate() type signature (dict not list), compute_opposing_flow definition, QUANTITY_STEPS coverage of max_contracts, legacy fill_rates wrapper documentation, bisect pseudocode, fast mode sourcing, preload_trades filter, fallback threshold clarification.
- Draft 3: Address review findings from job 4f8f3227. Fixes: replay trailing-volume plumbing, replay tape-lookup performance/architecture, MarketView constructor/from_db changes, fee-units pseudocode bug, EVOpportunity dataclass update, max_qualifying_events correction, price-agnostic optimizer analysis, partial fills in replay, FlowCDF extrapolation, virtual order sampling interval, notation consistency, outcome-merged semantics, MAX_CAPITAL_PER_ORDER definition.
- Draft 2: Address review findings from job c5a3e287. Fixes: multi-hour accumulation math, 6h/24h volume mismatch, FlowDistribution data structure, fallback hierarchy, MarketView boundary, EVStrategy ranking, replay design, p_fill removal, as_of parameter.
- Draft 1: Initial spec based on trade tape exploration (206M trades, 62M 1-min candles, 83 portfolio fills)

## Problem

The current fill model uses `capture_rate=0.20` — a made-up constant that assumes we passively capture 20% of passing volume per candle. This is wrong in kind: our orders provide liquidity (100% maker), don't capture existing flow. The model is also size-independent, making it useless for capacity analysis.

Two systems use fill estimates and must be replaced:

1. **FillRateEstimator** (`fill_rate.py`): Computes P(fill|won) and P(fill|lost) for the EV strategy. Uses a binary "was price touched?" check for 1 contract. Size-independent.
2. **FillModel** (`fill_model.py`): Simulates fills in replay/backtest via `capture_rate × candle_volume` per period. Size-dependent but uncalibrated.

Both feed into EVStrategy's trade selection (via FillRateEstimator) and replay validation (via FillModel).

## Empirical Findings

Exploration against the full trade tape established:

### Fill rates degrade moderately with order size

For counting_process/entertainment_sports (NO side, 1-3h to settlement, average across 87-95¢ limits):

| Q | P(fill) |
|---|---------|
| 1 | 86.0% |
| 10 | 81.1% |
| 20 | 77.8% |
| 50 | 72.0% |
| 100 | 66.7% |

Retention at Q=20 vs Q=1 is ~90%. Strategy can scale to Q=50 with ~84% retention.

### YES/NO asymmetry is large

NO-side fill rates are 10-15 percentage points higher than YES-side in the same market and time bucket. Our strategy predominantly buys favorites (NO at 87-97¢), and aggressive YES flow (longshot bettors) is the dominant taker force.

### Adverse selection is category-dependent

| Category | P(fill|lost) / P(fill|won) at Q=5 |
|----------|-----------------------------------|
| convergent_binary/ent_sports | 1.22 |
| counting_process/ent_sports | 1.37 |
| scheduled_release/economic_data | 1.10 |

Gets worse with larger orders (1.22 → 1.30 at Q=50 for convergent_binary sports).

### Trailing volume predicts future flow

Trailing 6h volume from the trade tape is strongly predictive of next-hour flow (sports markets, 1-24h to settlement). This motivational analysis used 6h trailing windows; the model calibrates on 24h trailing volume to match the API's `volume_24h_fp` field. Bucket boundaries for the 24h feature will be derived empirically during calibration.

| Trailing 6h vol | Median next-hr flow | P(flow ≥ 50) |
|-----------------|---------------------|--------------|
| 0 (dead) | 40 | 46% |
| 50-199 | 60 | 54% |
| 200-999 | 111 | 67% |
| 1K-5K | 335 | 82% |
| 5K+ | 3,355 | 95% |

### Price sensitivity at tail prices

Within the 87-95¢ band, fill rates vary by ~2-3 percentage points (e.g., counting_process/ent_sports YES 1-3h: 68.3% at 87¢ vs 69.3% at 95¢). This is small relative to the payoff difference across this range (13¢ at 87¢ vs 5¢ at 95¢ — a 2.6x ratio). The model averages over prices for calibration; see "Price-agnostic optimizer behavior" section below.

### Features available at order time (from Kalshi API)

The `/events` endpoint with `with_nested_markets=true` returns per-market:
- `open_interest_fp` — current open interest
- `volume_24h_fp` — trailing 24h volume
- `volume_fp` — lifetime volume
- `yes_bid_dollars`, `yes_ask_dollars` — current book
- `yes_bid_size_fp`, `yes_ask_size_fp` — depth at top of book

For historical model training, trailing volume is computed from the trade tape (universally available). Snapshot-based OI is available for only 0-2% of sports markets.

## Design

### Architecture

Replace both FillRateEstimator and FillModel with a single `FlowModel` that answers:

> Given observable features at order time, what is the distribution of opposing flow that will pass at my price level before settlement?

From this distribution, all downstream quantities follow:
- P(fill|won) and P(fill|lost) for Q contracts = empirical exceedance from multi-hour flow CDFs
- EV calculation uses these directly (caller computes unconditional P(fill) from P(event))

### Data Structures

```python
@dataclass
class FlowCDF:
    """Empirical CDF of cumulative opposing flow over a time window.

    Stores P(flow >= Q) for a set of quantity thresholds.
    Supports interpolation for arbitrary Q values.
    """
    thresholds: list[int]    # sorted ascending: [1, 2, 5, 10, 20, 50, 100, 200]
    exceedances: list[float] # P(flow >= threshold[i]), monotonically decreasing
    n_observations: int      # total virtual orders in the PARENT bin (both
                             # outcomes combined); used for MIN_COMBINED check
    n_outcome: int           # virtual orders that went into THIS specific CDF;
                             # equals n_observations for merged CDFs, less for
                             # outcome-split CDFs; used for MIN_PER_OUTCOME check

    def p_fill(self, quantity: int) -> float:
        """Interpolate P(flow >= quantity) via log-linear interpolation
        between bracketing thresholds.

        Extrapolation beyond max threshold: log-linear extrapolation
        if n_observations >= 200, else return exceedance at max threshold.
        Extrapolation beyond 2x max threshold always returns 0."""

@dataclass
class FlowEstimate:
    """Fill probability estimates for a proposed order."""
    p_fill_won: float       # P(fill Q contracts | outcome matches our side)
    p_fill_lost: float      # P(fill Q contracts | outcome is against us)
    # Note: when the outcome dimension is merged due to sparse data (fallback
    # level 3), p_fill_won == p_fill_lost. This is correct behavior — it means
    # the adverse selection adjustment drops out for this bin. Callers should
    # not treat equal values as a bug.

    # Caller computes unconditional p_fill = p_win * p_fill_won + (1-p_win) * p_fill_lost
```

### FlowModel

```python
class FlowModel:
    """Predicts fill probability from trade-tape flow distributions."""

    # Time bucket coarsening map. calibrate() pre-builds CDFs at both
    # fine and coarse levels. Keys use these canonical string labels.
    TIME_BUCKETS = ['<1h', '1-3h', '3-6h', '6-12h', '12-24h', '1-3d', '3-7d']
    COARSE_MAP = {
        '<1h': '<3h', '1-3h': '<3h',
        '3-6h': '3-12h', '6-12h': '3-12h',
        '12-24h': '12h-3d', '1-3d': '12h-3d',
        '3-7d': '3-7d',
    }

    # Minimum observations for a CDF to be usable
    MIN_COMBINED = 200   # total observations (both outcomes) in parent bin
    MIN_PER_OUTCOME = 50 # per outcome after split

    def __init__(self, flow_table: dict[tuple, FlowCDF]):
        """
        flow_table: mapping from bin key → FlowCDF.

        Keys use these forms (built during calibrate()):
          (gp, topic, tb, side, vb, outcome)     — full key (Level 1)
          (gp, topic, tb, side, '*', outcome)     — vol merged (Level 2)
          (gp, topic, ctb, side, '*', outcome)    — coarse time (Level 3)
          (gp, topic, ctb, side, '*', '*')         — outcome merged (Level 4)
          (gp, topic, ctb, '*', '*', '*')          — side merged (Level 5)

        Where tb = fine time bucket, ctb = coarse time bucket,
        vb = trailing vol bucket, outcome = 'won'|'lost'|'*',
        side = 'yes'|'no'|'*'. '*' means merged/any.
        """

    def estimate(self, gp: str, topic: str, hours_to_settle: float,
                 side: str, quantity: int, limit_price_cents: int,
                 trailing_volume: int) -> FlowEstimate | None:
        """
        Predict fill probability for a proposed resting order.

        Args:
            gp, topic: market classification
            hours_to_settle: hours until settlement
            side: 'yes' or 'no' — our resting side
            quantity: number of contracts
            limit_price_cents: our limit price (85-97 range; accepted for
                interface consistency, not used in lookup — see Price-Agnostic
                Optimizer section). Out-of-range values are accepted silently
                since the strategy clamps before calling.
            trailing_volume: recent volume (from API volume_24h_fp, or
                             trailing 24h volume from tape during calibration)

        Returns:
            FlowEstimate with conditional fill probabilities,
            or None if insufficient calibration data.
        """
        tb = self._time_bucket(hours_to_settle)
        ctb = self.COARSE_MAP[tb]
        vb = self._trailing_vol_bucket(trailing_volume)

        # Walk fallback hierarchy. Each level defines:
        #   - won_key, lost_key: lookup keys for outcome-split CDFs
        #   - merged_key: lookup key for outcome-merged CDF (levels 4-5)
        #   - outcome_split: whether to try won/lost separately
        levels = [
            # Level 1: full key
            {'won': (gp, topic, tb, side, vb, 'won'),
             'lost': (gp, topic, tb, side, vb, 'lost'),
             'split': True},
            # Level 2: drop trailing vol
            {'won': (gp, topic, tb, side, '*', 'won'),
             'lost': (gp, topic, tb, side, '*', 'lost'),
             'split': True},
            # Level 3: coarse time + drop vol
            {'won': (gp, topic, ctb, side, '*', 'won'),
             'lost': (gp, topic, ctb, side, '*', 'lost'),
             'split': True},
            # Level 4: drop outcome
            {'merged': (gp, topic, ctb, side, '*', '*'),
             'split': False},
            # Level 5: drop side
            {'merged': (gp, topic, ctb, '*', '*', '*'),
             'split': False},
        ]

        for level in levels:
            if level['split']:
                cdf_won = self.flow_table.get(level['won'])
                cdf_lost = self.flow_table.get(level['lost'])
                # n_observations = parent combined count, n_outcome = this CDF's count.
                # Check: parent >= MIN_COMBINED, each outcome >= MIN_PER_OUTCOME.
                if (cdf_won and cdf_lost
                        and cdf_won.n_observations >= self.MIN_COMBINED
                        and cdf_won.n_outcome >= self.MIN_PER_OUTCOME
                        and cdf_lost.n_outcome >= self.MIN_PER_OUTCOME):
                    return FlowEstimate(
                        p_fill_won=cdf_won.p_fill(quantity),
                        p_fill_lost=cdf_lost.p_fill(quantity))
                # If won passes but lost doesn't (or vice versa), fall through
                # to next level. Do NOT use one side without the other — that
                # would create asymmetric quality between the two estimates.
            else:
                cdf = self.flow_table.get(level['merged'])
                if cdf and cdf.n_observations >= self.MIN_COMBINED:
                    p = cdf.p_fill(quantity)
                    return FlowEstimate(p_fill_won=p, p_fill_lost=p)

        return None  # terminal: insufficient data even at coarsest level


    @classmethod
    def calibrate(cls, trades_by_ticker: dict[str, list],
                  settled_markets: dict, classifications: dict,
                  as_of: datetime = None) -> 'FlowModel':
        """
        Build model from trade tape data.

        Args:
            trades_by_ticker: dict of ticker → list of
                (created_time, count, yes_price, taker_side).
                Prices are fractional dollars (0.00-1.00) as stored
                in kalshi_trades. Lists must be sorted by created_time.
            settled_markets: ticker → (settled_at, result, event_ticker)
            classifications: series_ticker → (gp, topic)
            as_of: temporal boundary — only use data from markets settled
                   before this date. If None, use all data.
        """
```

### Calibration Procedure

**Input**: Trade tape (`kalshi_trades`), settled markets with results, classifications. Temporal boundary `as_of`.

**Step 1: Compute per-ticker cumulative flow windows**

For each settled market (settled before `as_of`), simulate virtual resting orders at **hourly intervals** through the market's lifetime (one virtual order per hour per side). For each virtual order:
- Placement time: every hour from first trade to settlement (hourly sampling balances data volume against intra-ticker autocorrelation)
- Side: both YES and NO
- Track cumulative opposing flow from placement until settlement

"Opposing flow for YES buy at limit L" = sum of `count` for trades where `taker_side='no'` AND `yes_price <= L/100` (comparing fractional dollars). A NO taker is buying NO / selling YES, filling our resting YES buy.

"Opposing flow for NO buy at limit L" = sum of `count` for trades where `taker_side='yes'` AND `yes_price >= (100 - L)/100` (comparing fractional dollars). A YES taker is buying YES, and our NO buy at `L` cents corresponds to `yes_price = (100 - L)/100`. Trades at or above this threshold fill us.

Each `(placement_time, side, limit_price)` combination is a separate virtual order observation. For 5 limit prices (87, 89, 91, 93, 95¢), each placement time generates 5 observations per side. The averaging happens at the CDF level — all 5 observations land in the same bin and contribute to the same exceedance curve. This is equivalent to averaging fill probabilities across prices, not averaging raw flow counts before thresholding. (Empirical sensitivity across this range is ~2-3pp.)

**Step 2: Compute trailing volume at each placement time**

For each virtual order placement, compute trailing_volume_24h from the trade tape: total contracts traded on this ticker in the 24 hours before placement. This matches the `volume_24h_fp` field available from the Kalshi API at live decision time.

**Step 3: Bin and build CDFs**

Group virtual orders by:
- `(gp, topic)` — category
- `time_bucket` — hours from placement to settlement: <1h, 1-3h, 3-6h, 6-12h, 12-24h, 1-3d, 3-7d
- `side` — yes or no (resting side)
- `trailing_vol_bucket` — binned trailing 24h volume: dead (0), low (1-99), moderate (100-999), active (1K-10K), high (10K+). These boundaries will be validated during calibration against the actual 24h distribution.
- `outcome` — won or lost (from settlement result, relative to our resting side)

Within each bin, build a `FlowCDF`: the empirical exceedance curve P(cumulative_flow ≥ Q) at thresholds Q = 1, 2, 5, 10, 20, 50, 100, 200.

This directly computes multi-hour fill probability without any independence assumption. A virtual order placed 6 hours before settlement with 50 contracts of cumulative opposing flow records P(flow ≥ 50) = 1 for that observation. No need to compound per-hour probabilities.

**Step 4: Fallback hierarchy for sparse bins**

When a bin has insufficient data, coarsen in this order. At each level, the **total** observation count (across both outcomes) must meet the threshold before the outcome split is applied.

1. **Full key** `(gp, topic, time_bucket, side, trailing_vol_bucket, outcome)` — need ≥ 200 total (both outcomes combined) in the parent `(gp, topic, time_bucket, side, trailing_vol_bucket)` bin, AND ≥ 50 per outcome after split.
2. **Drop `trailing_vol_bucket`** → `(gp, topic, time_bucket, side, outcome)` — merge all volume levels. Same thresholds: ≥ 200 total, ≥ 50 per outcome.
3. **Coarsen `time_bucket`** — merge adjacent: {<1h, 1-3h} → <3h, {3-6h, 6-12h} → 3-12h, {12-24h, 1-3d} → 12h-3d, {3-7d} stays. Same thresholds.
4. **Drop `outcome`** → `(gp, topic, coarsened_time_bucket, side)` — single CDF for won and lost. p_fill_won == p_fill_lost; adverse selection term drops out. Need ≥ 200 total.
5. **Drop `side`** → `(gp, topic, coarsened_time_bucket)` — merge YES and NO. Need ≥ 200 total.
6. **Terminal**: if (gp, topic) with all dimensions merged has < 200 observations, return None.

### Price-Agnostic Optimizer Behavior

The flow_table key excludes price — `estimate()` returns the same P(fill) regardless of `limit_price_cents`. This means the EV optimizer will favor lower limit prices (cheaper for us), since payoff increases monotonically as limit price decreases while P(fill) is flat.

This is **acceptable and expected** for our trading regime:
- The price search operates within the bid-ask spread (typically 2-10¢ wide). Within this narrow range, the flow sensitivity is negligible (~1pp).
- The dominant effect is correctly captured: lower prices mean higher payoff per contract.
- The optimizer picks the best price within the spread, which for a maker order is typically at or near the bid — the patient, EV-maximizing placement.
- At the extremes (bidding at min_tail=85¢ regardless of where the book is), the spread filter (`max_spread`) and the constraint that limit must be between bid and ask already prevent pathological behavior.

If future data shows price sensitivity > 5pp across the tail range, price should be added as a calibration dimension.

### Integration Points

#### MarketView (`market_view.py`)

FlowModel lives inside MarketView, replacing FillRateEstimator. The temporal boundary is enforced at MarketView construction: FlowModel is calibrated with `as_of` filtering.

**Updated constructor:**

```python
class MarketView:
    def __init__(self, as_of, all_observations, all_trades, settled_markets,
                 classifications, costs=KALSHI_COSTS):
        """
        Args:
            as_of: temporal boundary
            all_observations: list of Obs for EventRateEstimator (unchanged)
            all_trades: dict of ticker → list of (created_time, count,
                        yes_price, taker_side) for FlowModel calibration.
                        Includes all settled classified tickers; FlowModel
                        filters to as_of internally.
            settled_markets: dict of ticker → (settled_at, result, event_ticker)
            classifications: dict of series_ticker → (gp, topic)
        """
        # Filter observations to before as_of (unchanged)
        filtered_obs = [o for o in all_observations if o.settled_at < as_of]

        # Event rate estimator (unchanged)
        self._event_rates = EventRateEstimator()
        self._event_rates.set_classifications(classifications)
        self._event_rates.calibrate(filtered_obs, price_method='mid')

        # Flow model (replaces FillRateEstimator)
        self._flow_model = FlowModel.calibrate(
            all_trades, settled_markets, classifications, as_of=as_of)

    def fill_estimate(self, gp, topic, hours, side, quantity,
                      limit_price_cents, trailing_volume):
        """Replaces fill_rates(). Now quantity-dependent."""
        return self._flow_model.estimate(gp, topic, hours, side, quantity,
                                         limit_price_cents, trailing_volume)

    def fill_rates(self, gp, topic, hours, relative_price, side):
        """Legacy backward-compat wrapper — delegates to FlowModel with Q=1.

        WARNING: passes trailing_volume=0, which bins to the 'dead' volume
        bucket and understates fill probability for active markets. This is
        acceptable as a temporary bridge during migration; callers should
        migrate to fill_estimate() which accepts trailing_volume.
        """
        est = self._flow_model.estimate(gp, topic, hours, side, 1, 90, 0)
        if est is None:
            return None
        return (est.p_fill_won, est.p_fill_lost)

    # Existing interface preserved:
    # def event_rate(...) -> (p_yes, se, n_markets)
    # def classification(series) -> (gp, topic)
    # def maker_fee(price, contracts) -> fee_cents
```

**Updated from_db():**

```python
@classmethod
def from_db(cls, conn, as_of=None):
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    observations, classifications = preload_observations(conn)
    trades, settled = preload_trades(conn)  # new function
    return cls(as_of=as_of, all_observations=observations,
               all_trades=trades, settled_markets=settled,
               classifications=classifications)
```

**New preload function:**

```python
def preload_trades(conn):
    """Load trade tape and settled market data for FlowModel calibration.

    Returns:
        trades_by_ticker: dict of ticker → list of
            (created_time, count, yes_price, taker_side)
            sorted by created_time ascending.
        settled: dict of ticker → (settled_at, result, event_ticker)

    Only loads trades for settled, classified markets that have at least
    one trade at tail prices (yes_price >= 0.85 or yes_price <= 0.15).
    This ticker-level filter excludes markets that never enter the tail
    range. Within qualifying tickers, ALL trades are loaded (not just
    tail-price trades) because trailing volume computation needs the
    full trade history. FlowModel.calibrate() further filters at the
    observation level — Step 1 only counts opposing flow at 85-97¢ limits.

    Uses server-side cursor. Memory: ~2-4 GB for the ~50K-100K tickers
    that have tail-price trades in the Oct 2025 - Mar 2026 window.
    """
```

#### EVStrategy (`ev_strategy.py`)

**Updated EVOpportunity dataclass:**

```python
@dataclass
class EVOpportunity:
    ticker: str
    event_ticker: str
    series: str
    side: str
    limit_price: int
    ev_per_contract: float
    total_ev: float         # NEW: ev_per_contract * contracts
    p_event: float
    p_fill: float
    contracts: int          # now set by _find_best_order, not post-hoc
    days_to_settle: float
    generating_process: str
    topic: str
```

**Updated _find_best_limit → _find_best_order:**

Replaces `_find_best_limit()`. Now searches jointly over (side, price, quantity).

```python
# Capital constraint: max cents to deploy per order (from TradingParams)
# Existing formula: max(1, floor(400 / limit_price))
# This is equivalent to MAX_CAPITAL_PER_ORDER_CENTS = 400
MAX_CAPITAL_PER_ORDER_CENTS = 400

def _quantity_steps(self, max_contracts):
    """Generate quantity steps up to max_contracts.

    Uses geometric-ish spacing but always includes max_contracts itself
    so the optimizer can evaluate the full allowed size.
    E.g., max_contracts=8  → [1, 2, 4, 5, 8]
          max_contracts=12 → [1, 2, 4, 5, 8, 10, 12]
          max_contracts=20 → [1, 2, 4, 5, 8, 10, 15, 20]
          max_contracts=50 → [1, 2, 4, 5, 8, 10, 15, 20, 30, 50]
    """
    steps = []
    for q in [1, 2, 4, 5, 8, 10, 15, 20, 30, 50, 75, 100]:
        if q > max_contracts:
            break
        steps.append(q)
    if not steps or steps[-1] != max_contracts:
        steps.append(max_contracts)
    return steps

def _find_best_order(self, view, gp, topic, hours, yes_bid, yes_ask,
                     p_yes, trailing_vol, max_contracts):
    """Find (side, limit, q, total_ev, ev_per, p_fill) maximizing total EV."""
    best_total_ev = 0.0
    best = None
    q_steps = self._quantity_steps(max_contracts)

    for side in ('yes', 'no'):
        if side == 'yes':
            bid, ask = yes_bid, yes_ask
        else:
            bid = 100 - yes_ask
            ask = 100 - yes_bid

        # Enforce tail-price constraint: skip this side entirely if
        # the price range falls outside 85-97¢ (the calibrated region).
        # This is the structural guard ensuring the optimizer never
        # evaluates prices where FlowModel has no data.
        if bid > 97 or ask < 85:
            continue
        # Clamp search range to calibrated region
        search_bid = max(bid, 85)
        search_ask = min(ask, 97)

        for step in range(self.n_price_steps + 1):
            rel = step / self.n_price_steps
            limit = search_bid + int(round(rel * (search_ask - search_bid)))
            limit = max(search_bid, min(search_ask, limit))

            for q in q_steps:
                if q * limit > MAX_CAPITAL_PER_ORDER_CENTS:
                    break

                fill = view.fill_estimate(gp, topic, hours, side, q,
                                          limit, trailing_vol)
                if fill is None:
                    continue

                # Per-contract fee, matching compute_trade_ev's expectation
                fee_per_contract = view.maker_fee(limit, 1)

                ev_per = compute_trade_ev(p_yes, fill.p_fill_won,
                                          fill.p_fill_lost, limit, side,
                                          fee_per_contract)

                total_ev = ev_per * q

                if total_ev > best_total_ev:
                    if side == 'yes':
                        p_win = p_yes
                    else:
                        p_win = 1.0 - p_yes
                    p_fill = (p_win * fill.p_fill_won
                              + (1 - p_win) * fill.p_fill_lost)
                    best_total_ev = total_ev
                    best = (side, limit, q, total_ev, ev_per, p_fill)

    return best
```

**Updated scan():**

```python
# In scan(), the call site changes from:
best = self._find_best_limit(view, gp, topic, hours, yes_bid, yes_ask, p_yes)
side, limit_price, ev, p_fill = best
contracts = min(params.max_contracts, max(1, int(math.floor(400 / limit_price))))

# To:
trailing_vol = int(float(market.get('volume_24h_fp', '0')))
best = self._find_best_order(view, gp, topic, hours, yes_bid, yes_ask,
                              p_yes, trailing_vol, params.max_contracts)
side, limit_price, contracts, total_ev, ev_per, p_fill = best

# EVOpportunity now includes total_ev:
opportunities.append(EVOpportunity(
    ..., ev_per_contract=ev_per, total_ev=total_ev,
    contracts=contracts, ...))

# Sort changes from ev_per_contract to total_ev:
opportunities.sort(key=lambda o: -o.total_ev)

# Deduplication unchanged (one per event_ticker)
```

**Ranking rationale:** Sort by `total_ev` (not `ev_per_contract`). EV/contract always favors Q=1 when fill degrades with size, even when Q=20 has much higher total value. Concentration is prevented by:
- Per-event deduplication (one order per event_ticker)
- `max_contracts` from TradingParams (currently 8, will increase as confidence grows)
- `MAX_CAPITAL_PER_ORDER_CENTS` = 400 (caps deployment per order)

Note: EVStrategy does not currently have a `max_qualifying_events` cap — that parameter exists in TradingParams and the legacy FLBStrategy but is not enforced in EVStrategy. If concentration becomes a problem with total_ev ranking, this cap should be added to EVStrategy as a follow-on.

#### Replay (`replay.py`)

**Data loading architecture:**

The trade tape (206M rows) cannot be loaded entirely into memory or queried per-order. Instead:

```python
def preload_replay_trades(conn, tickers: set) -> dict[str, list[tuple]]:
    """Pre-load trade tape for the replay universe.

    Args:
        tickers: set of tickers that appear in the replay (from candle data)

    Returns:
        dict of ticker → list of (created_time, count, yes_price, taker_side)
        sorted by created_time ascending. Each entry is a namedtuple or
        tuple for memory efficiency.

    Uses server-side cursor. Only loads tickers in the replay universe.
    For a typical 6-month replay (~50K tickers), this is ~5-20M trades,
    fitting in memory (~2-4 GB).
    """
```

This is called once at replay startup, alongside the existing candle preload. The ticker set is known from the candle data.

**Trailing volume in replay:**

Replay must compute trailing 24h volume for each market at each period, since `volume_24h_fp` is not available from candle data.

```python
def compute_trailing_volume(trades: list[tuple], current_time: datetime) -> int:
    """Sum trade volumes in the 24h before current_time.

    Args:
        trades: sorted list of (created_time, count, yes_price, taker_side)
        current_time: the current replay period timestamp

    Implementation: binary search for the 24h-ago boundary using
    bisect on the created_time field (index 0 of each tuple), then
    sum count (index 1) for all trades in the [t-24h, t) window.

    For efficiency in the inner replay loop, callers may maintain a
    rolling index into the sorted trade list across consecutive periods,
    advancing the start pointer forward as the window slides. This
    avoids repeated binary searches and gives O(new_trades) per period
    instead of O(log n).
    """
```

This is injected into the market dict before passing to `EVStrategy.scan()`:

```python
# In replay period loop, before calling strategy.scan():
for market in event['markets']:
    ticker = market['ticker']
    trades = replay_trades.get(ticker, [])
    market['volume_24h_fp'] = str(compute_trailing_volume(
        trades, current_time))
```

**Fill simulation:**

Tape-based fill replaces candle-based FillModel. For each pending order, walk the pre-loaded trade list:

```python
def compute_opposing_flow(side: str, limit_price_cents: int,
                          trade_yes_price: float, trade_taker_side: str,
                          trade_count: int) -> int:
    """Compute how many contracts of a trade would fill our resting order.

    Args:
        side: 'yes' or 'no' — our resting side
        limit_price_cents: our limit price in cents
        trade_yes_price: the trade's yes_price in fractional dollars (0.00-1.00)
        trade_taker_side: 'yes' or 'no' — who was the aggressor
        trade_count: number of contracts in this trade

    Returns:
        Number of contracts that would fill our order (0 if trade
        doesn't match our side/price).

    Logic:
        YES buy at L: filled by taker_side='no' trades at
            yes_price <= L/100 (NO taker sells YES at our price or better)
        NO buy at L: filled by taker_side='yes' trades at
            yes_price >= (100-L)/100 (YES taker buys at price that
            implies no_price <= L)
    """
    if side == 'yes':
        if trade_taker_side == 'no' and trade_yes_price <= limit_price_cents / 100.0:
            return trade_count
    else:  # side == 'no'
        if trade_taker_side == 'yes' and trade_yes_price >= (100 - limit_price_cents) / 100.0:
            return trade_count
    return 0


def simulate_fill_from_tape(ticker, side, limit_price_cents, contracts,
                            placed_at, settlement_time, ticker_trades):
    """Simulate fill of a pending order against the trade tape.

    Called once at order placement time. Precomputes the full fill
    schedule by walking the trade tape from placed_at to settlement.
    The replay loop then processes these fills incrementally as it
    steps through periods — it checks pending_order['fill_schedule']
    and processes any fills whose timestamp falls in the current period.

    Args:
        ticker: market ticker (key into ticker_trades)
        side: 'yes' or 'no' — our resting side
        limit_price_cents: our limit price in cents
        contracts: number of contracts to fill
        placed_at: datetime of order placement
        settlement_time: datetime of market settlement
        ticker_trades: dict of ticker → sorted list of
            (created_time, count, yes_price, taker_side)

    Returns:
        list of (fill_time, contracts_filled) tuples, sorted by time.
        Sum of contracts_filled <= contracts (may be partial if
        insufficient opposing flow before settlement).
    """
    import bisect

    trades = ticker_trades.get(ticker, [])
    # Binary search for first trade after placed_at.
    # trades[i][0] is created_time. Build a timestamp list for bisect.
    times = [t[0] for t in trades]  # O(n) but done once per order
    start_idx = bisect.bisect_right(times, placed_at)
    remaining = contracts
    fills = []
    for i in range(start_idx, len(trades)):
        t_time, t_count, t_yes_price, t_taker_side = trades[i]
        if t_time >= settlement_time:
            break
        opposing = compute_opposing_flow(
            side, limit_price_cents, t_yes_price, t_taker_side, t_count)
        if opposing > 0:
            filled = min(opposing, remaining)
            fills.append((t_time, filled))
            remaining -= filled
            if remaining <= 0:
                break
    return fills
```

**Replay integration pattern:** The fill schedule is precomputed at placement and stored on the pending order dict. The existing hourly replay loop processes fills incrementally:

```python
# At order placement (fields match existing replay pending_orders structure):
pending_order = {
    'ticker': ticker,
    'side': side,
    'price': limit_price,       # cents
    'contracts': contracts,
    'placed_at': current_time,
    'event': event_ticker,      # for settlement/recording
    'gp': gp,                   # for trade recording
    'topic': topic,             # for trade recording
    'edge': ev_per_contract,    # for trade recording
    'contracts_filled': 0,
    # NEW: precomputed fill schedule from trade tape
    'fill_schedule': simulate_fill_from_tape(
        ticker, side, limit_price, contracts,
        current_time, settlement_time, replay_trades),
    'fill_schedule_idx': 0,     # cursor into fill_schedule
}

# In hourly fill processing (replaces FillModel.check_fill loop):
# current_period_end = period_end timestamp for this hour
# previous_period_end = period_end from the prior hour (= current_period_end - 1h)
for order in list(pending_orders.values()):
    schedule = order['fill_schedule']
    idx = order['fill_schedule_idx']
    while idx < len(schedule):
        fill_time, fill_qty = schedule[idx]
        if fill_time > current_period_end:
            break  # future fill, process in a later period
        # This fill falls in the current period
        order['contracts_filled'] += fill_qty
        idx += 1
        # Record partial fill, update escrow (same as current replay
        # lines 298-309: reduce remaining, track fill events)
    order['fill_schedule_idx'] = idx

    # Check if fully filled → move from pending to filled
    if order['contracts_filled'] >= order['contracts']:
        # transition to filled_orders (same as current code)
        ...
```

This preserves the existing replay's incremental fill tracking and escrow accounting (lines 274, 298-309 of current replay.py) while using real trade data instead of the capture_rate heuristic. No double-counting risk because the fill schedule is computed once and consumed incrementally.

**Expanding-window replay path:**

The current expanding-window replay preloads data once and rebuilds MarketView periodically (daily for event rates, weekly for fill rates). The new architecture follows the same pattern:

```python
# At replay startup (replaces current preload block):
from market_view import preload_observations, preload_trades
observations, classifications = preload_observations(conn)
trades_by_ticker, settled_markets = preload_trades(conn)
replay_trades = preload_replay_trades(conn, set(ticker_candles.keys()))

expanding = {
    'observations': observations,
    'trades_by_ticker': trades_by_ticker,  # replaces 'fill_data'
    'settled_markets': settled_markets,     # new
    'classifications': classifications,
}

# At recalibration (replaces current MarketView construction):
view = MarketView(
    as_of=cutoff,
    all_observations=expanding['observations'],
    all_trades=expanding['trades_by_ticker'],
    settled_markets=expanding['settled_markets'],
    classifications=expanding['classifications'])

# FlowModel caching strategy:
# FlowModel.calibrate() is ~30-60s (comparable to EventRateEstimator).
# Rebuild weekly (same frequency as old FillRateEstimator).
# Between weekly rebuilds, cache and reuse the FlowModel:
if recal_fills:
    # Full rebuild including FlowModel
    view = MarketView(
        as_of=cutoff,
        all_observations=expanding['observations'],
        all_trades=expanding['trades_by_ticker'],
        settled_markets=expanding['settled_markets'],
        classifications=expanding['classifications'])
    cached_flow_model = view._flow_model
else:
    # Rebuild EventRateEstimator only, skip FlowModel calibration.
    # Pass empty trades dict to suppress FlowModel.calibrate() cost.
    view = MarketView(
        as_of=cutoff,
        all_observations=expanding['observations'],
        all_trades={},  # empty → FlowModel gets no data, ~instant
        settled_markets=expanding['settled_markets'],
        classifications=expanding['classifications'])
    view._flow_model = cached_flow_model  # inject cached model
```

This mirrors the existing caching pattern (lines 237-244 of current replay.py) where `cached_fill_estimator` is reused between weekly recalibrations. The new code replaces `_fill_rates` with `_flow_model`.

**Probabilistic fast mode:**

For parameter sweeps where tape simulation is too slow, replay accepts a `use_flow_model=True` flag:

```python
def replay(conn, strategy_cls, ..., use_flow_model=False):
    """
    If use_flow_model=True, skip tape-based fill simulation. Instead,
    at order placement:
    1. Call view.fill_estimate(gp, topic, hours, side, contracts,
       limit_price, trailing_vol) to get FlowEstimate.
    2. Compute unconditional P(fill) using the event rate:
       p_fill = p_win * est.p_fill_won + (1 - p_win) * est.p_fill_lost
       where p_win comes from view.event_rate() (same as EVStrategy).
    3. Bernoulli draw: if random() < p_fill, order fills.
    4. Fill time = midpoint between placement and settlement (conservative
       for escrow accounting — capital is tied up longer than reality).
    5. Fill is all-or-nothing (no partial fills in fast mode).

    trailing_vol for each market comes from compute_trailing_volume()
    (same as tape mode — this function is always available when trade
    data is loaded).

    Requires expanding mode (--expanding flag). The FlowModel comes from
    the MarketView constructed in the expanding-window path. If
    use_flow_model=True without expanding mode, raise ValueError.

    ~100x faster than tape simulation. Use for parameter sweeps;
    validate against tape mode before drawing conclusions.
    """
```

#### Live Trader (`trader.py`)

At scan time, the trader fetches events with nested markets. The `volume_24h_fp` field is already in the market dict from the API response. EVStrategy.scan() extracts it internally (see scan() changes above). No changes to trader.py's scan loop needed beyond passing market dicts through unchanged.

### Data Requirements

**Training data**: Trade tape for settled, classified markets. Currently 206M trades covering Jun 2021 - Mar 2026. Prices stored as fractional dollars (0.00-1.00) in the `yes_price` column.

**Temporal boundary**: `FlowModel.calibrate(as_of=...)` only uses trades from markets that settled before `as_of`.

**Calibration scope**: `preload_trades()` filters to tickers with at least one tail-price trade (`yes_price >= 0.85` or `yes_price <= 0.15`, fractional dollars). Within those tickers, all trades are loaded for trailing volume computation. `FlowModel.calibrate()` Step 1 further filters to only count opposing flow at 85-97¢ limits.

### What This Spec Does NOT Cover

- **Queue position modeling**: The flow model assumes we're first in queue at our price. In reality, there may be resting orders ahead of us. At our current sizes (5-20 contracts) in tail markets, queue competition is minimal. This becomes important at larger sizes and is a natural follow-on.
- **Fill catalysis**: Placing an order tightens the spread and may attract counterparty flow. This is unobservable from historical data and biases our fill estimate conservatively (real fills ≥ predicted fills).
- **Time-varying flow**: Intraday patterns (e.g., sports markets active during games, financial during market hours) are captured implicitly by the trailing volume feature but not modeled explicitly.
- **Non-tail prices**: The model is calibrated for tail prices (85-97¢) only. EVStrategy constrains its price search to this range via min_tail/max_tail parameters.

## Build Order

### Phase 1: FlowModel core + calibration

1. `trading/flow_model.py` — FlowCDF, FlowEstimate, FlowModel with `calibrate()` and `estimate()`
2. `tests/test_flow_model.py` — unit tests with synthetic flow data (CDF interpolation, extrapolation, fallback hierarchy, direction logic, outcome-merged semantics)
3. Calibration script to build flow tables from DB and validate against the virtual order simulation results

### Phase 2: Integration

4. Update `market_view.py`:
   - New `preload_trades()` function (server-side cursor, returns dict)
   - Updated `__init__` accepting `all_trades` and `settled_markets` (replaces `all_fill_data`)
   - Updated `from_db()` calling `preload_trades()`
   - New `fill_estimate()` method
   - Legacy `fill_rates()` wrapper (documented trailing_volume=0 limitation)
5. Update `ev_strategy.py`:
   - `EVOpportunity` gains `total_ev` field
   - `_find_best_limit()` → `_find_best_order()` with joint (side, price, quantity) search
   - `_quantity_steps()` generates steps up to max_contracts (covers current max=8)
   - Tail-price guard: skip sides where bid-ask falls outside 85-97¢
   - `scan()` extracts `volume_24h_fp`, calls `_find_best_order`, sorts by `total_ev`
   - `compute_trade_ev()` unchanged (already per-contract)
6. Update `trader.py` — no changes needed; market dicts already contain `volume_24h_fp`
7. Update `replay.py`:
   - New `preload_replay_trades()` at startup
   - `compute_trailing_volume()` with rolling-index optimization
   - `compute_opposing_flow()` — explicit direction logic matching calibration
   - `simulate_fill_from_tape()` with partial fill support
   - Expanding-window path: `trades_by_ticker`/`settled_markets` replace `fill_data`, `_flow_model` cached weekly
   - `use_flow_model` flag for probabilistic fast mode

### Phase 3: Validation

8. Compare replay results: old FillModel vs new FlowModel (same strategy, different fill simulation)
9. Validate FlowModel predictions against 83 real portfolio fills
10. Run expanding-window replay to measure impact on strategy returns

## Verification Criteria

1. FlowModel.estimate() returns different P(fill) for Q=5 vs Q=50 (size-dependent)
2. P(fill_lost) > P(fill_won) for sports categories (adverse selection)
3. NO-side P(fill) > YES-side P(fill) for the same market (asymmetry)
4. Higher trailing volume → higher P(fill) (monotonic in activity)
5. Expanding-window replay returns within 20% of current performance (model change shouldn't destroy existing edge)
6. FlowModel predictions match virtual order simulation results within 5pp for bins with ≥500 observations
7. Fallback hierarchy is exercised: verify at least one category hits level 2+ coarsening
8. Tape-based replay fill simulation matches within 2% of direct virtual-order-simulation fill rates on the same tickers
