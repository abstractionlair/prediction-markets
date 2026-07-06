"""
Pure strategy logic for FLB tail trading.

All functions in this module are pure computations with no API or DB dependencies.
This is the canonical source for trading parameters, fee calculations, tail
identification, chain detection, and pair ranking.

Imported by trader.py (live execution), backtest.py (historical replay), and tests.
"""

import math
import random
import re
from dataclasses import dataclass


# ─── Trading parameters (single source of truth) ──────────────────

@dataclass
class TradingParams:
    """All trading parameters in one place.

    Used by both the live trader and backtester to ensure consistency.
    """
    min_tail: int = 85          # cents — lower bound of favorite zone
    max_tail: int = 97          # cents — upper bound of favorite zone
    max_spread: int = 10        # cents — max bid-ask spread for real quotes
    min_hours_to_settle: float = 3.0   # filters in-game sports adverse selection
    max_days_to_settle: float = 7.0
    max_contracts: int = 8
    maker_fee_rate: float = 0.0175
    min_edge: float = 0.005     # 0.5% minimum net edge (after fees)
    max_qualifying_events: int = 25
    scan_interval_seconds: int = 300

    @property
    def max_spread_dollars(self) -> float:
        """MAX_SPREAD in dollars (for calibration.py compatibility)."""
        return self.max_spread / 100.0


# Default instance
DEFAULT_PARAMS = TradingParams()

# Series blocklist: tickers to skip (e.g. structurally unsuitable, or found
# to have negative edge during your own calibration). Empty by default;
# populate from your own analysis.
BLOCKED_SERIES: set[str] = set()


# ─── Fee calculation ───────────────────────────────────────────────

def maker_fee(price_cents: int, contracts: int = 1,
              rate: float = DEFAULT_PARAMS.maker_fee_rate) -> int:
    """Canonical Kalshi maker fee in cents.

    Formula: ceil(rate * contracts * P * (1-P) * 100)
    where P = price_cents / 100.

    Returns fee in cents (minimum 1¢ per order for any non-zero amount).
    """
    p = price_cents / 100.0
    return math.ceil(rate * contracts * p * (1 - p) * 100)


def taker_fee(price_cents: int, contracts: int = 1,
              rate: float = 0.07) -> int:
    """Kalshi taker fee in cents."""
    p = price_cents / 100.0
    return math.ceil(rate * contracts * p * (1 - p) * 100)


# ─── Fee-optimal order sizing ─────────────────────────────────────

def optimal_quantity(price_cents: int, max_q: int = DEFAULT_PARAMS.max_contracts,
                     min_q: int = 1,
                     fee_rate: float = DEFAULT_PARAMS.maker_fee_rate) -> int:
    """Find quantity that minimizes maker fee per contract.

    At tail prices the fair fee per contract is often < 0.5¢, but
    ceil() rounds up to 1¢ for any q where the raw total < 1¢.
    Larger q amortizes the rounding; certain values hit near-integer
    fee totals and waste almost nothing.

    Returns the q in [min_q, max_q] with the lowest fee per contract.
    """
    p = price_cents / 100.0
    pp = p * (1 - p)
    best_q = min_q
    best_fpc = float('inf')
    for q in range(min_q, max_q + 1):
        raw_cents = fee_rate * q * pp * 100
        fee_cents = math.ceil(raw_cents)
        fpc = fee_cents / q
        if fpc < best_fpc:
            best_fpc = fpc
            best_q = q
    return best_q


# ─── Chain detection ──────────────────────────────────────────────

# Matches tickers ending with strike-like numeric suffixes:
#   -T67299.99, -234, -TOR4, -AB45.678
# Does NOT match name-only suffixes: -ARIZ, -USU, -WINNER
_STRIKE_RE = re.compile(r'-[A-Z]{0,4}[\d]+\.?[\d]*$')


def detect_chain(tickers: list[str]) -> bool:
    """Detect whether an event's tickers likely represent a monotone strike chain.

    A chain has ordered numeric thresholds (e.g., BTC > $65K, BTC > $70K).
    Returns True if there are 2+ tickers and >50% end with strike-like
    numeric suffixes. Single-ticker events are never chains.
    """
    if len(tickers) < 2:
        return False
    n_strike = sum(1 for t in tickers if _STRIKE_RE.search(t))
    return n_strike > len(tickers) / 2


# ─── Tail identification ─────────────────────────────────────────

@dataclass
class TailOpportunity:
    """A tail identified on one side of a market."""
    side: str       # 'yes' or 'no'
    mid: int        # midpoint price in cents (on this side)
    spread: int     # spread in cents (on this side)
    best_bid: int   # best bid in cents (on this side)
    best_ask: int   # best ask in cents (on this side)


