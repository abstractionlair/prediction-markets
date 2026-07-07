"""
EV-maximizing strategy.

For each market, evaluates all limit prices from bid to ask on both sides.
Selects the (side, limit_price) that maximizes expected PnL per contract:

  E[PnL] = P(event) × P(fill|event) × (100 - limit)
         - P(¬event) × P(fill|¬event) × limit
         - P(fill) × fee

The strategy receives a View — the single handle through which it
accesses all calibrated estimates. It has no database access, no knowledge
of calibration internals, and cannot see data beyond the view's as_of date.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from trading.strategy import BLOCKED_SERIES, DEFAULT_PARAMS


MAX_CAPITAL_PER_ORDER_CENTS = 400


@dataclass
class EVOpportunity:
    """A market opportunity with optimal limit price."""
    ticker: str
    event_ticker: str
    series: str
    side: str           # 'yes' or 'no'
    limit_price: int    # cents — optimal limit
    ev_per_contract: float  # expected cents PnL per contract
    total_ev: float     # ev_per_contract * contracts
    p_event: float      # P(YES) from calibration
    p_fill: float       # overall P(fill)
    contracts: int
    days_to_settle: float
    generating_process: str
    topic: str
    # Market state at scan time (for order logging / fill model features)
    yes_bid: int = 0
    yes_ask: int = 0
    volume: int = 0
    open_interest: int = 0

    @property
    def ev_per_day(self) -> float:
        """EV per contract per day — capital efficiency metric."""
        if self.days_to_settle <= 0:
            return self.ev_per_contract
        return self.ev_per_contract / self.days_to_settle


def compute_trade_ev(p_event, p_fill_won, p_fill_lost, limit_price, side, fee):
    """Expected PnL per contract for a proposed trade.

    Pure function — independently testable, no state.

    Args:
        p_event: P(YES) from event rate calibration
        p_fill_won: P(fill | our side wins)
        p_fill_lost: P(fill | our side loses)
        limit_price: price in cents we'd pay
        side: 'yes' or 'no'
        fee: maker fee in cents per contract at this price

    Returns:
        Expected PnL in cents per contract.
    """
    if side == 'yes':
        p_win = p_event
    else:
        p_win = 1.0 - p_event
    p_lose = 1.0 - p_win
    p_fill = p_win * p_fill_won + p_lose * p_fill_lost

    return (p_win * p_fill_won * (100 - limit_price)
            - p_lose * p_fill_lost * limit_price
            - p_fill * fee)


class EVStrategy:
    """EV-maximizing strategy.

    Receives a View (the capability boundary) and uses it to look up
    event rates, fill probabilities, and costs. Has no other data access.

    Two search modes controlled by joint_search:
    - False (default): search prices only, post-hoc quantity. Efficient
      when the fill model is size-independent (e.g., FillRateEstimator).
    - True: joint search over (side, price, quantity). Required when the
      fill model is size-dependent (e.g., FlowModel, FillPredictor).
    """

    def __init__(self, view, params=DEFAULT_PARAMS,
                 n_price_steps=10, min_bucket_markets=0,
                 joint_search=False):
        self.view = view
        self.params = params
        self.n_price_steps = n_price_steps
        self.min_bucket_markets = min_bucket_markets
        self.joint_search = joint_search

    def scan(self, events, traded_tickers=None, now=None):
        """Scan events for positive-EV opportunities.

        Returns list of EVOpportunity sorted by EV per contract descending.
        """
        if traded_tickers is None:
            traded_tickers = set()
        if now is None:
            now = datetime.now(timezone.utc)

        view = self.view
        params = self.params
        opportunities = []

        for event in events:
            event_ticker = event.get("event_ticker", "")
            series = event_ticker.split("-")[0] if event_ticker else ""
            markets = event.get("markets", [])

            if series in BLOCKED_SERIES:
                continue

            classification = view.classification(series)
            if classification is None:
                continue
            gp, topic = classification

            for market in markets:
                ticker = market.get("ticker", "")
                if not ticker or ticker in traded_tickers:
                    continue

                mstatus = market.get("status", "")
                if mstatus not in ("open", "active"):
                    continue

                close_time = (market.get("expected_expiration_time")
                              or market.get("close_time"))
                days_to_settle = self._parse_days_to_settle(close_time, now)
                if days_to_settle is None or days_to_settle < 0:
                    continue
                hours = days_to_settle * 24
                if hours < params.min_hours_to_settle:
                    continue
                if days_to_settle > params.max_days_to_settle:
                    continue
                hours = days_to_settle * 24

                yes_bid, yes_ask = self._parse_prices(market)
                if yes_bid is None:
                    continue
                spread = yes_ask - yes_bid
                if spread <= 0 or spread > params.max_spread:
                    continue

                yes_mid = (yes_bid + yes_ask) / 2.0
                result = view.event_rate(
                    series, hours, observed_price_dollars=yes_mid / 100.0)
                if result is None:
                    continue
                p_yes, se, n_markets = result

                # Skip cells with too few markets — calibration unreliable
                if n_markets < self.min_bucket_markets:
                    continue

                trailing_vol = int(float(market.get('volume_24h_fp', '0')))
                oi = int(market.get('open_interest', 0))

                if self.joint_search:
                    best = self._find_best_order(
                        view, gp, topic, hours, yes_bid, yes_ask,
                        p_yes, trailing_vol, params.max_contracts,
                        open_interest=oi)
                    if best is None:
                        continue
                    side, limit_price, contracts, total_ev, ev, p_fill = best
                else:
                    best = self._find_best_limit(
                        view, gp, topic, hours, yes_bid, yes_ask,
                        p_yes, trailing_vol, oi)
                    if best is None:
                        continue
                    side, limit_price, ev, p_fill = best
                    if limit_price <= 0:
                        continue
                    contracts = min(params.max_contracts,
                                    max(1, int(math.floor(400 / limit_price))))
                    total_ev = ev * contracts

                opportunities.append(EVOpportunity(
                    ticker=ticker,
                    event_ticker=event_ticker,
                    series=series,
                    side=side,
                    limit_price=limit_price,
                    ev_per_contract=ev,
                    total_ev=total_ev,
                    p_event=p_yes,
                    p_fill=p_fill,
                    contracts=contracts,
                    days_to_settle=days_to_settle,
                    generating_process=gp,
                    topic=topic,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    volume=trailing_vol,
                    open_interest=oi,
                ))

        opportunities.sort(key=lambda o: -o.total_ev)

        # Deduplicate: best opportunity per event (already sorted, so first wins)
        seen_events = set()
        result = []
        for opp in opportunities:
            if opp.event_ticker in seen_events:
                continue
            seen_events.add(opp.event_ticker)
            result.append(opp)

        return result

    def _find_best_limit(self, view, gp, topic, hours, yes_bid, yes_ask,
                         p_yes, trailing_vol=0, open_interest=0):
        """Find (side, limit_price, ev, p_fill) maximizing EV, or None.

        Searches over (side × price), quantity=1. Post-hoc sizing.
        Efficient for size-independent fill models.
        """
        best_ev = 0.0  # any positive EV; ranking + cap handle quality
        best = None

        for side in ('yes', 'no'):
            if side == 'yes':
                bid, ask = yes_bid, yes_ask
            else:
                bid = 100 - yes_ask
                ask = 100 - yes_bid

            for step in range(self.n_price_steps + 1):
                rel = step / self.n_price_steps
                limit = bid + int(round(rel * (ask - bid)))
                limit = max(bid, min(ask, limit))

                market_state = {
                    'bid': yes_bid, 'ask': yes_ask,
                    'hours_to_settlement': hours,
                    'generating_process': gp, 'topic': topic,
                    'trailing_volume': trailing_vol,
                    'open_interest': open_interest,
                }
                fill = view.fill_probability(side, limit, 1, market_state)
                if fill is None:
                    continue
                p_fill_won, p_fill_lost = fill.p_fill_won, fill.p_fill_lost

                fee = view.cost(limit, 1)

                ev = compute_trade_ev(p_yes, p_fill_won, p_fill_lost, limit, side, fee)

                if ev > best_ev:
                    if side == 'yes':
                        p_win = p_yes
                    else:
                        p_win = 1.0 - p_yes
                    p_fill = p_win * p_fill_won + (1 - p_win) * p_fill_lost
                    best_ev = ev
                    best = (side, limit, ev, p_fill)

        return best

    def _find_best_order(self, view, gp, topic, hours, yes_bid, yes_ask,
                         p_yes, trailing_vol, max_contracts,
                         open_interest=0):
        """Find (side, limit, q, total_ev, ev_per, p_fill) maximizing total EV.

        Joint search over (side, price, quantity) for size-dependent
        fill estimates.
        """
        best_total_ev = 0.0
        best = None
        q_steps = self._quantity_steps(max_contracts)

        for side in ('yes', 'no'):
            if side == 'yes':
                bid, ask = yes_bid, yes_ask
            else:
                bid = 100 - yes_ask
                ask = 100 - yes_bid

            # Skip sides outside calibrated tail range
            if bid > 97 or ask < 85:
                continue
            search_bid = max(bid, 85)
            search_ask = min(ask, 97)

            for step in range(self.n_price_steps + 1):
                rel = step / self.n_price_steps
                limit = search_bid + int(round(rel * (search_ask - search_bid)))
                limit = max(search_bid, min(search_ask, limit))

                market_state = {
                    'bid': yes_bid, 'ask': yes_ask,
                    'hours_to_settlement': hours,
                    'generating_process': gp, 'topic': topic,
                    'trailing_volume': trailing_vol,
                    'open_interest': open_interest,
                }

                for q in q_steps:
                    if q * limit > MAX_CAPITAL_PER_ORDER_CENTS:
                        break

                    fill = view.fill_probability(side, limit, q, market_state)
                    if fill is None:
                        continue

                    fee_per_contract = view.cost(limit, 1)
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

    @staticmethod
    def _quantity_steps(max_contracts):
        """Generate quantity steps up to max_contracts.

        Uses geometric-ish spacing but always includes max_contracts.
        """
        steps = []
        for q in [1, 2, 4, 5, 8, 10, 15, 20, 30, 50, 75, 100]:
            if q > max_contracts:
                break
            steps.append(q)
        if not steps or steps[-1] != max_contracts:
            steps.append(max_contracts)
        return steps

    @staticmethod
    def _parse_days_to_settle(close_time, now):
        if not close_time:
            return None
        try:
            if isinstance(close_time, str):
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            else:
                ct = close_time
            return (ct - now).total_seconds() / 86400
        except Exception:
            return None

    @staticmethod
    def _parse_prices(market):
        yb_str = market.get("yes_bid_dollars", "0")
        ya_str = market.get("yes_ask_dollars", "0")
        try:
            yes_bid = int(round(float(yb_str) * 100))
            yes_ask = int(round(float(ya_str) * 100))
            if yes_bid <= 0 or yes_ask <= 0:
                return None, None
            return yes_bid, yes_ask
        except (ValueError, TypeError):
            return None, None
