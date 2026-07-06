#!/usr/bin/env python3
"""
Prediction Markets SQLite -> PostgreSQL Migration

Creates a `prediction_markets` schema in the claude_hub PostgreSQL database
and bulk-loads all existing SQLite data into it.

Phase 1: Schema creation + bulk load script
Phase 2 (separate): Collector updates

Tables migrated:
  kalshi.db       -> kalshi_events, kalshi_markets, kalshi_snapshots,
                     kalshi_candlesticks, kalshi_settled_events, kalshi_settled_markets,
                     kalshi_historical_candles
  polymarket.db   -> polymarket_markets, polymarket_snapshots
  predictit.db    -> predictit_markets, predictit_contracts, predictit_snapshots
  cboe.db         -> cboe_snapshots, cboe_options
  fred.db         -> fred_series, fred_observations
  coingecko.db    -> coingecko_series, coingecko_observations, coingecko_market_data
  finfeed/ohlcv.db -> finfeed_ohlcv, finfeed_download_log

Usage:
    python migrate_to_postgres.py --create-schema     # Create tables only
    python migrate_to_postgres.py --migrate            # Create schema + bulk load
    python migrate_to_postgres.py --migrate --table kalshi_snapshots  # Migrate one table
    python migrate_to_postgres.py --status             # Show migration status
    python migrate_to_postgres.py --drop-schema        # Drop prediction_markets schema (DANGER)

Environment:
    CLAUDE_HUB_PG_DSN  PostgreSQL connection string (required, from env or ~/.env)
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COLLECTORS_DIR = Path(__file__).parent
DATA_DIR = COLLECTORS_DIR / "data"
FINFEED_DIR = COLLECTORS_DIR.parent / "data" / "finfeed"

SQLITE_DBS = {
    "kalshi":     DATA_DIR / "kalshi.db",
    "polymarket": DATA_DIR / "polymarket.db",
    "predictit":  DATA_DIR / "predictit.db",
    "cboe":       DATA_DIR / "cboe.db",
    "fred":       DATA_DIR / "fred.db",
    "coingecko":  DATA_DIR / "coingecko.db",
    "finfeed":    FINFEED_DIR / "ohlcv.db",
}

BATCH_SIZE = 10_000

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_DDL = """
-- =========================================================================
-- prediction_markets schema
-- =========================================================================

CREATE SCHEMA IF NOT EXISTS prediction_markets;