def identify_tails(yes_bid: int, yes_ask: int,
                   params: TradingParams = DEFAULT_PARAMS) -> list[TailOpportunity]:
    """Identify which sides of a market are in the tail zone with real quotes.

    Takes YES-side bid/ask in cents. Computes NO-side via complement.
    Returns list of TailOpportunity for each qualifying side (0, 1, or 2).
    Returns empty list for invalid quotes (crossed, non-positive, out of range).
    """
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
        return []

    no_bid = 100 - yes_ask
    no_ask = 100 - yes_bid
    yes_spread = yes_ask - yes_bid
    no_spread = no_ask - no_bid
    yes_mid = (yes_bid + yes_ask) // 2
    no_mid = (no_bid + no_ask) // 2

    tails = []
    if params.min_tail <= yes_mid <= params.max_tail and yes_spread <= params.max_spread:
        tails.append(TailOpportunity(
            side='yes', mid=yes_mid, spread=yes_spread,
            best_bid=yes_bid, best_ask=yes_ask,
        ))
    if params.min_tail <= no_mid <= params.max_tail and no_spread <= params.max_spread:
        tails.append(TailOpportunity(
            side='no', mid=no_mid, spread=no_spread,
            best_bid=no_bid, best_ask=no_ask,
        ))
    return tails


# ─── Data structures ──────────────────────────────────────────────

@dataclass
class Opportunity:
    """A potential trade identified by the scanner."""
    ticker: str
    event_ticker: str
    series: str
    side: str           # 'yes' or 'no'
    bid_price: int      # our intended bid (cents)
    best_bid: int       # current best bid
    best_ask: int       # current best ask
    spread: int
    edge: float         # smoothed calibration edge (fraction, e.g. 0.03)
    days_to_settle: float
    edge_per_day: float  # edge / days_to_settle — ranking score
    generating_process: str = ""
    topic: str = ""
    title: str = ""


@dataclass
class TradePair:
    """A paired trade: YES on one contract, NO on another in the same event."""
    yes_opp: Opportunity
    no_opp: Opportunity
    edge_per_day: float  # average of the two legs
    is_chain: bool = False  # True if monotone strike chain (safe zone exists)

    @property
    def event_ticker(self):
        return self.yes_opp.event_ticker

    @property
    def generating_process(self):
        return self.yes_opp.generating_process

    @property
    def topic(self):
        return self.yes_opp.topic


# ─── Pair ranking and selection ───────────────────────────────────

def rank_and_select_pairs(pairs: list[TradePair],
                          max_events: int = DEFAULT_PARAMS.max_qualifying_events,
                          rng: random.Random | None = None,
                          ) -> list[TradePair]:
    """Rank pairs by edge/day, qualify top N events, then shuffle.

    1. Sort all pairs by edge_per_day descending
    2. Assign event ranks by first appearance in sorted order
    3. Keep only pairs from top max_events events
    4. Shuffle for capital diversity across events

    Pass rng for reproducible results (e.g., random.Random(42) in tests).
    """
    if not pairs:
        return []

    sorted_pairs = sorted(pairs, key=lambda p: -p.edge_per_day)

    event_rank = {}
    for p in sorted_pairs:
        if p.event_ticker not in event_rank:
            event_rank[p.event_ticker] = len(event_rank)

    qualifying = {ev for ev, rank in event_rank.items() if rank < max_events}
    qualified = [p for p in sorted_pairs if p.event_ticker in qualifying]
    if rng is not None:
        rng.shuffle(qualified)
    else:
        random.shuffle(qualified)
    return qualified


def net_edge(edge: float, price_cents: int, contracts: int,
             fee_rate: float = DEFAULT_PARAMS.maker_fee_rate) -> float:
    """Edge after fees, as a fraction.

    edge: raw calibration edge (fraction, e.g. 0.02)
    price_cents: entry price
    contracts: order size (affects fee per contract via ceil rounding)

    Returns net edge as fraction. Negative means fees exceed the edge.
    """
    fee_cents = maker_fee(price_cents, contracts, rate=fee_rate)
    fee_per_contract = fee_cents / contracts
    return edge - fee_per_contract / 100.0


def parse_strike_value(ticker: str) -> float | None:
    """Extract numeric strike from ticker suffix.

    Handles: KXBTCD-26MAR1522-T59999.99, KXJOBLESSCLAIMS-26MAR12-215000, etc.
    Returns None if no numeric strike found.
    """
    m = re.search(r'-T(-?[\d.]+)$', ticker)
    if m:
        return float(m.group(1))
    m = re.search(r'-(-?[\d.]+)$', ticker)
    if m:
        val = m.group(1)
        if re.match(r'^-?\d+\.?\d*$', val):
            try:
                return float(val)
            except ValueError:
                pass
    return None


def edge_per_day(edge: float, days_to_settle: float) -> float:
    """Compute edge per day, with floor to prevent div-by-zero for very short horizons."""
    return edge / max(days_to_settle, 1/48)
