"""
FLB Paired Tail Trading Strategy.

Pure strategy logic: takes market data and edge estimates, produces
ranked TradePairs. No API calls, no DB queries, no side effects.

Used by:
- trader.py (live execution): strategy.scan(events_from_api, ...)
- backtest.py (historical replay): could call scan() on synthetic events
- tests: scan() with synthetic data and fake edge lookup
"""

from collections import defaultdict
from datetime import datetime, timezone

from strategy import (
    BLOCKED_SERIES,
    DEFAULT_PARAMS,
    Opportunity,
    TradePair,
    TradingParams,
    detect_chain,  # fallback only — prefer market_structure from event dict
    edge_per_day as compute_edge_per_day,
    identify_tails,
    net_edge,
    optimal_quantity,
    parse_strike_value,
    rank_and_select_pairs,
)


class FLBStrategy:
    """FLB paired tail trading strategy.

    Scans Kalshi event data for tail opportunities on monotone strike chains,
    pairs YES + NO legs, ranks by edge/day, and selects top events.

    The edge_lookup must support:
        get_edge(series: str, hours: float) -> float | None
        get_classification(series: str) -> tuple[str, str] | None
    """

    def __init__(self, edge_lookup, params: TradingParams = DEFAULT_PARAMS):
        self.edge_lookup = edge_lookup
        self.params = params

    def scan(self, events: list[dict], traded_tickers: set | None = None,
             unpaired: bool = False, now: datetime | None = None,
             ) -> list[TradePair]:
        """Scan event dicts for paired tail opportunities.

        Args:
            events: Raw Kalshi event dicts (with nested 'markets' lists).
            traded_tickers: Tickers to skip (already have positions/orders).
            unpaired: If True, also return single-leg opportunities.
            now: Current time for settlement calculation (default: utcnow).

        Returns:
            Ranked, qualified, shuffled list of TradePairs.
        """
        if traded_tickers is None:
            traded_tickers = set()
        if now is None:
            now = datetime.now(timezone.utc)

        opportunities = []
        event_is_chain = {}
        params = self.params
        edge_lookup = self.edge_lookup

        for event in events:
            event_ticker = event.get("event_ticker", "")
            series = event_ticker.split("-")[0] if event_ticker else ""
            markets = event.get("markets", [])

            if series in BLOCKED_SERIES:
                continue

            classification = edge_lookup.get_classification(series)
            if classification is None:
                continue
            gp, topic = classification

            # Chain detection: prefer market_structure from event data,
            # fall back to ticker regex if not available.
            # Only allow pairing for processes with genuine monotone safe zones.
            _PAIRABLE_PROCESSES = {
                "continuous_underlyer", "scheduled_release", "counting_process",
            }
            if event_ticker not in event_is_chain:
                ms = event.get("market_structure")
                if ms is not None:
                    event_is_chain[event_ticker] = (ms == "monotone_threshold")
                else:
                    tickers = [m.get('ticker', '') for m in markets]
                    event_is_chain[event_ticker] = detect_chain(tickers)
                # Generating process filter: convergent_binary and hazard_process
                # events should never be paired, even if tickers look numeric.
                if gp not in _PAIRABLE_PROCESSES:
                    event_is_chain[event_ticker] = False

            for market in markets:
                ticker = market.get("ticker", "")
                if not ticker or ticker in traded_tickers:
                    continue

                mstatus = market.get("status", "")
                if mstatus not in ("open", "active"):
                    continue

                # Parse settlement time
                close_time = (market.get("expected_expiration_time")
                              or market.get("close_time"))
                days_to_settle = self._parse_days_to_settle(close_time, now)
                if days_to_settle is None:
                    continue
                if days_to_settle > params.max_days_to_settle or days_to_settle < 0:
                    continue

                hours_to_settle = days_to_settle * 24

                # Parse prices (needed for both edge lookup and tail detection)
                yes_bid, yes_ask = self._parse_prices(market)
                if yes_bid is None:
                    continue

                title = market.get("title", "")

                # Tail detection + edge lookup per tail
                # For CalibrationLookup, edge depends on the observed price.
                # For EdgeLookup (legacy), edge is price-independent.
                yes_mid = (yes_bid + yes_ask) // 2
                for tail in identify_tails(yes_bid, yes_ask, params):
                    edge = edge_lookup.get_edge(
                        series, hours_to_settle,
                        observed_price_cents=yes_mid,
                        side=tail.side)
                    if edge is None or edge <= 0:
                        continue
                    contracts = optimal_quantity(tail.mid, max_q=params.max_contracts)
                    ne = net_edge(edge, tail.mid, contracts,
                                 fee_rate=params.maker_fee_rate)
                    if ne < params.min_edge:
                        continue
                    epd = compute_edge_per_day(ne, days_to_settle)
                    opportunities.append(Opportunity(
                        ticker=ticker, event_ticker=event_ticker, series=series,
                        side=tail.side, bid_price=tail.mid,
                        best_bid=tail.best_bid, best_ask=tail.best_ask,
                        spread=tail.spread, edge=ne,
                        days_to_settle=days_to_settle, edge_per_day=epd,
                        generating_process=gp, topic=topic, title=title,
                    ))

        # Build pairs
        pairs = self._build_pairs(opportunities, event_is_chain, unpaired)

        # Rank and select
        return rank_and_select_pairs(pairs, max_events=params.max_qualifying_events)

    def size_order(self, opp: Opportunity) -> int:
        """Compute optimal order quantity for an opportunity."""
        return optimal_quantity(opp.bid_price, max_q=self.params.max_contracts)

    # ─── Private helpers ──────────────────────────────────────────

    @staticmethod
    def _parse_days_to_settle(close_time, now: datetime) -> float | None:
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
    def _parse_prices(market: dict) -> tuple[int | None, int | None]:
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

    @staticmethod
    def _build_pairs(opportunities: list[Opportunity],
                     event_is_chain: dict[str, bool],
                     unpaired: bool) -> list[TradePair]:
        """Group opportunities by event and build the best atomic trade per event.

        A trade is the atomic unit of decision — currently a pair (YES + NO)
        on a monotone chain, or a single leg in unpaired mode.

        Leg selection is by net edge (not spread). Spread is a filter
        (applied earlier in scan); within qualifying opportunities, we
        maximize edge.

        For chain pairs, verifies strike ordering (YES strike < NO strike)
        to ensure a valid safe zone.
        """
        event_sides = defaultdict(lambda: {'yes': [], 'no': []})
        for o in opportunities:
            event_sides[o.event_ticker][o.side].append(o)

        pairs = []
        for ev, sides in event_sides.items():
            has_both = sides['yes'] and sides['no']
            is_chain = event_is_chain.get(ev, False)

            if has_both and is_chain:
                # Find the best valid pair by combined edge/day.
                # A valid pair has YES strike < NO strike (safe zone).
                best_pair = None
                best_combined_epd = -1.0

                for y in sides['yes']:
                    y_strike = parse_strike_value(y.ticker)
                    for n in sides['no']:
                        n_strike = parse_strike_value(n.ticker)
                        # Verify strike ordering if parseable
                        if y_strike is not None and n_strike is not None:
                            if y_strike >= n_strike:
                                continue
                        combined_epd = (y.edge_per_day + n.edge_per_day) / 2
                        if combined_epd > best_combined_epd:
                            best_combined_epd = combined_epd
                            best_pair = (y, n)

                if best_pair is not None:
                    pairs.append(TradePair(yes_opp=best_pair[0], no_opp=best_pair[1],
                                           edge_per_day=best_combined_epd, is_chain=True))
                elif unpaired:
                    # No valid pair (all inverted) — offer as unpaired
                    all_opps = sides['yes'] + sides['no']
                    best = max(all_opps, key=lambda o: o.edge_per_day)
                    pairs.append(TradePair(yes_opp=best, no_opp=best,
                                           edge_per_day=best.edge_per_day))
            elif unpaired:
                # Non-chain events, or chain events with only one side
                all_opps = sides['yes'] + sides['no']
                best = max(all_opps, key=lambda o: o.edge_per_day)
                pairs.append(TradePair(yes_opp=best, no_opp=best,
                                       edge_per_day=best.edge_per_day))
        return pairs