-- -------------------------------------------------------------------------
-- Kalshi
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_events (
    event_ticker    TEXT PRIMARY KEY,
    title           TEXT,
    category        TEXT,
    series_ticker   TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_markets (
    ticker          TEXT PRIMARY KEY,
    event_ticker    TEXT REFERENCES prediction_markets.kalshi_events(event_ticker),
    title           TEXT,
    status          TEXT,
    close_time      TEXT,
    volume          INTEGER,
    open_interest   INTEGER
);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL REFERENCES prediction_markets.kalshi_markets(ticker),
    timestamp       TIMESTAMPTZ NOT NULL,
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    no_bid          INTEGER,
    no_ask          INTEGER,
    spread_bps      DOUBLE PRECISION,
    yes_bid_depth   INTEGER,
    yes_ask_depth   INTEGER,
    volume          INTEGER,
    open_interest   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_kalshi_snap_ts
    ON prediction_markets.kalshi_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_kalshi_snap_ticker_ts
    ON prediction_markets.kalshi_snapshots (ticker, timestamp);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_candlesticks (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    yes_bid_open    INTEGER,
    yes_bid_close   INTEGER,
    yes_ask_open    INTEGER,
    yes_ask_close   INTEGER,
    volume          INTEGER,
    open_interest   INTEGER,
    UNIQUE (ticker, period_end)
);

CREATE INDEX IF NOT EXISTS idx_kalshi_candle_ticker
    ON prediction_markets.kalshi_candlesticks (ticker, period_end);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_settled_events (
    event_ticker    TEXT PRIMARY KEY,
    title           TEXT,
    category        TEXT,
    settled_at      TEXT,
    num_markets     INTEGER
);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_settled_markets (
    ticker          TEXT PRIMARY KEY,
    event_ticker    TEXT REFERENCES prediction_markets.kalshi_settled_events(event_ticker),
    title           TEXT,
    result          TEXT,
    volume          INTEGER,
    settled_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_kalshi_settled_event
    ON prediction_markets.kalshi_settled_markets (event_ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_settled_result
    ON prediction_markets.kalshi_settled_markets (result);

CREATE TABLE IF NOT EXISTS prediction_markets.kalshi_historical_candles (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    open_price      DOUBLE PRECISION,
    close_price     DOUBLE PRECISION,
    high_price      DOUBLE PRECISION,
    low_price       DOUBLE PRECISION,
    volume          INTEGER,
    UNIQUE (ticker, period_end)
);

-- -------------------------------------------------------------------------
-- Polymarket
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.polymarket_markets (
    market_id       TEXT PRIMARY KEY,
    token_id        TEXT NOT NULL,
    question        TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    end_date        TEXT,
    volume          DOUBLE PRECISION,
    liquidity       DOUBLE PRECISION,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS prediction_markets.polymarket_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL REFERENCES prediction_markets.polymarket_markets(market_id),
    timestamp       TIMESTAMPTZ NOT NULL,
    best_bid        DOUBLE PRECISION,
    best_ask        DOUBLE PRECISION,
    mid_price       DOUBLE PRECISION,
    spread_bps      DOUBLE PRECISION,
    bid_depth       DOUBLE PRECISION,
    ask_depth       DOUBLE PRECISION,
    volume_24h      DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_poly_snap_ts
    ON prediction_markets.polymarket_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_poly_snap_market_ts
    ON prediction_markets.polymarket_snapshots (market_id, timestamp);

-- -------------------------------------------------------------------------
-- PredictIt
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.predictit_markets (
    market_id       INTEGER PRIMARY KEY,
    name            TEXT,
    short_name      TEXT,
    url             TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS prediction_markets.predictit_contracts (
    contract_id     INTEGER PRIMARY KEY,
    market_id       INTEGER REFERENCES prediction_markets.predictit_markets(market_id),
    name            TEXT,
    short_name      TEXT
);

CREATE TABLE IF NOT EXISTS prediction_markets.predictit_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    contract_id     INTEGER NOT NULL REFERENCES prediction_markets.predictit_contracts(contract_id),
    timestamp       TIMESTAMPTZ NOT NULL,
    last_trade_price    DOUBLE PRECISION,
    best_buy_yes        DOUBLE PRECISION,
    best_sell_yes       DOUBLE PRECISION,
    best_buy_no         DOUBLE PRECISION,
    best_sell_no        DOUBLE PRECISION,
    last_close_price    DOUBLE PRECISION,
    spread_bps          DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_predictit_snap_ts
    ON prediction_markets.predictit_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_predictit_snap_contract_ts
    ON prediction_markets.predictit_snapshots (contract_id, timestamp);

-- -------------------------------------------------------------------------
-- CBOE Options
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.cboe_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL,
    spot_price      DOUBLE PRECISION,
    data_json       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cboe_snap_symbol_ts
    ON prediction_markets.cboe_snapshots (symbol, fetched_at);

CREATE TABLE IF NOT EXISTS prediction_markets.cboe_options (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_id     BIGINT REFERENCES prediction_markets.cboe_snapshots(id),
    option_symbol   TEXT,
    expiry          DATE,
    strike          DOUBLE PRECISION,
    option_type     TEXT,
    bid             DOUBLE PRECISION,
    ask             DOUBLE PRECISION,
    last_price      DOUBLE PRECISION,
    volume          INTEGER,
    open_interest   INTEGER,
    iv              DOUBLE PRECISION,
    delta           DOUBLE PRECISION,
    gamma           DOUBLE PRECISION,
    theta           DOUBLE PRECISION,
    vega            DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_cboe_opt_expiry
    ON prediction_markets.cboe_options (expiry, strike);
CREATE INDEX IF NOT EXISTS idx_cboe_opt_snapshot
    ON prediction_markets.cboe_options (snapshot_id);

-- -------------------------------------------------------------------------
-- FRED
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.fred_series (
    series_id       TEXT PRIMARY KEY,
    description     TEXT,
    frequency       TEXT,
    units           TEXT,
    last_updated    TEXT,
    category        TEXT
);

CREATE TABLE IF NOT EXISTS prediction_markets.fred_observations (
    series_id       TEXT NOT NULL REFERENCES prediction_markets.fred_series(series_id),
    date            DATE NOT NULL,
    value           DOUBLE PRECISION,
    PRIMARY KEY (series_id, date)
);

CREATE INDEX IF NOT EXISTS idx_fred_obs_date
    ON prediction_markets.fred_observations (date);
CREATE INDEX IF NOT EXISTS idx_fred_obs_series_date
    ON prediction_markets.fred_observations (series_id, date);

-- -------------------------------------------------------------------------
-- CoinGecko
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.coingecko_series (
    series_id       TEXT PRIMARY KEY,
    description     TEXT,
    frequency       TEXT,
    units           TEXT,
    last_updated    TEXT,
    symbol          TEXT,
    kalshi_series   TEXT
);

CREATE TABLE IF NOT EXISTS prediction_markets.coingecko_observations (
    series_id       TEXT NOT NULL REFERENCES prediction_markets.coingecko_series(series_id),
    date            DATE NOT NULL,
    value           DOUBLE PRECISION,
    PRIMARY KEY (series_id, date)
);

CREATE INDEX IF NOT EXISTS idx_cg_obs_date
    ON prediction_markets.coingecko_observations (date);
CREATE INDEX IF NOT EXISTS idx_cg_obs_series_date
    ON prediction_markets.coingecko_observations (series_id, date);

CREATE TABLE IF NOT EXISTS prediction_markets.coingecko_market_data (
    series_id       TEXT NOT NULL REFERENCES prediction_markets.coingecko_series(series_id),
    date            DATE NOT NULL,
    price           DOUBLE PRECISION,
    market_cap      DOUBLE PRECISION,
    total_volume    DOUBLE PRECISION,
    PRIMARY KEY (series_id, date)
);

CREATE INDEX IF NOT EXISTS idx_cg_mkt_series_date
    ON prediction_markets.coingecko_market_data (series_id, date);

-- -------------------------------------------------------------------------
-- FinFeed OHLCV
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prediction_markets.finfeed_ohlcv (
    exchange            TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    date                DATE NOT NULL,
    time_period_start   TEXT,
    time_period_end     TEXT,
    time_open           TEXT,
    time_close          TEXT,
    price_open          DOUBLE PRECISION,
    price_high          DOUBLE PRECISION,
    price_low           DOUBLE PRECISION,
    price_close         DOUBLE PRECISION,
    volume_traded       DOUBLE PRECISION,
    trades_count        INTEGER,
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (exchange, market_id, date)
);

CREATE INDEX IF NOT EXISTS idx_finfeed_ohlcv_exchange_date
    ON prediction_markets.finfeed_ohlcv (exchange, date);
CREATE INDEX IF NOT EXISTS idx_finfeed_ohlcv_market
    ON prediction_markets.finfeed_ohlcv (market_id);

CREATE TABLE IF NOT EXISTS prediction_markets.finfeed_download_log (
    exchange        TEXT NOT NULL,
    date            DATE NOT NULL,
    records_count   INTEGER,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (exchange, date)
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_pg_dsn() -> str:
    """Get PostgreSQL DSN from environment or ~/.env file."""
    dsn = os.environ.get("CLAUDE_HUB_PG_DSN")
    if dsn:
        return dsn
    # Try loading from ~/.env
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("CLAUDE_HUB_PG_DSN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("CLAUDE_HUB_PG_DSN not set")


def pg_connect():
    """Connect to PostgreSQL."""
    dsn = get_pg_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def sqlite_connect(db_path: Path):
    """Connect to a SQLite database if it exists."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(pg_conn):
    """Create the prediction_markets schema and all tables."""
    print(f"[{datetime.now().isoformat()}] Creating prediction_markets schema...")
    cur = pg_conn.cursor()
    cur.execute(SCHEMA_DDL)
    pg_conn.commit()
    print("  Schema created successfully.")


def drop_schema(pg_conn):
    """Drop the prediction_markets schema (DANGER)."""
    cur = pg_conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS prediction_markets CASCADE")
    pg_conn.commit()
    print("  Schema dropped.")


# ---------------------------------------------------------------------------
# Bulk load helpers
# ---------------------------------------------------------------------------

def bulk_insert(pg_conn, table: str, columns: list[str], rows,
                conflict_clause: str = "", label: str = ""):
    """Batch-insert rows into a Postgres table using execute_values.

    Args:
        pg_conn: psycopg2 connection
        table: fully qualified table name (e.g., prediction_markets.kalshi_events)
        columns: list of column names
        rows: iterable of tuples
        conflict_clause: ON CONFLICT clause (e.g., "ON CONFLICT DO NOTHING")
        label: label for progress reporting
    """
    cur = pg_conn.cursor()
    col_str = ", ".join(columns)
    template = "(" + ", ".join(["%s"] * len(columns)) + ")"
    sql = f"INSERT INTO {table} ({col_str}) VALUES %s {conflict_clause}"

    batch = []
    total = 0
    start_time = time.time()

    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(cur, sql, batch, template=template,
                                           page_size=BATCH_SIZE)
            pg_conn.commit()
            total += len(batch)
            elapsed = time.time() - start_time
            rate = total / elapsed if elapsed > 0 else 0
            print(f"\r    {label}: {total:,} rows ({rate:,.0f} rows/s)", end="", flush=True)
            batch = []

    if batch:
        psycopg2.extras.execute_values(cur, sql, batch, template=template,
                                       page_size=BATCH_SIZE)
        pg_conn.commit()
        total += len(batch)

    elapsed = time.time() - start_time
    rate = total / elapsed if elapsed > 0 else 0
    print(f"\r    {label}: {total:,} rows ({rate:,.0f} rows/s) - done in {elapsed:.1f}s")
    return total


def sqlite_iter(sqlite_conn, query: str, params=None):
    """Iterate over SQLite query results as tuples."""
    cur = sqlite_conn.cursor()
    cur.execute(query, params or [])
    while True:
        rows = cur.fetchmany(BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            yield tuple(row)


# ---------------------------------------------------------------------------
# Per-source migration functions
# ---------------------------------------------------------------------------

def migrate_kalshi(pg_conn):
    """Migrate all Kalshi tables."""
    db = sqlite_connect(SQLITE_DBS["kalshi"])
    if not db:
        print("  kalshi.db not found, skipping.")
        return

    print(f"\n[Kalshi] Migrating from {SQLITE_DBS['kalshi']}")

    # events
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_events",
        ["event_ticker", "title", "category", "series_ticker", "added_at"],
        sqlite_iter(db, "SELECT event_ticker, title, category, series_ticker, added_at FROM events"),
        conflict_clause="ON CONFLICT (event_ticker) DO NOTHING",
        label="kalshi_events",
    )

    # markets
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_markets",
        ["ticker", "event_ticker", "title", "status", "close_time", "volume", "open_interest"],
        sqlite_iter(db, "SELECT ticker, event_ticker, title, status, close_time, volume, open_interest FROM markets"),
        conflict_clause="ON CONFLICT (ticker) DO NOTHING",
        label="kalshi_markets",
    )

    # settled_events (load BEFORE settled_markets due to FK)
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_settled_events",
        ["event_ticker", "title", "category", "settled_at", "num_markets"],
        sqlite_iter(db, "SELECT event_ticker, title, category, settled_at, num_markets FROM settled_events"),
        conflict_clause="ON CONFLICT (event_ticker) DO NOTHING",
        label="kalshi_settled_events",
    )

    # settled_markets
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_settled_markets",
        ["ticker", "event_ticker", "title", "result", "volume", "settled_at"],
        sqlite_iter(db, "SELECT ticker, event_ticker, title, result, volume, settled_at FROM settled_markets"),
        conflict_clause="ON CONFLICT (ticker) DO NOTHING",
        label="kalshi_settled_markets",
    )

    # snapshots (BIG: ~6.6M rows)
    # Note: We skip the SQLite autoincrement `id` and let Postgres BIGSERIAL generate new ids.
    # The original SQLite ids are not referenced by FKs from other tables.
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_snapshots",
        ["ticker", "timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask",
         "spread_bps", "yes_bid_depth", "yes_ask_depth", "volume", "open_interest"],
        sqlite_iter(db, """
            SELECT ticker, timestamp, yes_bid, yes_ask, no_bid, no_ask,
                   spread_bps, yes_bid_depth, yes_ask_depth, volume, open_interest
            FROM snapshots ORDER BY id
        """),
        conflict_clause="ON CONFLICT DO NOTHING",
        label="kalshi_snapshots",
    )

    # candlesticks
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_candlesticks",
        ["ticker", "period_end", "yes_bid_open", "yes_bid_close",
         "yes_ask_open", "yes_ask_close", "volume", "open_interest"],
        sqlite_iter(db, """
            SELECT ticker, period_end, yes_bid_open, yes_bid_close,
                   yes_ask_open, yes_ask_close, volume, open_interest
            FROM candlesticks
        """),
        conflict_clause="ON CONFLICT (ticker, period_end) DO NOTHING",
        label="kalshi_candlesticks",
    )

    # historical_candles
    bulk_insert(
        pg_conn, "prediction_markets.kalshi_historical_candles",
        ["ticker", "period_end", "open_price", "close_price",
         "high_price", "low_price", "volume"],
        sqlite_iter(db, """
            SELECT ticker, period_end, open_price, close_price,
                   high_price, low_price, volume
            FROM historical_candles
        """),
        conflict_clause="ON CONFLICT (ticker, period_end) DO NOTHING",
        label="kalshi_historical_candles",
    )

    db.close()


def migrate_polymarket(pg_conn):
    """Migrate Polymarket tables."""
    db = sqlite_connect(SQLITE_DBS["polymarket"])
    if not db:
        print("  polymarket.db not found, skipping.")
        return

    print(f"\n[Polymarket] Migrating from {SQLITE_DBS['polymarket']}")

    # markets
    # SQLite has is_active as INTEGER (0/1), Postgres has BOOLEAN
    def poly_markets_iter():
        for row in sqlite_iter(db, """
            SELECT market_id, token_id, question, outcome, end_date,
                   volume, liquidity, added_at, is_active
            FROM markets
        """):
            # Convert integer is_active to boolean
            r = list(row)
            r[8] = bool(r[8]) if r[8] is not None else True
            yield tuple(r)

    bulk_insert(
        pg_conn, "prediction_markets.polymarket_markets",
        ["market_id", "token_id", "question", "outcome", "end_date",
         "volume", "liquidity", "added_at", "is_active"],
        poly_markets_iter(),
        conflict_clause="ON CONFLICT (market_id) DO NOTHING",
        label="polymarket_markets",
    )

    # snapshots (BIG: ~9.3M rows)
    bulk_insert(
        pg_conn, "prediction_markets.polymarket_snapshots",
        ["market_id", "timestamp", "best_bid", "best_ask", "mid_price",
         "spread_bps", "bid_depth", "ask_depth", "volume_24h"],
        sqlite_iter(db, """
            SELECT market_id, timestamp, best_bid, best_ask, mid_price,
                   spread_bps, bid_depth, ask_depth, volume_24h
            FROM snapshots ORDER BY id
        """),
        conflict_clause="ON CONFLICT DO NOTHING",
        label="polymarket_snapshots",
    )

    db.close()


def migrate_predictit(pg_conn):
    """Migrate PredictIt tables."""
    db = sqlite_connect(SQLITE_DBS["predictit"])
    if not db:
        print("  predictit.db not found, skipping.")
        return

    print(f"\n[PredictIt] Migrating from {SQLITE_DBS['predictit']}")

    # markets
    bulk_insert(
        pg_conn, "prediction_markets.predictit_markets",
        ["market_id", "name", "short_name", "url", "added_at"],
        sqlite_iter(db, "SELECT market_id, name, short_name, url, added_at FROM markets"),
        conflict_clause="ON CONFLICT (market_id) DO NOTHING",
        label="predictit_markets",
    )

    # contracts
    bulk_insert(
        pg_conn, "prediction_markets.predictit_contracts",
        ["contract_id", "market_id", "name", "short_name"],
        sqlite_iter(db, "SELECT contract_id, market_id, name, short_name FROM contracts"),
        conflict_clause="ON CONFLICT (contract_id) DO NOTHING",
        label="predictit_contracts",
    )

    # snapshots
    bulk_insert(
        pg_conn, "prediction_markets.predictit_snapshots",
        ["contract_id", "timestamp", "last_trade_price", "best_buy_yes",
         "best_sell_yes", "best_buy_no", "best_sell_no", "last_close_price", "spread_bps"],
        sqlite_iter(db, """
            SELECT contract_id, timestamp, last_trade_price, best_buy_yes,
                   best_sell_yes, best_buy_no, best_sell_no, last_close_price, spread_bps
            FROM snapshots ORDER BY id
        """),
        conflict_clause="ON CONFLICT DO NOTHING",
        label="predictit_snapshots",
    )

    db.close()


def migrate_cboe(pg_conn):
    """Migrate CBOE tables."""
    db = sqlite_connect(SQLITE_DBS["cboe"])
    if not db:
        print("  cboe.db not found, skipping.")
        return

    print(f"\n[CBOE] Migrating from {SQLITE_DBS['cboe']}")

    # snapshots — need to map SQLite ids to Postgres ids for the FK in options
    # Since cboe is small (4 snapshots, 104K options), we can do this in memory
    cur = db.cursor()
    cur.execute("SELECT id, symbol, fetched_at, spot_price, data_json FROM snapshots ORDER BY id")
    snapshot_rows = cur.fetchall()

    pg_cur = pg_conn.cursor()
    sqlite_id_to_pg_id = {}

    for row in snapshot_rows:
        sqlite_id = row[0]
        pg_cur.execute("""
            INSERT INTO prediction_markets.cboe_snapshots
                (symbol, fetched_at, spot_price, data_json)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, (row[1], row[2], row[3], row[4]))
        result = pg_cur.fetchone()
        if result:
            sqlite_id_to_pg_id[sqlite_id] = result[0]
        else:
            # Already existed — find it by matching
            pg_cur.execute("""
                SELECT id FROM prediction_markets.cboe_snapshots
                WHERE symbol = %s AND fetched_at = %s
            """, (row[1], row[2]))
            existing = pg_cur.fetchone()
            if existing:
                sqlite_id_to_pg_id[sqlite_id] = existing[0]

    pg_conn.commit()
    print(f"    cboe_snapshots: {len(sqlite_id_to_pg_id)} rows")

    # options — remap snapshot_id
    def cboe_options_iter():
        for row in sqlite_iter(db, """
            SELECT snapshot_id, option_symbol, expiry, strike, option_type,
                   bid, ask, last_price, volume, open_interest,
                   iv, delta, gamma, theta, vega
            FROM options
        """):
            sqlite_snap_id = row[0]
            pg_snap_id = sqlite_id_to_pg_id.get(sqlite_snap_id)
            if pg_snap_id is None:
                continue
            yield (pg_snap_id,) + row[1:]

    bulk_insert(
        pg_conn, "prediction_markets.cboe_options",
        ["snapshot_id", "option_symbol", "expiry", "strike", "option_type",
         "bid", "ask", "last_price", "volume", "open_interest",
         "iv", "delta", "gamma", "theta", "vega"],
        cboe_options_iter(),
        conflict_clause="ON CONFLICT DO NOTHING",
        label="cboe_options",
    )

    db.close()


def migrate_fred(pg_conn):
    """Migrate FRED tables."""
    db = sqlite_connect(SQLITE_DBS["fred"])
    if not db:
        print("  fred.db not found, skipping.")
        return

    print(f"\n[FRED] Migrating from {SQLITE_DBS['fred']}")

    # series
    bulk_insert(
        pg_conn, "prediction_markets.fred_series",
        ["series_id", "description", "frequency", "units", "last_updated", "category"],
        sqlite_iter(db, "SELECT series_id, description, frequency, units, last_updated, category FROM series"),
        conflict_clause="ON CONFLICT (series_id) DO NOTHING",
        label="fred_series",
    )

    # observations
    bulk_insert(
        pg_conn, "prediction_markets.fred_observations",
        ["series_id", "date", "value"],
        sqlite_iter(db, "SELECT series_id, date, value FROM observations"),
        conflict_clause="ON CONFLICT (series_id, date) DO NOTHING",
        label="fred_observations",
    )

    db.close()


def migrate_coingecko(pg_conn):
    """Migrate CoinGecko tables."""
    db = sqlite_connect(SQLITE_DBS["coingecko"])
    if not db:
        print("  coingecko.db not found, skipping.")
        return

    print(f"\n[CoinGecko] Migrating from {SQLITE_DBS['coingecko']}")

    # series
    bulk_insert(
        pg_conn, "prediction_markets.coingecko_series",
        ["series_id", "description", "frequency", "units", "last_updated",
         "symbol", "kalshi_series"],
        sqlite_iter(db, """
            SELECT series_id, description, frequency, units, last_updated,
                   symbol, kalshi_series
            FROM series
        """),
        conflict_clause="ON CONFLICT (series_id) DO NOTHING",
        label="coingecko_series",
    )

    # observations
    bulk_insert(
        pg_conn, "prediction_markets.coingecko_observations",
        ["series_id", "date", "value"],
        sqlite_iter(db, "SELECT series_id, date, value FROM observations"),
        conflict_clause="ON CONFLICT (series_id, date) DO NOTHING",
        label="coingecko_observations",
    )

    # market_data
    bulk_insert(
        pg_conn, "prediction_markets.coingecko_market_data",
        ["series_id", "date", "price", "market_cap", "total_volume"],
        sqlite_iter(db, "SELECT series_id, date, price, market_cap, total_volume FROM market_data"),
        conflict_clause="ON CONFLICT (series_id, date) DO NOTHING",
        label="coingecko_market_data",
    )

    db.close()


def migrate_finfeed(pg_conn):
    """Migrate FinFeed OHLCV tables."""
    db = sqlite_connect(SQLITE_DBS["finfeed"])
    if not db:
        print("  finfeed/ohlcv.db not found, skipping.")
        return

    print(f"\n[FinFeed] Migrating from {SQLITE_DBS['finfeed']}")

    # ohlcv (BIG: ~9.5M rows)
    bulk_insert(
        pg_conn, "prediction_markets.finfeed_ohlcv",
        ["exchange", "market_id", "date", "time_period_start", "time_period_end",
         "time_open", "time_close", "price_open", "price_high", "price_low",
         "price_close", "volume_traded", "trades_count", "fetched_at"],
        sqlite_iter(db, """
            SELECT exchange, market_id, date, time_period_start, time_period_end,
                   time_open, time_close, price_open, price_high, price_low,
                   price_close, volume_traded, trades_count, fetched_at
            FROM ohlcv
        """),
        conflict_clause="ON CONFLICT (exchange, market_id, date) DO NOTHING",
        label="finfeed_ohlcv",
    )

    # download_log
    bulk_insert(
        pg_conn, "prediction_markets.finfeed_download_log",
        ["exchange", "date", "records_count", "fetched_at"],
        sqlite_iter(db, "SELECT exchange, date, records_count, fetched_at FROM download_log"),
        conflict_clause="ON CONFLICT (exchange, date) DO NOTHING",
        label="finfeed_download_log",
    )

    db.close()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

TABLES_TO_CHECK = [
    "kalshi_events", "kalshi_markets", "kalshi_snapshots",
    "kalshi_candlesticks", "kalshi_settled_events", "kalshi_settled_markets",
    "kalshi_historical_candles",
    "polymarket_markets", "polymarket_snapshots",
    "predictit_markets", "predictit_contracts", "predictit_snapshots",
    "cboe_snapshots", "cboe_options",
    "fred_series", "fred_observations",
    "coingecko_series", "coingecko_observations", "coingecko_market_data",
    "finfeed_ohlcv", "finfeed_download_log",
]


def show_status(pg_conn):
    """Show migration status — row counts for all tables."""
    cur = pg_conn.cursor()

    # Check if schema exists
    cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'prediction_markets'")
    if not cur.fetchone():
        print("  prediction_markets schema does not exist.")
        return

    print(f"\n{'Table':<45} {'Rows':>12}")
    print("-" * 60)

    total = 0
    for table in TABLES_TO_CHECK:
        fq = f"prediction_markets.{table}"
        try:
            cur.execute(f"SELECT COUNT(*) FROM {fq}")
            count = cur.fetchone()[0]
            total += count
            print(f"  {fq:<43} {count:>12,}")
        except Exception as e:
            print(f"  {fq:<43} {'ERROR':>12}  {e}")
            pg_conn.rollback()

    print("-" * 60)
    print(f"  {'Total':<43} {total:>12,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Map table names to migration functions for selective migration
TABLE_MIGRATIONS = {
    "kalshi_events": migrate_kalshi,
    "kalshi_markets": migrate_kalshi,
    "kalshi_snapshots": migrate_kalshi,
    "kalshi_candlesticks": migrate_kalshi,
    "kalshi_settled_events": migrate_kalshi,
    "kalshi_settled_markets": migrate_kalshi,
    "kalshi_historical_candles": migrate_kalshi,
    "polymarket_markets": migrate_polymarket,
    "polymarket_snapshots": migrate_polymarket,
    "predictit_markets": migrate_predictit,
    "predictit_contracts": migrate_predictit,
    "predictit_snapshots": migrate_predictit,
    "cboe_snapshots": migrate_cboe,
    "cboe_options": migrate_cboe,
    "fred_series": migrate_fred,
    "fred_observations": migrate_fred,
    "coingecko_series": migrate_coingecko,
    "coingecko_observations": migrate_coingecko,
    "coingecko_market_data": migrate_coingecko,
    "finfeed_ohlcv": migrate_finfeed,
    "finfeed_download_log": migrate_finfeed,
}

# Source-level migration functions (in dependency order)
SOURCE_MIGRATIONS = [
    ("kalshi", migrate_kalshi),
    ("polymarket", migrate_polymarket),
    ("predictit", migrate_predictit),
    ("cboe", migrate_cboe),
    ("fred", migrate_fred),
    ("coingecko", migrate_coingecko),
    ("finfeed", migrate_finfeed),
]


def main():
    parser = argparse.ArgumentParser(
        description="Migrate prediction market data from SQLite to PostgreSQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --create-schema                 Create tables only (no data)
  %(prog)s --migrate                       Create schema + bulk load all data
  %(prog)s --migrate --source kalshi       Migrate only Kalshi data
  %(prog)s --migrate --source finfeed      Migrate only FinFeed OHLCV
  %(prog)s --status                        Show row counts
  %(prog)s --drop-schema                   Drop everything (DANGER)
        """,
    )
    parser.add_argument("--create-schema", action="store_true",
                        help="Create prediction_markets schema and tables")
    parser.add_argument("--migrate", action="store_true",
                        help="Create schema + bulk load all data from SQLite")
    parser.add_argument("--source", type=str, default=None,
                        choices=["kalshi", "polymarket", "predictit", "cboe",
                                 "fred", "coingecko", "finfeed"],
                        help="Migrate only a specific data source")
    parser.add_argument("--status", action="store_true",
                        help="Show migration status (row counts)")
    parser.add_argument("--drop-schema", action="store_true",
                        help="Drop prediction_markets schema (DANGER)")

    args = parser.parse_args()

    if not any([args.create_schema, args.migrate, args.status, args.drop_schema]):
        parser.print_help()
        return

    pg_conn = pg_connect()

    try:
        if args.drop_schema:
            confirm = input("Type 'yes' to drop prediction_markets schema: ")
            if confirm.strip().lower() == "yes":
                drop_schema(pg_conn)
            else:
                print("  Aborted.")
            return

        if args.status:
            show_status(pg_conn)
            return

        if args.create_schema or args.migrate:
            create_schema(pg_conn)

        if args.migrate:
            start = time.time()

            if args.source:
                # Migrate specific source
                for name, func in SOURCE_MIGRATIONS:
                    if name == args.source:
                        func(pg_conn)
                        break
            else:
                # Migrate everything
                for name, func in SOURCE_MIGRATIONS:
                    func(pg_conn)

            elapsed = time.time() - start
            print(f"\n[{datetime.now().isoformat()}] Migration complete in {elapsed:.1f}s")

            # Show final status
            show_status(pg_conn)

    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
