"""Virtual order evaluator — wraps the fill simulator with historical state.

Given a hypothetical order (ticker, time, side, price, qty), reconstructs
the market state at that moment from the depth snapshot and trade tape,
runs the fill simulator forward, and returns features + outcome.

Output is one row per virtual order, suitable as training data for a
hazard/survival fill model.

Usage:
    from research.virtual_order import evaluate_virtual_order, get_depth_snapshot

    row = evaluate_virtual_order(
        conn, ticker='KXNBA-26-SAS',
        placement_time=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        side='yes', limit_price_cents=93, quantity=4,
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from research.fill_simulator import FillSimulator, SimResult, Trade, load_trades


# Default simulation horizon: 7 days (matches strategy's max_days_to_settle)
DEFAULT_HORIZON = timedelta(days=7)

# Recent activity windows for features
ACTIVITY_WINDOWS = {
    "5m": timedelta(minutes=5),
    "30m": timedelta(minutes=30),
    "2h": timedelta(hours=2),
}


@dataclass
class VirtualOrderResult:
    """One virtual order evaluation: placement context + fill outcome."""
    # Order parameters
    ticker: str
    placement_time: datetime
    side: str                          # 'yes' or 'no'
    limit_price_cents: int             # in yes_price terms (always)
    quantity: int

    # Market state at placement (from depth snapshot)
    snapshot_time: datetime | None
    snapshot_age_seconds: float | None  # how stale is our snapshot
    yes_bid: int | None
    yes_ask: int | None
    spread: int | None
    volume: int | None
    open_interest: int | None

    # Queue-state decomposition (Codex review)
    same_price_depth: float            # contracts at our exact price level on our side
                                        # (aka q_ahead — what we'd queue behind)
    better_price_depth: float          # contracts at strictly better prices on our side
                                        # (for YES buy: higher YES bids; NO buy: higher NO bids)
    is_level_populated: bool           # same_price_depth > 0 (joining an active level)
    is_new_best_price: bool            # our price would be the new best bid/ask on our side
    gap_to_nearest_populated: int | None  # yes-price distance from our level to nearest
                                        # level with any depth on our side (None if no other levels)
    would_cross_spread: bool           # our order would be marketable (taker, not maker) —
                                        # YES buy >= yes_ask, NO buy >= best NO ask

    distance_to_touch: int | None      # our_price - best_price_on_our_side
                                        # negative = we're behind touch
                                        # zero = at touch
                                        # positive = more aggressive than touch

    # Recent activity (volume in trades in last N before placement)
    vol_5m_before: int
    vol_30m_before: int
    vol_2h_before: int
    # Time since last trade at or through our exact level (seconds). None if no
    # such trade in the lookback window.
    time_since_trade_at_level_seconds: float | None

    # Fill outcome (from simulator)
    horizon_end: datetime
    contracts_filled: int
    fully_filled: bool
    time_to_first_fill_seconds: float | None
    time_to_full_fill_seconds: float | None
    num_fill_events: int

    # For validation
    n_trades_in_horizon: int

    # Compatibility alias (existing code expects this name)
    @property
    def q_ahead(self) -> float:
        return self.same_price_depth

    @property
    def depth_at_better_prices(self) -> float:
        return self.better_price_depth


def get_depth_snapshot(conn, ticker: str, before_time: datetime) -> dict | None:
    """Fetch the most recent depth snapshot at or before a given time.

    Returns dict with yes_bid, yes_ask, yes_levels, no_levels, etc., or
    None if no depth snapshot is available.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp, yes_bid, yes_ask, yes_bid_depth, yes_ask_depth,
               volume, open_interest, yes_levels, no_levels
        FROM prediction_markets.kalshi_snapshots
        WHERE ticker = %s
          AND timestamp <= %s
          AND yes_levels IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    """, (ticker, before_time))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    ts, stored_yes_bid, stored_yes_ask, yes_bid_depth, yes_ask_depth, vol, oi, yes_lv, no_lv = row
    # yes_levels/no_levels come back as already-parsed JSONB (list of [price, qty])
    if isinstance(yes_lv, str):
        yes_lv = json.loads(yes_lv)
    if isinstance(no_lv, str):
        no_lv = json.loads(no_lv)
    yes_lv = yes_lv or []
    no_lv = no_lv or []
    # Recompute best bid/ask from levels — the stored yes_bid/yes_ask fields
    # are buggy for snapshots collected before 2026-04-19 (took first entry
    # instead of max). See collectors/kalshi/snapshots.py history.
    yes_bid = max((lv[0] for lv in yes_lv), default=None)
    best_no_bid = max((lv[0] for lv in no_lv), default=None)
    yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
    return {
        "timestamp": ts,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_bid_depth": yes_bid_depth,
        "yes_ask_depth": yes_ask_depth,
        "volume": vol,
        "open_interest": oi,
        "yes_levels": yes_lv,
        "no_levels": no_lv,
    }


def compute_queue_state(snapshot: dict, side: str,
                        limit_price_cents: int) -> dict:
    """Compute queue-state features from a depth snapshot.

    The limit_price_cents is always in yes_price terms (matching the fill
    simulator's convention).

    The raw depth lives on our ORDER'S SIDE of the book:
      - YES buy joins yes_levels (priced as yes_price)
      - NO buy joins no_levels (priced as no_price = 100 - yes_price)

    Returns a dict with:
      same_price_depth: qty at our exact level on our side
      better_price_depth: qty at strictly better prices on our side
                          (higher yes_bid / higher no_bid fills first)
      is_level_populated: True if same_price_depth > 0
      is_new_best_price: True if no existing depth at our price OR better
      gap_to_nearest_populated: cents from our level to nearest populated level
                                 on our side (in our side's price space), or None
    """
    if side == "yes":
        levels = snapshot.get("yes_levels") or []
        our_level_price = limit_price_cents
    else:
        levels = snapshot.get("no_levels") or []
        our_level_price = 100 - limit_price_cents

    q_at = 0.0
    q_better = 0.0
    populated_prices = set()
    for entry in levels:
        price, qty = entry[0], entry[1]
        if qty > 0:
            populated_prices.add(price)
        if price == our_level_price:
            q_at += float(qty)
        elif price > our_level_price:
            q_better += float(qty)

    # Gap to nearest populated level (excluding our own price)
    other_prices = populated_prices - {our_level_price}
    if other_prices:
        gap = min(abs(p - our_level_price) for p in other_prices)
    else:
        gap = None

    is_level_populated = q_at > 0
    # Our order would be the new best if no existing depth at our price or better
    is_new_best = (q_at == 0) and (q_better == 0)

    return {
        "same_price_depth": q_at,
        "better_price_depth": q_better,
        "is_level_populated": is_level_populated,
        "is_new_best_price": is_new_best,
        "gap_to_nearest_populated": gap,
    }


# Backward-compat wrapper
def compute_q_ahead(snapshot: dict, side: str,
                    limit_price_cents: int) -> tuple[float, float]:
    """Legacy two-tuple API. Prefer compute_queue_state()."""
    q = compute_queue_state(snapshot, side, limit_price_cents)
    return q["same_price_depth"], q["better_price_depth"]


def get_recent_volume(trades: list[Trade], t_ref: datetime,
                      window: timedelta) -> int:
    """Sum trade quantities in [t_ref - window, t_ref]."""
    cutoff = t_ref - window
    return sum(
        t.quantity for t in trades
        if cutoff <= t.timestamp < t_ref
    )


def evaluate_virtual_order(
    conn,
    ticker: str,
    placement_time: datetime,
    side: str,
    limit_price_cents: int,
    quantity: int,
    horizon: timedelta = DEFAULT_HORIZON,
    prefetched_trades: list[Trade] | None = None,
    prefetched_snapshot: dict | None = None,
) -> VirtualOrderResult | None:
    """Evaluate a single virtual order.

    Looks up the most recent depth snapshot at or before placement_time,
    loads the trade tape from (placement_time - 2h) to
    (placement_time + horizon), computes features, and runs the simulator.

    Returns None if no depth snapshot is available for the ticker at
    that time (can't evaluate without Q_ahead).

    Args:
        prefetched_trades: if provided, skip the DB load. Useful for
            batch evaluation over the same ticker.
        prefetched_snapshot: if provided, skip the DB lookup.
    """
    assert side in ("yes", "no")
    assert 1 <= limit_price_cents <= 99
    assert quantity >= 1

    # 1. Depth snapshot (for Q_ahead)
    snapshot = prefetched_snapshot or get_depth_snapshot(
        conn, ticker, placement_time)
    if snapshot is None:
        return None

    snapshot_time = snapshot["timestamp"]
    snapshot_age = (placement_time - snapshot_time).total_seconds()

    queue_state = compute_queue_state(snapshot, side, limit_price_cents)

    # distance_to_touch: our side's best price minus our price
    # For YES buy: yes_bid is the best price on our side (we're bidding YES)
    # For NO buy: the best NO bid (in yes_price terms = 100 - yes_ask)
    yes_bid = snapshot.get("yes_bid")
    yes_ask = snapshot.get("yes_ask")
    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid

    distance_to_touch = None
    would_cross_spread = False
    if side == "yes" and yes_bid is not None:
        # Both in yes_price terms. Our price - best bid.
        # 0 = at touch, positive = higher bid (aggressive), negative = below.
        distance_to_touch = limit_price_cents - yes_bid
        # YES buy crosses if price >= yes_ask (would hit an existing ask)
        if yes_ask is not None:
            would_cross_spread = limit_price_cents >= yes_ask
    elif side == "no" and yes_ask is not None:
        # Best NO bid has yes_price = 100 - yes_ask.
        # Negative distance = our yes_price lower than best NO bid
        # => our no_price higher => more aggressive NO.
        best_no_bid_yes_price = 100 - yes_ask
        distance_to_touch = best_no_bid_yes_price - limit_price_cents
        # NO buy crosses if our no_price >= best NO ask (= 100 - yes_bid)
        # i.e., our yes_price <= yes_bid
        if yes_bid is not None:
            would_cross_spread = limit_price_cents <= yes_bid

    # 2. Trades
    horizon_end = placement_time + horizon
    trades_start = placement_time - timedelta(hours=2)  # for recent-vol features
    if prefetched_trades is not None:
        trades = prefetched_trades
    else:
        trades = load_trades(
            conn, ticker, t0=trades_start, t_end=horizon_end)

    vol_5m = get_recent_volume(trades, placement_time, ACTIVITY_WINDOWS["5m"])
    vol_30m = get_recent_volume(trades, placement_time, ACTIVITY_WINDOWS["30m"])
    vol_2h = get_recent_volume(trades, placement_time, ACTIVITY_WINDOWS["2h"])

    # Time since last trade at or through our level (on our side).
    # YES buy: trades at yes_price <= our_price that hit levels at or past ours.
    # NO buy: trades at yes_price >= our_price.
    time_since_trade = None
    for t in reversed(trades):
        if t.timestamp >= placement_time:
            continue
        if side == "yes":
            at_or_past = t.yes_price_cents <= limit_price_cents
            opposing = t.taker_side == "no"
        else:
            at_or_past = t.yes_price_cents >= limit_price_cents
            opposing = t.taker_side == "yes"
        if at_or_past and opposing:
            time_since_trade = (placement_time - t.timestamp).total_seconds()
            break

    # 3. Simulate
    sim_trades = [t for t in trades if t.timestamp >= placement_time]
    sim = FillSimulator(sim_trades)
    result: SimResult = sim.run(
        side=side, price_cents=limit_price_cents, quantity=quantity,
        q_ahead=queue_state["same_price_depth"],
        t0=placement_time, t_end=horizon_end,
    )

    # Compute time to full fill if applicable
    time_to_full = None
    if result.fully_filled and result.fills:
        time_to_full = (
            result.fills[-1].timestamp - placement_time).total_seconds()

    return VirtualOrderResult(
        ticker=ticker,
        placement_time=placement_time,
        side=side,
        limit_price_cents=limit_price_cents,
        quantity=quantity,
        snapshot_time=snapshot_time,
        snapshot_age_seconds=snapshot_age,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        spread=spread,
        volume=snapshot.get("volume"),
        open_interest=snapshot.get("open_interest"),
        same_price_depth=queue_state["same_price_depth"],
        better_price_depth=queue_state["better_price_depth"],
        is_level_populated=queue_state["is_level_populated"],
        is_new_best_price=queue_state["is_new_best_price"],
        gap_to_nearest_populated=queue_state["gap_to_nearest_populated"],
        would_cross_spread=would_cross_spread,
        distance_to_touch=distance_to_touch,
        vol_5m_before=vol_5m,
        vol_30m_before=vol_30m,
        vol_2h_before=vol_2h,
        time_since_trade_at_level_seconds=time_since_trade,
        horizon_end=horizon_end,
        contracts_filled=result.contracts_filled,
        fully_filled=result.fully_filled,
        time_to_first_fill_seconds=result.time_to_first_fill,
        time_to_full_fill_seconds=time_to_full,
        num_fill_events=len(result.fills),
        n_trades_in_horizon=len(sim_trades),
    )
