# Prediction Market Taxonomy

Markets are classified along four dimensions: **generating process**, **payoff type**, **market structure**, and **topic**. Each dimension is independent — any combination can occur.


## Generating Process

The stochastic mechanism by which probability evolves toward resolution. This is the primary axis for selecting analytical transformations (logit, hazard rate, implied distribution, etc.).

### Classification decision tree

Use this precedence ordering when classifying:

```
1. Does the series settle on a continuously-traded asset price/level?
     → continuous_underlyer
2. Does the series settle on a value revealed at a specific known time
   (data release, official decision, scheduled announcement)?
     → scheduled_release
3. Does the series settle on whether a cumulative count exceeds a threshold?
   (But: count-of-1 is hazard_process, not counting_process — see rule 4)
     → counting_process
4. Does the series settle on whether a discrete event happens by a deadline,
   where non-occurrence is informative?
     → hazard_process
5. Does the series settle via an explicit randomization mechanism
   (lottery, coin flip, random draw)?
     → explicit_randomization
6. Otherwise — binary event with an information channel that doesn't fit above:
     → convergent_binary
```

### continuous_underlyer

Probability is a function of an external, continuously-traded asset (price, index, yield) relative to a strike and time remaining. The underlyer provides a high-frequency, publicly observable signal. See also `payoff_type` for the distinction between terminal-value and path-dependent contracts.

*Examples: "Will BTC be above $75,000 at 8am on March 17?", "Will the S&P 500 hit a new all-time high this year?"*

### scheduled_release

A hidden value is revealed at a known time. Before the release, probability updates on indirect signals (leading indicators, surveys, adjacent data, institutional signaling). At the release moment, all uncertainty resolves in a single instant. This category covers both quantitative data releases (CPI, GDP) and institutional decisions (Fed rate, court rulings) — the distinction between these is captured by the `topic` dimension.

*Examples: "Will CPI MoM be above 0.3%?", "Will the Fed cut rates at the March meeting?"*

### hazard_process

"Will event X happen by time T?" where X is a discrete occurrence. The natural variable is the instantaneous hazard rate — the probability of X occurring in the next instant given it hasn't yet. As time passes without occurrence, the remaining window shrinks mechanically. Non-occurrence is informative. Note: "will at least 1 of X happen by T" is hazard_process (count-of-1), not counting_process.

*Examples: "Will a cabinet member resign by July?", "Will Trump visit China before 2027?"*

### convergent_binary

The residual category for binary events with an information channel that don't fit the categories above. Probability drifts as signals arrive. This category is intentionally heterogeneous — it encompasses both determined-but-hidden outcomes (votes being counted) and genuinely evolving outcomes (election months away). The common thread is gradual information accumulation toward a binary resolution.

*Examples: Elections, legislative votes, award results, sports season outcomes.*

### counting_process

Will a cumulative count exceed a threshold by a deadline? The count is an observable, monotonically increasing process. Probability depends on the current count, the implied rate, and time remaining. Excludes count-of-1 (use `hazard_process`).

*Examples: "Will there be more than 50 executive orders by June?", "Will Trump post more than 100 times this week?"*

### explicit_randomization

The outcome is determined by an explicit randomization mechanism or a process that is effectively random at the resolution timescale. Defined positively: there must be an identifiable source of randomness, not merely absence of known information. The price should be a martingale with no drift. This is NOT a bucket for "I don't know what's driving this" — use `convergent_binary` or `other` for uncertain cases.

*Examples: Draft lotteries, tiebreak lotteries, explicit coin-flip mechanisms. Rare on prediction market platforms.*

### Notes on process boundaries

- **scheduled_release vs continuous_underlyer**: Both may involve a known resolution time. The distinction is whether a high-frequency public price exists for the underlyer. Fed funds rate markets settle on a decision (`scheduled_release`) even though an EFFR exists, because the decision is the discontinuity.
- **convergent_binary vs scheduled_release**: Scheduled release has a sharp reveal at a known point where the outcome was hidden. Convergent binary has gradual information revelation where the outcome may still be evolving. Some institutional decisions (court rulings, confirmations) have features of both — use `scheduled_release` if there is a known decision date, `convergent_binary` if the timing is uncertain.
- **hazard_process vs convergent_binary**: Hazard process events can happen at any time (non-occurrence is informative and shrinks the window). Convergent binary events resolve at a known or approximately known point.
- **continuous_underlyer vs counting_process**: When a count is defined on a continuous underlyer (e.g., "Will BTC make more than 5 new ATHs this month?"), the count is the defining feature → `counting_process`. When a single threshold crossing is the question → `continuous_underlyer`.
- **Sports outcomes**: Regular-season championship questions (who wins the league) are `convergent_binary`. Individual game outcomes are typically `convergent_binary` as well (information accumulates through the game), though pre-game betting is closer to the boundary with `explicit_randomization`.


## Payoff Type

What function of the world state the contract pays on. This interacts with generating process: the same underlyer (e.g., BTC price) can support multiple payoff types with very different probability dynamics.

### terminal

Contract settles on the value of some observable at a specific time. "Will BTC be above $75,000 at 8am Friday?" Probability depends on current level, strike, and time remaining — standard binary option dynamics.

### barrier

Contract settles on whether an observable crosses a threshold at any point during a window. "Will BTC touch $100,000 before January 1?" Once the barrier is hit, the contract is resolved. Probability is monotonically non-decreasing after a touch and has different dynamics from terminal contracts (roughly 2x the probability of an equivalent terminal contract for an ATM forward).

### extremum

Contract settles on the running maximum or minimum of an observable. "Will the S&P 500 yearly max exceed 6,000?" Similar to barrier contracts but may involve running statistics over the observation window.

### binary_event

