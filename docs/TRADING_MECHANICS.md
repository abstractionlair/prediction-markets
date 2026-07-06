# Kalshi Trading Mechanics

## 1. Market Structure

### 1.1 The Contract

A Kalshi market is a binary question that resolves YES or NO. Example: "Will BTC be above $75,000 at 8am March 16?"

At settlement, each contract pays exactly $1.00 — to the YES holder if the outcome is YES, to the NO holder if the outcome is NO.

### 1.2 Contract Creation: The YES/NO Pair

Contracts are born in pairs. When a trade matches a YES buyer with a NO buyer, the exchange creates one YES contract and one NO contract simultaneously. The YES buyer pays P¢, the NO buyer pays (100-P)¢, and the exchange holds the combined $1.00 in escrow until settlement.

Example: Alice wants to buy YES at 60¢. Bob wants to buy NO at 40¢. Their orders match:
- Alice pays 60¢, receives a YES contract. Settlement: $1.00 if YES, $0 if NO. (P&L: +40¢ or -60¢)
- Bob pays 40¢, receives a NO contract. Settlement: $1.00 if NO, $0 if YES. (P&L: +60¢ or -40¢)
- The exchange holds 60¢ + 40¢ = $1.00. It will distribute the full dollar to whichever side wins.

This is the fundamental act. Both parties are *buyers*. Both pay cash upfront. Neither has an obligation or margin call. One will profit, one will lose, and the sum is always zero.

### 1.3 Secondary Trading

Once contracts exist, holders can resell them. If Alice later wants to exit her YES position, she can sell her YES contract to Carol. This is a transfer of an existing contract, not the creation of a new pair.

On Kalshi's API:
- Alice: `side=yes, action=sell` — she gives up her YES contract, receives cash
- Carol: `side=yes, action=buy` — she pays cash, receives Alice's YES contract

The exchange matches these the same way it matches new-pair trades. From the order book's perspective, there is no distinction between "Alice selling her existing YES" and "someone new wanting to buy NO." This is why the book is unified.

### 1.4 The Order Book: Two Views of One Book

Because every new trade creates a YES/NO pair, a bid to buy YES at P¢ is simultaneously an offer for someone to buy NO at (100-P)¢. The order book is a single structure with two equivalent views:

| YES view | NO view | Same order? |
|----------|---------|-------------|
| YES bid at 60¢ | NO ask at 40¢ | Yes — "I'll pay 60¢ for YES" = "NO available at 40¢" |
| YES ask at 65¢ | NO bid at 35¢ | Yes — "YES available at 65¢" = "I'll pay 35¢ for NO" |

The identities:
```
no_bid  = 100 - yes_ask
no_ask  = 100 - yes_bid
yes_spread = yes_ask - yes_bid = no_ask - no_bid = no_spread
```

The spread is identical from both perspectives because it's the same book.

#### Worked example

Market: "Knicks win by 4+". The YES book shows: bid 89¢ / ask 92¢ (spread: 3¢).

| | YES view | NO view |
|--|----------|---------|
| Best bid | 89¢ (someone wants to buy YES) | 8¢ (= 100 - 92; someone wants to buy NO) |
| Best ask | 92¢ (someone selling YES, or new NO buyer at 8¢) | 11¢ (= 100 - 89; new YES buyer willing to pay 89¢) |
| Midpoint | 90.5¢ | 9.5¢ |
| Spread | 3¢ | 3¢ |

If you buy YES at the ask (92¢), you pay 92¢ and profit if the outcome is YES.
If you buy NO at the ask (11¢), you pay 11¢ and profit if the outcome is NO.
These are *different* trades with *different* counterparties, risk, and reward — but they're priced consistently through the shared book.

### 1.5 Events and Strike Chains

An **event** groups related contracts around the same underlying outcome. Example: "NBA: Knicks at Suns — Points Spread"

Contracts at different strikes:

| Contract | YES view | NO view |
|----------|----------|---------|
| "Knicks +4" | YES 89¢ (Knicks cover, likely) | NO 11¢ (Knicks don't cover, unlikely) |
| "Knicks +37" | YES 3¢ (Knicks win by 37+, extreme longshot) | NO 97¢ (Knicks don't win by 37+, near-certain) |
| "Suns +16" | YES 5¢ (Suns win by 16+, longshot) | NO 95¢ (Suns don't win by 16+, near-certain) |

Every contract has a favorite side (85-97¢) and a longshot side (3-15¢). For a bounded outcome space like point differential, extreme strikes exist in both directions — deep Knicks strikes and deep Suns strikes.

### 1.6 Liquidity Asymmetry

The probability distribution of the outcome is roughly symmetric around some central expectation, so there *should* be tail contracts in both directions. But the order book may not be.

A contract might have YES = 90¢ with a 3¢ spread (liquid) while a contract in the opposite tail of the same event has YES = 10¢ with a 47¢ spread (no real book). Both contracts exist on the exchange, but only one is tradeable.

This is common in practice. Spread markets (soccer, basketball, MLS) often have liquid books only on the favorite side of each strike. The longshot strikes for the less-likely team direction may have a single resting bid at 1¢ and no meaningful ask. The probability distribution has two tails, but the order book only has one liquid tail.

Multi-strike events (NBA player props with many thresholds, index strike chains) are more likely to have liquid books in both tails, because there are enough strikes to attract market makers on both sides.

### 1.7 Calibration

If prices are calibrated, a contract at YES = P¢ should resolve YES with probability P/100. At YES = 90¢, the outcome should be YES 90% of the time.

A miscalibration at any contract is visible from both sides simultaneously:

| View | Overpriced means | Underpriced means |
|------|-----------------|-------------------|
| YES at 90¢ | Resolves YES *less* than 90% | Resolves YES *more* than 90% |
| NO at 10¢ (same contract) | Resolves NO *less* than 10% | Resolves NO *more* than 10% |

When YES is overpriced, NO is underpriced on the same contract. There is only one price and one empirical resolution rate; "overpriced" and "underpriced" are perspectives on the same gap.

### 1.8 Fees

Kalshi charges fees on trade execution using the formula:

```
maker_fee = ceil(0.0175 × contracts × P × (1-P) × 100)    (in cents)
taker_fee = ceil(0.07  × contracts × P × (1-P) × 100)     (in cents)
```

Where P is the price as a fraction (0-1). The inner expression evaluates in cents before `ceil` is applied, so the minimum fee is 1¢. Fees are highest at P = 0.50 and approach zero in the tails. Larger orders amortize the rounding.

Resting limit orders (post_only) pay maker fees. Market orders pay taker fees.
