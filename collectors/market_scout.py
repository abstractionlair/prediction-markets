#!/usr/bin/env python3
"""
Market Scout — LLM-powered classifier for Kalshi prediction market series.

For each Kalshi series_ticker without a classification in market_classifications,
the scout:
  1. Gathers input data from Postgres (event info, market samples, settlement rules)
  2. Calls claude CLI to classify via LLM
  3. Stores the classification in prediction_markets.market_classifications

Usage:
    python3 market_scout.py                    # Full run: LLM classification
    python3 market_scout.py --dry-run          # Preview what would be classified
    python3 market_scout.py --status           # Show classification stats
    python3 market_scout.py --unclassified     # List unclassified series
    python3 market_scout.py --review           # Show series flagged for review
    python3 market_scout.py --series KXFOO     # Classify a specific series
    python3 market_scout.py --model opus       # Use a specific model for LLM tier
    python3 market_scout.py --batch-size 5     # Series per LLM call
    python3 market_scout.py --dry-run          # Show what would be classified
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

# ---------------------------------------------------------------------------
# Database helpers (same pattern as kalshi_collector.py)
# ---------------------------------------------------------------------------

def get_pg_dsn() -> str:
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if dsn:
        return dsn
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("CLAUDE_HUB_PG_DSN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("CLAUDE_HUB_PG_DSN not set")


def get_connection():
    conn = psycopg2.connect(get_pg_dsn())
    with conn.cursor() as cur:
        cur.execute("SET search_path TO prediction_markets, public")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Valid process categories
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {
    'financial_settlement',
    'economic_data_release',
    'policy_decision',
    'threshold_counting',
    'hazard_decay',
    'convergent_binary',
    'coin_flip',
    'entertainment',
    'other',
}

VALID_PROCESSES = {
    'continuous_underlyer',
    'scheduled_release',
    'hazard_process',
    'convergent_binary',
    'counting_process',
    'explicit_randomization',
    'other',
}

VALID_PAYOFF_TYPES = {
    'terminal',
    'barrier',
    'extremum',
    'binary_event',
    'other',
}

VALID_TOPICS = {
    'financial',
    'economic_data',
    'politics_elections',
    'government_policy',
    'geopolitics',
    'entertainment_sports',
    'science_technology',
    'weather_environment',
    'other',
}

# ---------------------------------------------------------------------------
# Data gathering from Postgres
# ---------------------------------------------------------------------------

def get_all_series(conn) -> list[dict]:
    """Get all distinct series with summary stats."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                e.series_ticker,
                e.category,
                COUNT(DISTINCT e.event_ticker) as event_count,
                COUNT(m.ticker) as market_count,
                COALESCE(SUM(m.volume), 0) as total_volume,
                COALESCE(SUM(m.open_interest), 0) as total_oi
            FROM kalshi_events e
            JOIN kalshi_markets m ON m.event_ticker = e.event_ticker
            WHERE e.series_ticker IS NOT NULL
            GROUP BY e.series_ticker, e.category
            ORDER BY total_volume DESC
        """)
        rows = cur.fetchall()
    return [
        {
            'series_ticker': r[0],
            'category': r[1],
            'event_count': r[2],
            'market_count': r[3],
            'total_volume': r[4],
            'total_oi': r[5],
        }
        for r in rows
    ]


def get_classified_series(conn) -> set[str]:
    """Get set of already-classified series_tickers."""
    with conn.cursor() as cur:
        cur.execute("SELECT series_ticker FROM market_classifications")
        return {r[0] for r in cur.fetchall()}


def get_series_detail(conn, series_ticker: str) -> dict:
    """Gather detailed info about a series for LLM classification.

    Returns event titles, sample market titles, settlement rules,
    strike structure, and volume stats.
    """
    detail = {
        'series_ticker': series_ticker,
        'event_titles': [],
        'market_samples': [],
        'rules_primary': None,
        'strike_info': {},
        'kalshi_category': None,
        'event_count': 0,
        'market_count': 0,
        'total_volume': 0,
    }

    with conn.cursor() as cur:
        # Event info
        cur.execute("""
            SELECT e.event_ticker, e.title, e.category, e.sub_title, e.mutually_exclusive
            FROM kalshi_events e
            WHERE e.series_ticker = %s
            ORDER BY e.event_ticker
            LIMIT 5
        """, (series_ticker,))
        events = cur.fetchall()
        detail['event_titles'] = [r[1] for r in events if r[1]]
        if events:
            detail['kalshi_category'] = events[0][2]
            detail['mutually_exclusive'] = events[0][4]

        # Aggregate stats
        cur.execute("""
            SELECT
                COUNT(DISTINCT e.event_ticker),
                COUNT(m.ticker),
                COALESCE(SUM(m.volume), 0)
            FROM kalshi_events e
            JOIN kalshi_markets m ON m.event_ticker = e.event_ticker
            WHERE e.series_ticker = %s
        """, (series_ticker,))
        row = cur.fetchone()
        if row:
            detail['event_count'] = row[0]
            detail['market_count'] = row[1]
            detail['total_volume'] = row[2]

        # Sample markets (diverse titles, rules, strikes)
        cur.execute("""
            SELECT m.ticker, m.title, m.rules_primary, m.strike_type, m.floor_strike, m.volume
            FROM kalshi_markets m
            JOIN kalshi_events e ON e.event_ticker = m.event_ticker
            WHERE e.series_ticker = %s
            ORDER BY m.volume DESC NULLS LAST
            LIMIT 5
        """, (series_ticker,))
        markets = cur.fetchall()
        for m in markets:
            detail['market_samples'].append({
                'ticker': m[0],
                'title': m[1],
                'strike_type': m[3],
                'floor_strike': m[4],
                'volume': m[5],
            })
            # Grab first non-null rules_primary
            if m[2] and not detail['rules_primary']:
                detail['rules_primary'] = m[2]

        # Strike structure: distinct strike types and ranges
        cur.execute("""
            SELECT
                m.strike_type,
                COUNT(*) as cnt,
                MIN(m.floor_strike) as min_strike,
                MAX(m.floor_strike) as max_strike
            FROM kalshi_markets m
            JOIN kalshi_events e ON e.event_ticker = m.event_ticker
            WHERE e.series_ticker = %s
            GROUP BY m.strike_type
        """, (series_ticker,))
        strikes = cur.fetchall()
        detail['strike_info'] = [
            {
                'strike_type': s[0],
                'count': s[1],
                'min_strike': s[2],
                'max_strike': s[3],
            }
            for s in strikes
        ]

        # Markets per event (chain depth proxy)
        cur.execute("""
            SELECT AVG(cnt)::int, MAX(cnt)
            FROM (
                SELECT COUNT(*) as cnt
                FROM kalshi_markets m
                JOIN kalshi_events e ON e.event_ticker = m.event_ticker
                WHERE e.series_ticker = %s
                GROUP BY e.event_ticker
            ) sub
        """, (series_ticker,))
        row = cur.fetchone()
        if row and row[0]:
            detail['avg_markets_per_event'] = row[0]
            detail['max_markets_per_event'] = row[1]

        # If no active event data, try settled events/markets
        if not detail['event_titles'] and not detail['market_samples']:
            cur.execute("""
                SELECT event_ticker, title
                FROM kalshi_settled_events
                WHERE split_part(event_ticker, '-', 1) = %s
                ORDER BY event_ticker
                LIMIT 5
            """, (series_ticker,))
            events = cur.fetchall()
            detail['event_titles'] = [r[1] for r in events if r[1]]

            if events:
                # Get sample markets from settled data
                event_tickers = [r[0] for r in events]
                cur.execute("""
                    SELECT ticker, title, volume
                    FROM kalshi_settled_markets
                    WHERE event_ticker = ANY(%s)
                    ORDER BY volume DESC NULLS LAST
                    LIMIT 5
                """, (event_tickers,))
                for m in cur.fetchall():
                    detail['market_samples'].append({
                        'ticker': m[0],
                        'title': m[1],
                        'strike_type': None,
                        'floor_strike': None,
                        'volume': m[2],
                    })

                # Aggregate stats from settled
                cur.execute("""
                    SELECT
                        COUNT(DISTINCT se.event_ticker),
                        COUNT(sm.ticker),
                        COALESCE(SUM(sm.volume), 0)
                    FROM kalshi_settled_events se
                    JOIN kalshi_settled_markets sm ON sm.event_ticker = se.event_ticker
                    WHERE split_part(se.event_ticker, '-', 1) = %s
                """, (series_ticker,))
                row = cur.fetchone()
                if row:
                    detail['event_count'] = row[0]
                    detail['market_count'] = row[1]
                    detail['total_volume'] = row[2]

    return detail


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

def build_llm_prompt(batch: list[dict]) -> str:
    """Build the classification prompt for a batch of series."""

    series_blocks = []
    for detail in batch:
        block = f"### Series: {detail['series_ticker']}\n"
        block += f"- Kalshi category: {detail.get('kalshi_category', 'unknown')}\n"
        block += f"- Events: {detail.get('event_count', 0)}, Markets: {detail.get('market_count', 0)}, Total volume: {detail.get('total_volume', 0):,}\n"

        if detail.get('avg_markets_per_event'):
            block += f"- Markets per event: avg {detail['avg_markets_per_event']}, max {detail.get('max_markets_per_event', '?')}\n"

        if detail.get('mutually_exclusive') is not None:
            block += f"- Mutually exclusive: {detail['mutually_exclusive']}\n"

        if detail.get('event_titles'):
            block += "- Event titles:\n"
            for t in detail['event_titles'][:3]:
                block += f"  - {t}\n"

        if detail.get('market_samples'):
            block += "- Sample markets (by volume):\n"
            for m in detail['market_samples'][:5]:
                block += f"  - {m['title']}"
                if m.get('strike_type'):
                    block += f" [strike_type={m['strike_type']}]"
                if m.get('floor_strike') is not None:
                    block += f" [floor_strike={m['floor_strike']}]"
                if m.get('volume'):
                    block += f" [vol={m['volume']:,}]"
                block += "\n"

        if detail.get('rules_primary'):
            rules = detail['rules_primary']
            if len(rules) > 800:
                rules = rules[:800] + "..."
            block += f"- Settlement rules:\n  {rules}\n"

        if detail.get('strike_info'):
            block += "- Strike structure:\n"
            for s in detail['strike_info']:
                block += f"  - type={s['strike_type']}, count={s['count']}"
                if s.get('min_strike') is not None:
                    block += f", range=[{s['min_strike']}, {s['max_strike']}]"
                block += "\n"

        series_blocks.append(block)

    series_text = "\n".join(series_blocks)

    prompt = textwrap.dedent(f"""\
    You are classifying Kalshi prediction market series along three dimensions:
    generating process, payoff type, and topic.

    ## Dimension 1: Generating Process

    The stochastic mechanism by which probability evolves toward resolution.
    Use this precedence ordering when classifying:

    ```
    1. Does the series settle on a continuously-traded asset price/level?
         -> continuous_underlyer
    2. Does the series settle on a value revealed at a specific known time
       (data release, official decision, scheduled announcement)?
         -> scheduled_release
    3. Does the series settle on whether a cumulative count exceeds a threshold?
       (But: count-of-1 is hazard_process, not counting_process -- see rule 4)
         -> counting_process
    4. Does the series settle on whether a discrete event happens by a deadline,
       where non-occurrence is informative?
         -> hazard_process
    5. Does the series settle via an explicit randomization mechanism
       (lottery, coin flip, random draw)?
         -> explicit_randomization
    6. Otherwise -- binary event with an information channel that doesn't fit above:
         -> convergent_binary
    ```

    If none fit, use **other** and set needs_review=true.

    ### Boundary cases

    - **scheduled_release vs continuous_underlyer**: Both may involve a known resolution time. The distinction is whether a high-frequency public price exists for the underlyer. Fed funds rate markets settle on a decision (scheduled_release) even though an EFFR exists, because the decision is the discontinuity.
    - **convergent_binary vs scheduled_release**: Scheduled release has a sharp reveal at a known point where the outcome was hidden. Convergent binary has gradual information revelation where the outcome may still be evolving. Use scheduled_release if there is a known decision date, convergent_binary if the timing is uncertain.
    - **hazard_process vs convergent_binary**: Hazard process events can happen at any time (non-occurrence is informative and shrinks the window). Convergent binary events resolve at a known or approximately known point.
    - **continuous_underlyer vs counting_process**: When a count is defined on a continuous underlyer (e.g., "Will BTC make more than 5 new ATHs this month?"), the count is the defining feature -> counting_process. When a single threshold crossing is the question -> continuous_underlyer.
    - **Sports outcomes**: Regular-season championship questions (who wins the league) are convergent_binary. Individual game outcomes are typically convergent_binary as well.

    ## Dimension 2: Payoff Type

    What function of the world state the contract pays on.

    - **terminal** — Contract settles on the value of some observable at a specific time. Standard binary option dynamics.
    - **barrier** — Contract settles on whether an observable crosses a threshold at any point during a window. Once hit, resolved.
    - **extremum** — Contract settles on the running maximum or minimum of an observable over a window.
    - **binary_event** — Contract settles on a discrete yes/no event that is not a function of a continuous observable. Default for hazard_process, convergent_binary, scheduled_release, and explicit_randomization contracts.
    - **other** — Does not fit.

    Note: terminal, barrier, and extremum primarily apply to continuous_underlyer markets. Most non-financial markets are binary_event.

    ## Dimension 3: Topic

    What the market is about. Independent of process and payoff type.

    - **financial** — Crypto, equities, indices, commodities, FX, interest rates, yields, credit.
    - **economic_data** — Official government/institutional statistics: CPI, GDP, unemployment, payrolls, housing starts, PMI, trade balance.
    - **politics_elections** — Elections, primaries, referenda, approval ratings, vote margins, party control.
    - **government_policy** — Decisions by officials/institutions: Fed rate decisions, legislation, executive orders, regulatory actions, pardons, nominations.
    - **geopolitics** — International relations, diplomacy, sanctions, treaties, state visits, military actions.
    - **entertainment_sports** — Sports outcomes, award shows, reality TV, cultural events.
    - **science_technology** — Technology milestones, scientific discoveries, space events, AI benchmarks.
    - **weather_environment** — Weather events, climate data, natural disasters.
    - **other** — Does not fit cleanly.

    ## Reference Data Sources We Already Collect

    - **FRED**: Treasury yields, CPI, PCE, employment, GDP, oil, gas, FX, gold, silver, copper, Fed funds rate, balance sheet, breakevens, mortgage rates, housing data.
    - **CBOE**: SPX and NDX options data.
    - **CoinGecko**: All major crypto prices (BTC, ETH, SOL, DOGE, XRP, SHIB, etc.).
    - **EIA**: Energy data (oil, gas, production).
    - **BLS**: Official employment and inflation data.
    - **CME FedWatch**: Fed rate decision probabilities.

    ## Series to Classify

    {series_text}

    ## Instructions

    For each series, provide:
    - **generating_process**: One of: continuous_underlyer, scheduled_release, hazard_process, convergent_binary, counting_process, explicit_randomization, other
    - **payoff_type**: One of: terminal, barrier, extremum, binary_event, other
    - **topic**: One of: financial, economic_data, politics_elections, government_policy, geopolitics, entertainment_sports, science_technology, weather_environment, other
    - **description**: Brief human-readable description (e.g., "Bitcoin daily price brackets")
    - **confidence**: "high", "medium", or "low"
    - **has_external_benchmark**: true/false
    - **benchmark_source**: If has_external_benchmark is true, what source? (e.g., "FRED DGS10" or "CoinGecko BTC")
    - **needs_review**: true if confidence is "low" or the series is ambiguous
    - **reasoning**: 1-2 sentences explaining your classification

    **Taxonomy feedback:** If you encounter markets that don't fit cleanly into the existing categories, or if you think a category description should be refined, add a "taxonomy_feedback" field with your suggestion. Optional — only when genuinely warranted.

    Respond with ONLY valid JSON — an array of objects, one per series. No markdown code fences, no commentary.

    Example output format:
    [
      {{
        "series_ticker": "KXFOO",
        "generating_process": "continuous_underlyer",
        "payoff_type": "terminal",
        "topic": "financial",
        "description": "Foo index daily brackets",
        "confidence": "high",
        "has_external_benchmark": true,
        "benchmark_source": "FRED FOOINDEX",
        "needs_review": false,
        "reasoning": "Settles on daily close of Foo index. FRED tracks this."
      }}
    ]
    """)

    return prompt



def classify_llm_batch(batch: list[dict], model: str = "sonnet") -> list[dict]:
    """Classify a batch of series via claude CLI.

    Returns list of classification dicts.
    """
    prompt = build_llm_prompt(batch)

    # Pipe prompt via stdin to avoid ARG_MAX limits on large batches
    cmd = ["claude", "--model", model, "--print"]
    # Scale timeout with batch size: 120s base + 20s per series
    timeout = max(120, 20 * len(batch) + 60)
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LANG": "en_US.UTF-8"},
        )
    except subprocess.TimeoutExpired:
        print(f"  LLM call timed out for batch of {len(batch)} series")
        return []
    except FileNotFoundError:
        print("  ERROR: 'claude' CLI not found. Install Claude Code.")
        return []

    if result.returncode != 0:
        print(f"  LLM call failed (exit {result.returncode}): {result.stderr[:200]}")
        return []

    raw = result.stdout.strip()

    # Try to extract JSON from the response
    # Sometimes the model wraps it in ```json ... ```
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Find start and end of JSON block
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        raw = "\n".join(lines[start:end])

    try:
        classifications = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  Failed to parse LLM response as JSON: {e}")
        print(f"  Raw response (first 500 chars): {raw[:500]}")
        return []

    if not isinstance(classifications, list):
        print(f"  LLM response is not a list: {type(classifications)}")
        return []

    # Validate and enrich
    valid = []
    for c in classifications:
        if not isinstance(c, dict):
            continue
        if 'series_ticker' not in c:
            continue

        # Validate generating_process
        gp = c.get('generating_process', 'other')
        if gp == 'irreducible_random':
            gp = 'explicit_randomization'  # handle legacy name from LLM
        if gp not in VALID_PROCESSES:
            print(f"  WARNING: Invalid generating_process '{gp}' for {c['series_ticker']}, setting to 'other'")
            c['generating_process'] = 'other'
            c['needs_review'] = True
        else:
            c['generating_process'] = gp

        # Validate payoff_type
        pt = c.get('payoff_type', 'binary_event')
        if pt not in VALID_PAYOFF_TYPES:
            print(f"  WARNING: Invalid payoff_type '{pt}' for {c['series_ticker']}, defaulting to 'binary_event'")
            c['payoff_type'] = 'binary_event'
        else:
            c['payoff_type'] = pt

        # Validate topic
        tp = c.get('topic', 'other')
        if tp not in VALID_TOPICS:
            print(f"  WARNING: Invalid topic '{tp}' for {c['series_ticker']}, setting to 'other'")
            c['topic'] = 'other'
            c['needs_review'] = True
        else:
            c['topic'] = tp

        # Handle categorical confidence -> numeric
        conf_str = c.get('confidence', 'medium')
        if isinstance(conf_str, str):
            conf_map = {'high': 0.9, 'medium': 0.7, 'low': 0.4}
            c['confidence'] = conf_map.get(conf_str.lower(), 0.5)
            if conf_str.lower() == 'low':
                c['needs_review'] = True
        elif isinstance(conf_str, (int, float)):
            c['confidence'] = float(conf_str)
        else:
            c['confidence'] = 0.5

        c['classified_by'] = model
        c.setdefault('has_external_benchmark', False)
        c.setdefault('benchmark_source', None)
        c.setdefault('needs_review', c.get('confidence', 0) < 0.6)
        c.setdefault('reasoning', '')
        c.setdefault('description', '')

        # Persist taxonomy_feedback if present
        feedback = c.get('taxonomy_feedback')
        if feedback:
            print(f"  TAXONOMY FEEDBACK [{c['series_ticker']}]: {feedback}")
            reasoning = c.get('reasoning', '')
            c['reasoning'] = reasoning + "\n\nTaxonomy feedback: " + feedback if reasoning else "Taxonomy feedback: " + feedback

        valid.append(c)

    return valid


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def store_classification(conn, c: dict):
    """Upsert a single classification into market_classifications."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_classifications
                (series_ticker, confidence, has_external_benchmark,
                 benchmark_source, reasoning, classified_by, classified_at,
                 needs_review, description, generating_process, topic, payoff_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (series_ticker) DO UPDATE SET
                confidence = EXCLUDED.confidence,
                has_external_benchmark = EXCLUDED.has_external_benchmark,
                benchmark_source = EXCLUDED.benchmark_source,
                reasoning = EXCLUDED.reasoning,
                classified_by = EXCLUDED.classified_by,
                classified_at = EXCLUDED.classified_at,
                needs_review = EXCLUDED.needs_review,
                description = EXCLUDED.description,
                generating_process = EXCLUDED.generating_process,
                topic = EXCLUDED.topic,
                payoff_type = EXCLUDED.payoff_type
        """, (
            c['series_ticker'],
            c.get('confidence', 1.0),
            c.get('has_external_benchmark', False),
            c.get('benchmark_source'),
            c.get('reasoning'),
            c.get('classified_by', 'unknown'),
            datetime.now(timezone.utc),
            c.get('needs_review', False),
            c.get('description'),
            c.get('generating_process'),
            c.get('topic'),
            c.get('payoff_type'),
        ))
    conn.commit()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_status(conn):
    """Show classification statistics."""
    with conn.cursor() as cur:
        # Total series in DB
        cur.execute("""
            SELECT COUNT(DISTINCT e.series_ticker)
            FROM kalshi_events e WHERE e.series_ticker IS NOT NULL
        """)
        total_series = cur.fetchone()[0]

        # Classified
        cur.execute("SELECT COUNT(*) FROM market_classifications")
        classified = cur.fetchone()[0]

        # By category
        cur.execute("""
            SELECT generating_process, topic, classified_by, COUNT(*), AVG(confidence)::numeric(3,2)
            FROM market_classifications
            GROUP BY generating_process, topic, classified_by
            ORDER BY generating_process, topic, classified_by
        """)
        by_category = cur.fetchall()

        # Needs review
        cur.execute("SELECT COUNT(*) FROM market_classifications WHERE needs_review = true")
        needs_review = cur.fetchone()[0]

        # With benchmarks
        cur.execute("SELECT COUNT(*) FROM market_classifications WHERE has_external_benchmark = true")
        has_benchmark = cur.fetchone()[0]

    print("Market Scout Status")
    print("=" * 60)
    print(f"  Total series in DB:       {total_series:,}")
    print(f"  Classified:               {classified:,}")
    print(f"  Unclassified:             {total_series - classified:,}")
    print(f"  Needs review:             {needs_review:,}")
    print(f"  Has external benchmark:   {has_benchmark:,}")
    print()

    if by_category:
        print("  Category breakdown:")
        print(f"  {'Category':<30} {'By':<12} {'Count':>6} {'Avg Conf':>9}")
        print(f"  {'-'*30} {'-'*12} {'-'*6} {'-'*9}")
        for cat, by, count, avg_conf in by_category:
            print(f"  {cat:<30} {by:<12} {count:>6} {avg_conf:>9}")
    print()


def cmd_unclassified(conn):
    """List unclassified series."""
    all_series = get_all_series(conn)
    classified = get_classified_series(conn)

    unclassified = [s for s in all_series if s['series_ticker'] not in classified]

    print(f"Unclassified series: {len(unclassified)}")
    print(f"{'Series':<30} {'Category':<15} {'Events':>7} {'Markets':>8} {'Volume':>12}")
    print(f"{'-'*30} {'-'*15} {'-'*7} {'-'*8} {'-'*12}")
    for s in unclassified[:50]:
        print(f"{s['series_ticker']:<30} {s['category'] or '':<15} {s['event_count']:>7} {s['market_count']:>8} {s['total_volume']:>12,}")
    if len(unclassified) > 50:
        print(f"  ... and {len(unclassified) - 50} more")


def cmd_review(conn):
    """Show series flagged for review."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mc.series_ticker, mc.generating_process, mc.topic, mc.confidence,
                   mc.classified_by, mc.reasoning, mc.description
            FROM market_classifications mc
            WHERE mc.needs_review = true
            ORDER BY mc.confidence ASC
        """)
        rows = cur.fetchall()

    if not rows:
        print("No series flagged for review.")
        return

    print(f"Series flagged for review: {len(rows)}")
    print()
    for r in rows:
        print(f"  {r[0]:<25} cat={r[1]:<25} conf={r[2]:.2f}  by={r[3]}")
        if r[5]:
            print(f"    desc: {r[5]}")
        if r[4]:
            print(f"    reason: {r[4][:120]}")
        print()


def cmd_classify_series(conn, series_ticker: str, model: str = "sonnet", dry_run: bool = False):
    """Classify a specific series via LLM."""
    detail = get_series_detail(conn, series_ticker)
    if not detail.get('event_titles') and not detail.get('market_samples'):
        print(f"  {series_ticker}: No data found in database. Cannot classify.")
        return

    print(f"  {series_ticker}: Calling {model} for classification...")
    if dry_run:
        print(f"    [DRY RUN] Would classify via LLM. Data summary:")
        print(f"    Kalshi category: {detail.get('kalshi_category')}")
        print(f"    Events: {detail.get('event_count')}, Markets: {detail.get('market_count')}")
        if detail.get('event_titles'):
            print(f"    Event title: {detail['event_titles'][0]}")
        return

    results = classify_llm_batch([detail], model)
    for c in results:
        print(f"    -> {c.get('generating_process', '?')}/{c.get('topic', '?')} (conf={c.get('confidence', '?')}) {c.get('description', '')}")
        if c.get('needs_review'):
            print(f"    ** FLAGGED FOR REVIEW: {c.get('reasoning', '')}")
        store_classification(conn, c)
        print(f"    Stored.")

    if not results:
        print(f"    No classification returned by LLM.")


def cmd_backfill_structure(conn, dry_run: bool = False):
    """Backfill kalshi_settled_events.market_structure from sibling events.

    For each settled event with NULL market_structure, look for any sibling
    (same series_ticker) in kalshi_events or kalshi_settled_events that has
    a known market_structure and copy it.
    """
    with conn.cursor() as cur:
        # Count before
        cur.execute("""
            SELECT COUNT(*) FROM kalshi_settled_events WHERE market_structure IS NULL
        """)
        null_before = cur.fetchone()[0]

        if dry_run:
            # Preview what would be updated
            cur.execute("""
                WITH known AS (
                    SELECT DISTINCT series_ticker, market_structure
                    FROM kalshi_events
                    WHERE market_structure IS NOT NULL
                    UNION
                    SELECT DISTINCT split_part(event_ticker, '-', 1), market_structure
                    FROM kalshi_settled_events
                    WHERE market_structure IS NOT NULL
                )
                SELECT COUNT(DISTINCT se.event_ticker)
                FROM kalshi_settled_events se
                JOIN known k ON k.series_ticker = split_part(se.event_ticker, '-', 1)
                WHERE se.market_structure IS NULL
            """)
            fillable = cur.fetchone()[0]
            print(f"Backfill market_structure (dry run):")
            print(f"  NULL settled events: {null_before:,}")
            print(f"  Can fill from siblings: {fillable:,}")
            print(f"  Would remain NULL: {null_before - fillable:,}")
            return

        # Step 1: Fill from active events (authoritative source)
        cur.execute("""
            UPDATE kalshi_settled_events se
            SET market_structure = ke.market_structure
            FROM (
                SELECT DISTINCT ON (series_ticker) series_ticker, market_structure
                FROM kalshi_events
                WHERE market_structure IS NOT NULL
                ORDER BY series_ticker
            ) ke
            WHERE split_part(se.event_ticker, '-', 1) = ke.series_ticker
              AND se.market_structure IS NULL
        """)
        from_active = cur.rowcount
        print(f"  Filled {from_active:,} from active events")

        # Step 2: Fill remaining from settled siblings
        cur.execute("""
            UPDATE kalshi_settled_events se
            SET market_structure = src.market_structure
            FROM (
                SELECT DISTINCT ON (split_part(event_ticker, '-', 1))
                       split_part(event_ticker, '-', 1) as series_ticker,
                       market_structure
                FROM kalshi_settled_events
                WHERE market_structure IS NOT NULL
                ORDER BY split_part(event_ticker, '-', 1)
            ) src
            WHERE split_part(se.event_ticker, '-', 1) = src.series_ticker
              AND se.market_structure IS NULL
        """)
        from_settled = cur.rowcount
        print(f"  Filled {from_settled:,} from settled siblings")

        conn.commit()

        # Count after
        cur.execute("""
            SELECT COUNT(*) FROM kalshi_settled_events WHERE market_structure IS NULL
        """)
        null_after = cur.fetchone()[0]
        print(f"  Total filled: {from_active + from_settled:,}")
        print(f"  Remaining NULL: {null_after:,} (was {null_before:,})")


def cmd_reclassify_bulk(conn, model: str = "sonnet",
                        batch_size: int = 5, dry_run: bool = False):
    """Reclassify series that were bulk-classified from finfeed archive.

    These 677 series were classified by pattern matching (e.g., 'sports
    pattern match') without looking at actual contract descriptions.
    Many sports totals/spreads are counting_process, not convergent_binary.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT series_ticker, generating_process, topic, reasoning
            FROM market_classifications
            WHERE reasoning LIKE 'Bulk classified%%'
            ORDER BY series_ticker
        """)
        rows = cur.fetchall()

    if not rows:
        print("No bulk-classified series found.")
        return

    series_tickers = [r[0] for r in rows]
    print(f"Bulk-classified series to reclassify: {len(rows)}")

    if dry_run:
        by_reason = {}
        for _, gp, topic, reason in rows:
            by_reason.setdefault(reason, []).append(1)
        for reason, items in sorted(by_reason.items(), key=lambda x: -len(x[1])):
            print(f"  {reason}: {len(items)}")
        print(f"\n  [DRY RUN] Would reclassify {len(rows)} series via {model}")
        return

    # Gather details
    print(f"  Gathering series details...")
    details = []
    for st in series_tickers:
        detail = get_series_detail(conn, st)
        details.append(detail)

    # Batch and classify
    total_classified = 0
    total_changed = 0

    for i in range(0, len(details), batch_size):
        batch = details[i:i + batch_size]
        batch_tickers = [d['series_ticker'] for d in batch]
        batch_num = (i // batch_size) + 1
        total_batches = (len(details) + batch_size - 1) // batch_size

        print(f"\n  Batch {batch_num}/{total_batches}: {', '.join(batch_tickers)}")

        results = classify_llm_batch(batch, model)

        result_map = {c['series_ticker']: c for c in results}
        for d in batch:
            st = d['series_ticker']
            old_row = next((r for r in rows if r[0] == st), None)
            old_gp = old_row[1] if old_row else '?'
            if st in result_map:
                c = result_map[st]
                new_gp = c.get('generating_process', '?')
                changed = " CHANGED" if new_gp != old_gp else ""
                print(f"    {st:<30} {old_gp:<22} -> {new_gp:<22}{changed}")
                store_classification(conn, c)
                total_classified += 1
                if new_gp != old_gp:
                    total_changed += 1
            else:
                print(f"    {st:<30} -> NO RESULT")

    print(f"\nReclassification complete: {total_classified} processed, {total_changed} changed")


def cmd_full_run(conn, model: str = "sonnet",
                 batch_size: int = 5, dry_run: bool = False,
                 reclassify_all: bool = False):
    """Full classification run via LLM."""
    all_series = get_all_series(conn)
    classified = get_classified_series(conn)

    if reclassify_all:
        unclassified = all_series
        print(f"RECLASSIFY ALL: {len(all_series)} series (ignoring existing classifications)")
    else:
        unclassified = [s for s in all_series if s['series_ticker'] not in classified]
        print(f"Total series: {len(all_series)}, Already classified: {len(classified)}, To classify: {len(unclassified)}")
    print()

    if not unclassified:
        print("All series are already classified.")
        return

    # --- LLM classification for all series ---
    print(f"LLM classification via {model}: {len(unclassified)} series in batches of {batch_size}")

    if dry_run:
        n_batches = (len(unclassified) + batch_size - 1) // batch_size
        print(f"  [DRY RUN] Would send {len(unclassified)} series to {model} in {n_batches} batches")
        for s in unclassified[:20]:
            print(f"    {s['series_ticker']:<30} ({s['category']}, vol={s['total_volume']:,})")
        if len(unclassified) > 20:
            print(f"    ... and {len(unclassified) - 20} more")
        return

    # Gather details for all series
    print(f"  Gathering series details...")
    details = []
    for s in unclassified:
        detail = get_series_detail(conn, s['series_ticker'])
        details.append(detail)

    # Batch and classify
    total_classified = 0
    total_review = 0

    for i in range(0, len(details), batch_size):
        batch = details[i:i + batch_size]
        batch_tickers = [d['series_ticker'] for d in batch]
        batch_num = (i // batch_size) + 1
        total_batches = (len(details) + batch_size - 1) // batch_size

        print(f"\n  Batch {batch_num}/{total_batches}: {', '.join(batch_tickers)}")

        results = classify_llm_batch(batch, model)

        # Match results back to batch
        result_map = {c['series_ticker']: c for c in results}
        for d in batch:
            st = d['series_ticker']
            if st in result_map:
                c = result_map[st]
                status = "REVIEW" if c.get('needs_review') else "OK"
                print(f"    {st:<30} -> {c.get('generating_process', '?'):<20}/{c.get('topic', '?'):<15} [{status}]")
                store_classification(conn, c)
                total_classified += 1
                if c.get('needs_review'):
                    total_review += 1
            else:
                print(f"    {st:<30} -> NO RESULT (LLM did not return classification)")

    print(f"\nClassification complete: {total_classified} classified, {total_review} flagged for review")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Market Scout: classify Kalshi prediction market series"
    )

    # Mode flags (mutually exclusive)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true",
                      help="Show classification statistics")
    mode.add_argument("--unclassified", action="store_true",
                      help="List unclassified series")
    mode.add_argument("--review", action="store_true",
                      help="Show series flagged for review")
    mode.add_argument("--series", type=str,
                      help="Classify a specific series ticker")
    mode.add_argument("--reclassify", action="store_true",
                      help="Reclassify ALL series (full re-run with current taxonomy)")
    mode.add_argument("--reclassify-bulk", action="store_true",
                      help="Reclassify only bulk-classified series (finfeed archive)")
    mode.add_argument("--backfill-structure", action="store_true",
                      help="Backfill kalshi_settled_events.market_structure from siblings")

    # Options
    parser.add_argument("--model", type=str, default="sonnet",
                        help="Model for LLM classification (default: sonnet)")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="Number of series per LLM call (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be classified without doing it")

    args = parser.parse_args()

    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.status:
            cmd_status(conn)
        elif args.unclassified:
            cmd_unclassified(conn)
        elif args.review:
            cmd_review(conn)
        elif args.reclassify:
            cmd_full_run(
                conn,
                model=args.model,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                reclassify_all=True,
            )
        elif args.reclassify_bulk:
            cmd_reclassify_bulk(
                conn,
                model=args.model,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
            )
        elif args.backfill_structure:
            cmd_backfill_structure(conn, dry_run=args.dry_run)
        elif args.series:
            cmd_classify_series(conn, args.series, model=args.model, dry_run=args.dry_run)
        else:
            cmd_full_run(
                conn,
                model=args.model,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