Contract settles on a discrete yes/no event that is not a function of a continuous observable. This is the default for `hazard_process`, `convergent_binary`, `scheduled_release` (non-threshold), and `explicit_randomization` contracts.

### Notes on payoff type

- `terminal`, `barrier`, and `extremum` primarily apply to `continuous_underlyer` markets. Most non-financial markets are `binary_event`.
- The distinction matters for dynamics analysis: terminal contracts follow Black-Scholes-like dynamics; barrier contracts have path-dependent probability that only increases; extremum contracts combine features of both.
- `market_structure` (monotone_threshold, exhaustive_partition) is orthogonal to payoff type — you can have a partition of terminal contracts or a threshold chain of barrier contracts.


## Market Structure

How a platform packages contracts within an event. Currently documented for Kalshi; other platforms (Polymarket, PredictIt) may use different structures. Stored per-event in `kalshi_events.market_structure` and `kalshi_settled_events.market_structure`, derived from `strike_type` per market + `mutually_exclusive` per event during collector discovery.

### standalone

A single yes/no contract. The event has one market.

### monotone_threshold

Markets form a survival function (or its inverse): "X > s₁", "X > s₂", "X > s₃", ... where prices decrease monotonically with the strike. Multiple contracts can resolve "yes" simultaneously. The direction (>, >=, <, <=) is recorded per-market in `kalshi_markets.strike_type`.

### exhaustive_partition

Markets form a mutually exclusive partition of the outcome space: "s₁ < X ≤ s₂", "s₂ < X ≤ s₃", ... Exactly one contract resolves "yes". Prices should sum to ~1 across the event. Kalshi flags these events with `mutually_exclusive = true`. Tail bookends may use `greater`/`less` strike types.

### Notes on structure

- Both `monotone_threshold` and `exhaustive_partition` encode the same implied distribution, just differently. The threshold chain gives the CDF directly; the partition gives the PDF directly.
- The direction within a monotone_threshold chain is recoverable from `kalshi_markets.strike_type` and is not duplicated on the classification.


## Topic

What the market is about. Independent of generating process, payoff type, and market structure. Primarily useful for identifying cross-market correlations and broad event-day grouping. Keep coarse — there is limited analytical alpha from fine-grained topic distinctions.

### financial

Crypto, equities, equity indices, commodities, FX, interest rates, yields, credit.

### economic_data

Official government/institutional statistics: CPI, GDP, unemployment, payrolls, housing starts, PMI, trade balance.

### politics_elections

Elections, primaries, referenda, approval ratings, vote margins, party control.

### government_policy

Decisions by officials or institutions: Fed rate decisions, legislation, executive orders, regulatory actions, pardons, nominations, confirmations.

### geopolitics

International relations, diplomacy, sanctions, treaties, state visits, military actions.

### entertainment_sports

Sports outcomes, award shows, reality TV, cultural events.

### science_technology

Technology milestones, scientific discoveries, space events, AI benchmarks.

### weather_environment

Weather events, climate data, natural disasters.

### other

Does not fit cleanly into the above topics.


## Putting It Together

A fully classified market has all four dimensions:

| Series | Platform | Process | Payoff | Structure | Topic |
|--------|----------|---------|--------|-----------|-------|
| KXBTC | Kalshi | continuous_underlyer | terminal | monotone_threshold,exhaustive_partition | financial |
| KXBTCMAX100 | Kalshi | continuous_underlyer | barrier | standalone | financial |
| KXINXMAXY | Kalshi | continuous_underlyer | extremum | monotone_threshold | financial |
| KXCPI | Kalshi | scheduled_release | binary_event | monotone_threshold | economic_data |
| KXFED | Kalshi | scheduled_release | binary_event | monotone_threshold | government_policy |
| KXHOUSERACE | Kalshi | convergent_binary | binary_event | standalone | politics_elections |
| KXCABOUT | Kalshi | hazard_process | binary_event | standalone | government_policy |
| KXTRUTHSOCIAL | Kalshi | counting_process | binary_event | monotone_threshold,exhaustive_partition | politics_elections |

This separation lets analysis code select the right transformation based on generating process (logit, hazard rate, implied distribution) and the right pricing model based on payoff type (Black-Scholes for terminal, barrier option models for barrier), independently of what the market is about or how the contracts are structured.


## Field Mapping

| Dimension | Field | Table | Status |
|-----------|-------|-------|--------|
| Generating process | `generating_process` | `market_classifications` | Populated |
| Payoff type | `payoff_type` | `market_classifications` | **To be added** |
| Market structure | `market_structure` | `kalshi_events`, `kalshi_settled_events` | Per-event, derived during discovery |
| Topic | `topic` | `market_classifications` | Partially populated (1,066 NULL) |
| Legacy | `process_category` | `market_classifications` | Dropped Mar 18, 2026. |
| Strike direction | `strike_type` | `kalshi_markets` | Per-market, populated by Kalshi |
| Mutual exclusivity | `mutually_exclusive` | `kalshi_events` | Per-event, populated by Kalshi |


## References

- Wolfers, J. & Zitzewitz, E. (2004). "Prediction Markets." *Journal of Economic Perspectives*, 18(2), 107-126. — Classifies contracts by payoff design: winner-take-all (reveals probability), index (reveals mean), spread (reveals median). A family of winner-take-all contracts at different thresholds reveals the full CDF. Our `market_structure` dimension captures this.
- Snowberg, E., Wolfers, J. & Zitzewitz, E. (2013). "Prediction Markets for Economic Forecasting." In *Handbook of Economic Forecasting*. — Extends the W&Z framework to economic applications.
- Hanson, R. (2003). "Combinatorial Information Market Design." *Information Systems Frontiers*. — LMSR market-making mechanism; focuses on mechanism design rather than contract taxonomy.
