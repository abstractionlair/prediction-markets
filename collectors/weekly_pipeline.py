#!/usr/bin/env python3
"""
Weekly Data Pipeline — Prediction Markets

Orchestrates the four-step weekly data pipeline defined in DATA_ARCHITECTURE.md:

1. Classify new series — call market_scout for unclassified series
2. Download settled outcomes — per-series + events-endpoint paths
3. Fill categories — map uncategorized settled events to classifications
4. Rebuild settled_with_prices — derived analysis table

Each step is idempotent and independent. If one fails, the pipeline continues.

Usage:
    python3 weekly_pipeline.py                  # Full pipeline
    python3 weekly_pipeline.py --step classify  # Just step 1
    python3 weekly_pipeline.py --step settled   # Just step 2
    python3 weekly_pipeline.py --step categories # Just step 3
    python3 weekly_pipeline.py --step materialize # Just step 4
    python3 weekly_pipeline.py --status         # Show current state
    python3 weekly_pipeline.py --skip-scout     # Skip LLM classification
"""

import argparse
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

import psycopg2


def _scalar(cur) -> int:
    """Fetch a single scalar value from the cursor, defaulting to 0."""
    row = cur.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Database helpers (shared pattern)
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
# Logging
# ---------------------------------------------------------------------------

def log(msg: str):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


def log_step(step_num: int, name: str):
    print(f"\n{'=' * 60}", flush=True)
    log(f"STEP {step_num}: {name}")
    print('=' * 60, flush=True)


# ---------------------------------------------------------------------------
# Step 1: Classify new series
# ---------------------------------------------------------------------------

def step_classify(conn, skip_scout: bool = False) -> dict:
    """Classify any unclassified series using market_scout's LLM classifier.

    If claude CLI isn't available or skip_scout is True, skips classification.
    """
    log_step(1, "Classify new series")

    result = {"new_classifications": 0, "skipped": False, "error": None}

    try:
        from collectors.market_scout import (
            get_all_series,
            get_classified_series,
            get_series_detail,
            store_classification,
        )
    except ImportError as e:
        log(f"WARNING: Cannot import market_scout: {e}")
        result["error"] = str(e)
        return result

    all_series = get_all_series(conn)
    classified = get_classified_series(conn)
    unclassified = [s for s in all_series if s['series_ticker'] not in classified]

    if not unclassified:
        log("All series already classified.")
        return result

    log(f"Found {len(unclassified)} unclassified series.")

    if skip_scout:
        log(f"Skipping LLM classification (--skip-scout). {len(unclassified)} series remain unclassified.")
        result["skipped"] = True
        return result

    # Check if claude CLI is available
    if not shutil.which("claude"):
        log("WARNING: claude CLI not found. Skipping LLM classification.")
        log(f"  {len(unclassified)} series remain unclassified.")
        result["skipped"] = True
        return result

    # LLM classification
    try:
        from collectors.market_scout import classify_llm_batch

        log(f"LLM classification for {len(unclassified)} series...")
        batch_size = 5
        llm_count = 0

        for i in range(0, len(unclassified), batch_size):
            batch = unclassified[i:i + batch_size]
            details = []
            for s in batch:
                detail = get_series_detail(conn, s['series_ticker'])
                details.append(detail)

            batch_tickers = [d['series_ticker'] for d in details]
            batch_num = (i // batch_size) + 1
            total_batches = (len(unclassified) + batch_size - 1) // batch_size
            log(f"  Batch {batch_num}/{total_batches}: {', '.join(batch_tickers)}")

            results = classify_llm_batch(details)
            result_map = {c['series_ticker']: c for c in results}

            for d in details:
                st = d['series_ticker']
                if st in result_map:
                    c = result_map[st]
                    store_classification(conn, c)
                    llm_count += 1
                    log(f"    {st} -> {c.get('generating_process', '?')}/{c.get('topic', '?')} (conf={c.get('confidence', '?')})")
                else:
                    log(f"    {st} -> NO RESULT from LLM")

        result["new_classifications"] = llm_count
        log(f"LLM classifications: {llm_count}")

    except Exception as e:
        log(f"WARNING: LLM classification failed: {e}")
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Step 2: Download settled outcomes
# ---------------------------------------------------------------------------

def step_settled(conn) -> dict:
    """Report settled market counts (download handled by kalshi-settled-sync.timer at 06:00 UTC)."""
    log_step(2, "Settled outcomes (managed by data system)")

    result = {"markets": 0, "events": 0, "error": None}

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM kalshi_settled_markets")
    result["markets"] = _scalar(cur)
    cur.execute("SELECT COUNT(*) FROM kalshi_settled_events")
    result["events"] = _scalar(cur)

    log(f"Settled markets: {result['markets']:,}")
    log(f"Settled events:  {result['events']:,}")
    log("(Download handled by kalshi-settled-sync.timer — Sun 06:00 UTC)")

    return result


# ---------------------------------------------------------------------------
# Step 3: Fill categories for uncategorized settled events
# ---------------------------------------------------------------------------

def step_categories(conn) -> dict:
    """Map uncategorized settled events to classifications via ticker prefix."""
    log_step(3, "Fill categories for uncategorized events")

    result = {"updated": 0, "error": None}

    cur = conn.cursor()

    # Count uncategorized before
    cur.execute("""
        SELECT COUNT(*) FROM kalshi_settled_events
        WHERE category IS NULL OR category = ''
    """)
    before = _scalar(cur)
    log(f"Uncategorized events before: {before:,}")

    if before == 0:
        log("No uncategorized events. Nothing to do.")
        return result

    # Map old-format tickers to classifications via KX prefix
    # Uses topic for display-label mapping (topic is the subject-matter dimension)
    cur.execute("""
        UPDATE kalshi_settled_events se
        SET category = CASE mc.topic
            WHEN 'financial' THEN 'Financials'
            WHEN 'economic_data' THEN 'Economics'
            WHEN 'politics_elections' THEN 'Elections'
            WHEN 'government_policy' THEN 'Politics'
            WHEN 'entertainment_sports' THEN 'Entertainment'
            WHEN 'geopolitics' THEN 'Geopolitics'
            WHEN 'science_technology' THEN 'Science'
            WHEN 'weather_environment' THEN 'Weather'
            ELSE COALESCE(mc.topic, 'Other')
        END
        FROM market_classifications mc
        WHERE (se.category IS NULL OR se.category = '')
          AND mc.series_ticker = 'KX' || split_part(se.event_ticker, '-', 1)
    """)
    kx_updated = cur.rowcount
    conn.commit()
    log(f"  Updated via KX prefix match: {kx_updated}")

    # Also try direct prefix match (no KX) for any remaining
    cur.execute("""
        UPDATE kalshi_settled_events se
        SET category = CASE mc.topic
            WHEN 'financial' THEN 'Financials'
            WHEN 'economic_data' THEN 'Economics'
            WHEN 'politics_elections' THEN 'Elections'
            WHEN 'government_policy' THEN 'Politics'
            WHEN 'entertainment_sports' THEN 'Entertainment'
            WHEN 'geopolitics' THEN 'Geopolitics'
            WHEN 'science_technology' THEN 'Science'
            WHEN 'weather_environment' THEN 'Weather'
            ELSE COALESCE(mc.topic, 'Other')
        END
        FROM market_classifications mc
        WHERE (se.category IS NULL OR se.category = '')
          AND mc.series_ticker = split_part(se.event_ticker, '-', 1)
    """)
    direct_updated = cur.rowcount
    conn.commit()
    log(f"  Updated via direct prefix match: {direct_updated}")

    # Also try matching via kalshi_events.series_ticker for events that exist
    cur.execute("""
        UPDATE kalshi_settled_events se
        SET category = CASE mc.topic
            WHEN 'financial' THEN 'Financials'
            WHEN 'economic_data' THEN 'Economics'
            WHEN 'politics_elections' THEN 'Elections'
            WHEN 'government_policy' THEN 'Politics'
            WHEN 'entertainment_sports' THEN 'Entertainment'
            WHEN 'geopolitics' THEN 'Geopolitics'
            WHEN 'science_technology' THEN 'Science'
            WHEN 'weather_environment' THEN 'Weather'
            ELSE COALESCE(mc.topic, 'Other')
        END
        FROM kalshi_events ke
        JOIN market_classifications mc ON mc.series_ticker = ke.series_ticker
        WHERE (se.category IS NULL OR se.category = '')
          AND se.event_ticker = ke.event_ticker
    """)
    ke_updated = cur.rowcount
    conn.commit()
    log(f"  Updated via kalshi_events join: {ke_updated}")

    total_updated = kx_updated + direct_updated + ke_updated
    result["updated"] = total_updated

    # Count remaining uncategorized
    cur.execute("""
        SELECT COUNT(*) FROM kalshi_settled_events
        WHERE category IS NULL OR category = ''
    """)
    after = _scalar(cur)
    log(f"\nUncategorized events: {before:,} -> {after:,} ({total_updated} updated)")

    return result


# ---------------------------------------------------------------------------
# Step 4: Rebuild settled_with_prices
# ---------------------------------------------------------------------------

def step_materialize(conn) -> dict:
    """Rebuild the settled_with_prices derived table.

    Joins settled_markets with snapshots/finfeed for last price before settlement,
    then joins to classifications and settled_events for analysis.
    Uses DISTINCT ON to get the last price per ticker from each source.
    Applies spread filter (<=20c) for snapshot data.
    """
    log_step(4, "Rebuild settled_with_prices")

    result = {"rows": 0, "error": None}

    cur = conn.cursor()

    log("Building settled_with_prices (this may take a few minutes)...")

    # Build into a new table, then atomically swap to avoid downtime.
    # If something fails between CREATE and RENAME, the old table stays intact.
    cur.execute("DROP TABLE IF EXISTS settled_with_prices_new")
    conn.commit()

    # The query:
    # 1. Get last snapshot price per ticker (spread <= 20c, before settlement)
    # 2. Get last FinFeed price per ticker (before settlement)
    # 3. UNION ALL both, take the latest per ticker
    # 4. Join to classifications (3-level fallback) and settled_events
    build_sql = """
    CREATE TABLE settled_with_prices_new AS

    WITH snapshot_prices AS (
        -- Last snapshot price per ticker, spread <= 20c, before settlement
        SELECT DISTINCT ON (sm.ticker)
            sm.ticker,
            sm.event_ticker,
            sm.title,
            sm.result,
            sm.settled_at,
            ks.yes_bid::numeric / 100.0 AS bid,
            ks.yes_ask::numeric / 100.0 AS ask,
            (ks.yes_bid + ks.yes_ask)::numeric / 200.0 AS last_price,
            ks.timestamp AS price_observed_at,
            'snapshot' AS price_source
        FROM kalshi_settled_markets sm
        JOIN kalshi_snapshots ks ON ks.ticker = sm.ticker
        WHERE sm.result IS NOT NULL
          AND sm.settled_at != ''
          AND ks.timestamp < sm.settled_at::timestamptz
          AND (ks.yes_ask - ks.yes_bid) <= 20  -- spread filter: <=20 cents
        ORDER BY sm.ticker, ks.timestamp DESC
    ),

    finfeed_prices AS (
        -- Last FinFeed price per ticker, before settlement
        SELECT DISTINCT ON (sm.ticker)
            sm.ticker,
            sm.event_ticker,
            sm.title,
            sm.result,
            sm.settled_at,
            NULL::numeric AS bid,
            NULL::numeric AS ask,
            ff.price_close AS last_price,
            ff.date::timestamp AS price_observed_at,
            'finfeed' AS price_source
        FROM kalshi_settled_markets sm
        JOIN finfeed_ohlcv ff ON ff.market_id = sm.ticker || '_YES'
        WHERE sm.result IS NOT NULL
          AND sm.settled_at != ''
          AND ff.date < sm.settled_at::date
        ORDER BY sm.ticker, ff.date DESC
    ),

    hourly_prices AS (
        -- Last hourly candle price per ticker, spread <= 20c, before settlement
        -- Note: hourly candles are already in dollars (numeric(6,4)), not cents
        SELECT DISTINCT ON (sm.ticker)
            sm.ticker,
            sm.event_ticker,
            sm.title,
            sm.result,
            sm.settled_at,
            hc.yes_bid_close AS bid,
            hc.yes_ask_close AS ask,
            ((hc.yes_bid_close + hc.yes_ask_close) / 2.0) AS last_price,
            hc.period_end AS price_observed_at,
            'hourly_candle' AS price_source
        FROM kalshi_settled_markets sm
        JOIN kalshi_hourly_candles hc ON hc.ticker = sm.ticker
        WHERE sm.result IS NOT NULL AND sm.settled_at != ''
          AND hc.period_end < sm.settled_at::timestamptz
          AND hc.yes_bid_close > 0 AND hc.yes_ask_close > 0
          AND (hc.yes_ask_close - hc.yes_bid_close) <= 0.20
        ORDER BY sm.ticker, hc.period_end DESC
    ),

    daily_candle_prices AS (
        -- Last daily candle price per ticker, spread <= 20c, before settlement
        -- Note: daily candlesticks are in cents (integer), so divide by 100
        SELECT DISTINCT ON (sm.ticker)
            sm.ticker,
            sm.event_ticker,
            sm.title,
            sm.result,
            sm.settled_at,
            dc.yes_bid_close::numeric / 100.0 AS bid,
            dc.yes_ask_close::numeric / 100.0 AS ask,
            ((dc.yes_bid_close + dc.yes_ask_close)::numeric / 200.0) AS last_price,
            dc.period_end AS price_observed_at,
            'daily_candle' AS price_source
        FROM kalshi_settled_markets sm
        JOIN kalshi_candlesticks dc ON dc.ticker = sm.ticker
        WHERE sm.result IS NOT NULL AND sm.settled_at != ''
          AND dc.period_end < sm.settled_at::timestamptz
          AND dc.yes_bid_close > 0 AND dc.yes_ask_close > 0
          AND (dc.yes_ask_close - dc.yes_bid_close) <= 20
        ORDER BY sm.ticker, dc.period_end DESC
    ),

    -- Combine all sources, prefer the most recent observation per ticker
    all_prices AS (
        SELECT * FROM snapshot_prices
        UNION ALL
        SELECT * FROM finfeed_prices
        UNION ALL
        SELECT * FROM hourly_prices
        UNION ALL
        SELECT * FROM daily_candle_prices
    ),

    best_prices AS (
        SELECT DISTINCT ON (ticker) *
        FROM all_prices
        ORDER BY ticker, price_observed_at DESC
    ),

    -- Classification with 3-level fallback
    classified AS (
        SELECT
            bp.*,
            COALESCE(mc1.generating_process, mc2.generating_process, mc3.generating_process) AS generating_process,
            COALESCE(mc1.topic, mc2.topic, mc3.topic) AS topic,
            COALESCE(mc1.payoff_type, mc2.payoff_type, mc3.payoff_type) AS payoff_type,
            COALESCE(
                mc1.has_external_benchmark,
                mc2.has_external_benchmark,
                mc3.has_external_benchmark
            ) AS has_external_benchmark,
            COALESCE(
                mc1.benchmark_source,
                mc2.benchmark_source,
                mc3.benchmark_source
            ) AS benchmark_source,
            COALESCE(
                mc1.description,
                mc2.description,
                mc3.description
            ) AS classification_description
        FROM best_prices bp
        LEFT JOIN kalshi_events ke ON ke.event_ticker = bp.event_ticker
        LEFT JOIN market_classifications mc1 ON mc1.series_ticker = ke.series_ticker
        LEFT JOIN market_classifications mc2
            ON mc2.series_ticker = 'KX' || split_part(bp.event_ticker, '-', 1)
        LEFT JOIN market_classifications mc3
            ON mc3.series_ticker = split_part(bp.event_ticker, '-', 1)
    )

    SELECT
        c.ticker,
        c.event_ticker,
        c.title,
        c.result,
        c.settled_at,
        c.bid,
        c.ask,
        c.last_price,
        c.price_observed_at,
        c.price_source,
        EXTRACT(EPOCH FROM (c.settled_at::timestamptz - c.price_observed_at)) / 3600.0 AS hours_to_settlement,
        c.generating_process,
        c.topic,
        c.payoff_type,
        c.has_external_benchmark,
        c.benchmark_source,
        c.classification_description,
        se.category AS event_category,
        se.title AS event_title
    FROM classified c
    LEFT JOIN kalshi_settled_events se ON se.event_ticker = c.event_ticker
    """

    try:
        cur.execute(build_sql)
        conn.commit()

        # Count rows in the new table
        cur.execute("SELECT COUNT(*) FROM settled_with_prices_new")
        row_count = _scalar(cur)
        result["rows"] = row_count
        log(f"Built settled_with_prices_new: {row_count:,} rows")

        # Add indexes on the new table before swapping
        # process_category index removed — use generating_process and topic indexes instead
        cur.execute("CREATE INDEX idx_swp_new_generating_process ON settled_with_prices_new (generating_process)")
        cur.execute("CREATE INDEX idx_swp_new_topic ON settled_with_prices_new (topic)")
        cur.execute("CREATE INDEX idx_swp_new_event_category ON settled_with_prices_new (event_category)")
        cur.execute("CREATE INDEX idx_swp_new_result ON settled_with_prices_new (result)")
        cur.execute("CREATE INDEX idx_swp_new_price_source ON settled_with_prices_new (price_source)")
        conn.commit()
        log("Indexes created on new table.")

        # Atomic swap: drop old + rename new in a single transaction
        cur.execute("DROP TABLE IF EXISTS settled_with_prices")
        cur.execute("ALTER TABLE settled_with_prices_new RENAME TO settled_with_prices")
        # Rename indexes to match the final table name
        # process_category index removed
        cur.execute("ALTER INDEX IF EXISTS idx_swp_new_generating_process RENAME TO idx_swp_generating_process")
        cur.execute("ALTER INDEX IF EXISTS idx_swp_new_topic RENAME TO idx_swp_topic")
        cur.execute("ALTER INDEX IF EXISTS idx_swp_new_event_category RENAME TO idx_swp_event_category")
        cur.execute("ALTER INDEX IF EXISTS idx_swp_new_result RENAME TO idx_swp_result")
        cur.execute("ALTER INDEX IF EXISTS idx_swp_new_price_source RENAME TO idx_swp_price_source")
        conn.commit()
        log("Atomic swap complete: settled_with_prices replaced.")

        # Summary breakdown
        cur.execute("""
            SELECT price_source, COUNT(*), AVG(last_price)::numeric(5,3)
            FROM settled_with_prices
            GROUP BY price_source
        """)
        for row in cur.fetchall():
            log(f"  {row[0]}: {row[1]:,} rows (avg price: {row[2]})")

        cur.execute("""
            SELECT generating_process, topic, COUNT(*)
            FROM settled_with_prices
            WHERE generating_process IS NOT NULL
            GROUP BY generating_process, topic
            ORDER BY COUNT(*) DESC
        """)
        rows = cur.fetchall()
        if rows:
            log("\n  By classification:")
            for row in rows:
                log(f"    {row[0]:<25} {row[1]:<25}: {row[2]:,}")

        cur.execute("""
            SELECT COUNT(*) FROM settled_with_prices
            WHERE generating_process IS NULL
        """)
        unclassified = _scalar(cur)
        if unclassified:
            log(f"\n  Unclassified: {unclassified:,}")

        cur.execute("""
            SELECT result, COUNT(*)
            FROM settled_with_prices
            GROUP BY result ORDER BY COUNT(*) DESC
        """)
        log("\n  By result:")
        for row in cur.fetchall():
            log(f"    {row[0] or 'NULL':>5}: {row[1]:,}")

    except Exception as e:
        log(f"ERROR building settled_with_prices: {e}")
        traceback.print_exc()
        result["error"] = str(e)
        conn.rollback()

    return result


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------

def show_status():
    """Show current state of the pipeline's data."""
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    cur = conn.cursor()

    print(f"\n{'=' * 60}")
    print(f"Weekly Pipeline Status — {datetime.now().isoformat()}")
    print('=' * 60)

    # --- Classifications ---
    print(f"\n--- Classifications ---")
    cur.execute("SELECT COUNT(DISTINCT e.series_ticker) FROM kalshi_events e WHERE e.series_ticker IS NOT NULL")
    total_series = _scalar(cur)
    cur.execute("SELECT COUNT(*) FROM market_classifications")
    classified = _scalar(cur)
    cur.execute("SELECT COUNT(*) FROM market_classifications WHERE needs_review = true")
    needs_review = _scalar(cur)
    print(f"  Series in DB:    {total_series:,}")
    print(f"  Classified:      {classified:,}")
    print(f"  Unclassified:    {total_series - classified:,}")
    print(f"  Needs review:    {needs_review:,}")

    cur.execute("""
        SELECT generating_process, COUNT(*)
        FROM market_classifications
        WHERE generating_process IS NOT NULL
        GROUP BY generating_process ORDER BY COUNT(*) DESC
    """)
    print(f"\n  By generating_process:")
    for row in cur.fetchall():
        print(f"    {row[0]:<25}: {row[1]:,}")

    # --- Settled Markets ---
    print(f"\n--- Settled Markets ---")
    cur.execute("SELECT COUNT(*) FROM kalshi_settled_markets")
    total_sm = _scalar(cur)
    cur.execute("SELECT COUNT(*) FROM kalshi_settled_markets WHERE result IS NOT NULL")
    with_result = _scalar(cur)
    cur.execute("SELECT COUNT(*) FROM kalshi_settled_events")
    total_se = _scalar(cur)
    print(f"  Settled markets: {total_sm:,}")
    print(f"  With result:     {with_result:,}")
    print(f"  Settled events:  {total_se:,}")

    cur.execute("""
        SELECT category, COUNT(*)
        FROM kalshi_settled_events
        WHERE category IS NOT NULL AND category != ''
        GROUP BY category ORDER BY COUNT(*) DESC
    """)
    print(f"\n  Events by category:")
    for row in cur.fetchall():
        print(f"    {row[0]:<20}: {row[1]:,}")

    cur.execute("""
        SELECT COUNT(*) FROM kalshi_settled_events
        WHERE category IS NULL OR category = ''
    """)
    uncategorized = _scalar(cur)
    print(f"    {'(uncategorized)':<20}: {uncategorized:,}")

    # --- Derived Table ---
    print(f"\n--- Derived Tables ---")
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'prediction_markets' AND table_name = 'settled_with_prices'
        )
    """)
    swp_exists = bool(_scalar(cur))

    if swp_exists:
        cur.execute("SELECT COUNT(*) FROM settled_with_prices")
        swp_count = _scalar(cur)
        print(f"  settled_with_prices: {swp_count:,} rows")

        cur.execute("""
            SELECT price_source, COUNT(*)
            FROM settled_with_prices
            GROUP BY price_source ORDER BY COUNT(*) DESC
        """)
        for row in cur.fetchall():
            print(f"    {row[0]}: {row[1]:,}")

        cur.execute("""
            SELECT generating_process, COUNT(*)
            FROM settled_with_prices
            WHERE generating_process IS NOT NULL
            GROUP BY generating_process ORDER BY COUNT(*) DESC
            LIMIT 8
        """)
        rows = cur.fetchall()
        if rows:
            print(f"\n  By generating_process:")
            for row in rows:
                print(f"    {row[0]:<25}: {row[1]:,}")

        cur.execute("SELECT COUNT(*) FROM settled_with_prices WHERE generating_process IS NULL")
        uc = _scalar(cur)
        if uc:
            print(f"    {'(unclassified)':<25}: {uc:,}")
    else:
        print(f"  settled_with_prices: NOT BUILT (run --step materialize)")

    # --- Price sources ---
    print(f"\n--- Price Data ---")
    cur.execute("SELECT COUNT(*) FROM kalshi_snapshots")
    snap_count = _scalar(cur)
    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM kalshi_snapshots")
    snap_range = cur.fetchone()
    print(f"  Kalshi snapshots:  {snap_count:,}")
    if snap_range and snap_range[0]:
        print(f"    Range: {str(snap_range[0])[:10]} to {str(snap_range[1])[:10]}")

    cur.execute("SELECT COUNT(*) FROM finfeed_ohlcv")
    ff_count = _scalar(cur)
    cur.execute("SELECT MIN(date), MAX(date) FROM finfeed_ohlcv")
    ff_range = cur.fetchone()
    print(f"  FinFeed OHLCV:     {ff_count:,}")
    if ff_range and ff_range[0]:
        print(f"    Range: {ff_range[0]} to {ff_range[1]}")

    print()
    conn.close()


# ---------------------------------------------------------------------------
# Main pipeline orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(skip_scout: bool = False):
    """Run the full pipeline, continuing past failures."""
    log("Starting weekly pipeline")

    conn = get_connection()
    errors = []
    results = {}

    # Step 1: Classify
    try:
        results["classify"] = step_classify(conn, skip_scout=skip_scout)
        if results["classify"].get("error"):
            errors.append(f"Step 1 (classify): {results['classify']['error']}")
    except Exception as e:
        log(f"STEP 1 FAILED: {e}")
        traceback.print_exc()
        errors.append(f"Step 1 (classify): {e}")
        conn.rollback()  # Reset aborted transaction so subsequent steps can use conn

    # Step 2: Download settled
    try:
        results["settled"] = step_settled(conn)
        if results["settled"].get("error"):
            errors.append(f"Step 2 (settled): {results['settled']['error']}")
    except Exception as e:
        log(f"STEP 2 FAILED: {e}")
        traceback.print_exc()
        errors.append(f"Step 2 (settled): {e}")
        conn.rollback()  # Reset aborted transaction so subsequent steps can use conn

    # Step 3: Fill categories
    try:
        results["categories"] = step_categories(conn)
        if results["categories"].get("error"):
            errors.append(f"Step 3 (categories): {results['categories']['error']}")
    except Exception as e:
        log(f"STEP 3 FAILED: {e}")
        traceback.print_exc()
        errors.append(f"Step 3 (categories): {e}")
        conn.rollback()  # Reset aborted transaction so subsequent steps can use conn

    # Step 4: Rebuild materialized table
    try:
        results["materialize"] = step_materialize(conn)
        if results["materialize"].get("error"):
            errors.append(f"Step 4 (materialize): {results['materialize']['error']}")
    except Exception as e:
        log(f"STEP 4 FAILED: {e}")
        traceback.print_exc()
        errors.append(f"Step 4 (materialize): {e}")
        conn.rollback()

    # Summary
    print(f"\n{'=' * 60}")
    log("Pipeline complete")
    print('=' * 60)

    if results.get("classify"):
        log(f"  Classify: {results['classify'].get('new_classifications', 0)} new classifications")
    if results.get("settled"):
        r = results["settled"]
        ps = r.get("per_series", {})
        ep = r.get("events_endpoint", {})
        log(f"  Settled: per-series={ps.get('markets', 0):,} markets, "
            f"events-endpoint={ep.get('markets', 0):,} markets")
    if results.get("categories"):
        log(f"  Categories: {results['categories'].get('updated', 0)} events updated")
    if results.get("materialize"):
        log(f"  Materialize: {results['materialize'].get('rows', 0):,} rows in settled_with_prices")

    if errors:
        log(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            log(f"    - {e}")
        conn.close()
        return 1

    conn.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Weekly data pipeline for prediction markets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps:
  classify    — Classify new series via market_scout (LLM)
  settled     — Download settled markets (per-series + events endpoint)
  categories  — Map uncategorized events to classifications
  materialize — Rebuild settled_with_prices derived table

Examples:
  %(prog)s                      Full pipeline (all 4 steps)
  %(prog)s --step settled       Just download settled markets
  %(prog)s --step materialize   Just rebuild the derived table
  %(prog)s --skip-scout         Skip LLM classification
  %(prog)s --status             Show current data state
        """,
    )

    parser.add_argument("--step", choices=["classify", "settled", "categories", "materialize"],
                        help="Run a single step instead of the full pipeline")
    parser.add_argument("--status", action="store_true",
                        help="Show current pipeline state and exit")
    parser.add_argument("--skip-scout", action="store_true",
                        help="Skip LLM classification")

    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.step:
        conn = get_connection()
        try:
            if args.step == "classify":
                step_classify(conn, skip_scout=args.skip_scout)
            elif args.step == "settled":
                step_settled(conn)
            elif args.step == "categories":
                step_categories(conn)
            elif args.step == "materialize":
                step_materialize(conn)
        finally:
            conn.close()
        return

    # Full pipeline
    exit_code = run_pipeline(skip_scout=args.skip_scout)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
