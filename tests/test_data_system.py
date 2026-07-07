"""Tests for the data system (registry, ingestion framework, health checks).

Tests marked @pytest.mark.db require a live database connection.
Pure logic tests run without any external dependencies.
"""

import os
from unittest.mock import MagicMock

import pytest


# --- Pure logic tests (no DB) ---


class TestRetry:
    """Tests for data.ingestion.retry.with_retry"""

    def test_success_on_first_try(self):
        from data.ingestion.retry import with_retry

        result = with_retry(lambda: 42, max_retries=3)
        assert result == 42

    def test_retries_on_exception(self):
        from data.ingestion.retry import with_retry

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"

        result = with_retry(flaky, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        from data.ingestion.retry import with_retry, RetryExhausted

        def always_fail():
            raise ConnectionError("permanent")

        with pytest.raises(RetryExhausted) as exc_info:
            with_retry(always_fail, max_retries=2, base_delay=0.01)
        assert exc_info.value.attempts == 2
        assert "permanent" in str(exc_info.value.last_error)

    def test_non_retryable_http_raises_immediately(self):
        from data.ingestion.retry import with_retry
        import requests

        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = requests.HTTPError("Forbidden")

        call_count = 0

        def forbidden():
            nonlocal call_count
            call_count += 1
            return mock_resp

        with pytest.raises(requests.HTTPError):
            with_retry(forbidden, max_retries=3, base_delay=0.01)
        assert call_count == 1  # no retry on 403

    def test_retries_on_429(self):
        from data.ingestion.retry import with_retry
        import requests

        call_count = 0

        def rate_limited():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=requests.Response)
            if call_count < 2:
                resp.status_code = 429
                resp.text = "Rate limited"
            else:
                resp.status_code = 200
            return resp

        result = with_retry(rate_limited, max_retries=3, base_delay=0.01)
        assert result.status_code == 200
        assert call_count == 2


class TestCheckResult:
    """Tests for the anomaly check data structure."""

    def test_check_result_creation(self):
        from data.health.checks import CheckResult

        r = CheckResult("volume_positive", True, "OK")
        assert r.passed is True
        assert r.check_name == "volume_positive"

    def test_check_result_failure(self):
        from data.health.checks import CheckResult

        r = CheckResult("volume_positive", False, "5 rows with negative volume")
        assert r.passed is False
        assert "5 rows" in r.message


class TestValidateIdentifier:
    """Tests for SQL identifier validation."""

    def test_valid_table(self):
        from data.health.check import _validate_identifier

        _validate_identifier("prediction_markets.kalshi_trades")  # should not raise

    def test_valid_column(self):
        from data.health.check import _validate_identifier

        _validate_identifier("created_time")  # should not raise

    def test_rejects_injection(self):
        from data.health.check import _validate_identifier

        with pytest.raises(ValueError):
            _validate_identifier("table; DROP TABLE users")

    def test_rejects_uppercase(self):
        from data.health.check import _validate_identifier

        with pytest.raises(ValueError):
            _validate_identifier("SELECT")


class TestSplitTable:
    """Tests for schema.table splitting."""

    def test_qualified_name(self):
        from data.health.check import _split_table

        assert _split_table("prediction_markets.kalshi_trades") == (
            "prediction_markets", "kalshi_trades"
        )

    def test_unqualified_name(self):
        from data.health.check import _split_table

        assert _split_table("kalshi_trades") == (
            "prediction_markets", "kalshi_trades"
        )


# --- DB-dependent tests ---


@pytest.fixture
def db_conn():
    """Get a database connection for testing."""
    import psycopg2

    dsn = os.environ.get("CLAUDE_HUB_PG_DSN", "")
    if not dsn:
        pytest.skip("CLAUDE_HUB_PG_DSN not set")
    conn = psycopg2.connect(dsn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.mark.db
class TestRegistryDB:
    """Registry operations against a real database."""

    def test_register_and_get(self, db_conn):
        from data.registry import register_dataset, get_dataset

        register_dataset(
            dataset_id="_test_ds",
            source="test",
            storage_table="prediction_markets.kalshi_trades",
            description="Test dataset",
            provenance="Unit test",
            resolution="tick",
            natural_key=["trade_id"],
            freshness_column="created_time",
            max_stale_interval="2 hours",
            expected_coverage={"earliest": "2021-06-30"},
            conn=db_conn,
        )

        ds = get_dataset("_test_ds", db_conn)
        assert ds is not None
        assert ds.source == "test"
        assert ds.resolution == "tick"
        assert ds.natural_key == ["trade_id"]
        assert ds.expected_coverage["earliest"] == "2021-06-30"

        # Cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_markets.dataset_registry WHERE dataset_id = '_test_ds'")
        db_conn.commit()

    def test_list_datasets(self, db_conn):
        from data.registry import list_datasets

        datasets = list_datasets(db_conn)
        # Should return a list (possibly empty)
        assert isinstance(datasets, list)


@pytest.mark.db
class TestRunLoggerDB:
    """RunLogger operations against a real database."""

    def test_run_lifecycle(self, db_conn):
        from data.registry import register_dataset
        from data.ingestion.run_logger import RunLogger, get_last_run

        # Need a registered dataset for the FK
        register_dataset(
            dataset_id="_test_run",
            source="test",
            storage_table="prediction_markets.kalshi_trades",
            description="Test",
            provenance="Unit test",
            resolution="tick",
            natural_key=["trade_id"],
            freshness_column="created_time",
            conn=db_conn,
        )

        logger = RunLogger("_test_run", db_conn)
        run_id = logger.start()
        assert run_id > 0

        logger.record_progress(rows_fetched=100, rows_inserted=95)
        logger.record_progress(rows_fetched=50, rows_inserted=48)
        logger.finish("completed")

        last = get_last_run("_test_run", db_conn)
        assert last is not None
        assert last.status == "completed"
        assert last.rows_fetched == 150
        assert last.rows_inserted == 143

        # Cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_markets.ingestion_runs WHERE dataset_id = '_test_run'")
            cur.execute("DELETE FROM prediction_markets.dataset_registry WHERE dataset_id = '_test_run'")
        db_conn.commit()


@pytest.mark.db
class TestHealthCheckDB:
    """Health check operations against a real database."""

    def test_check_all_runs(self, db_conn):
        from data.health.check import check_all

        statuses = check_all(db_conn)
        assert isinstance(statuses, list)

    def test_update_cache(self, db_conn):
        from data.registry import register_dataset
        from data.health.check import update_cache, check_one

        register_dataset(
            dataset_id="_test_health",
            source="test",
            storage_table="prediction_markets.kalshi_trades",
            description="Test",
            provenance="Unit test",
            resolution="tick",
            natural_key=["trade_id"],
            freshness_column="created_time",
            max_stale_interval="2 hours",
            expected_coverage={"earliest": "2021-06-30"},
            conn=db_conn,
        )

        # Update cache for this dataset
        update_cache(db_conn)

        # Check health status
        status = check_one("_test_health", db_conn)
        assert status is not None
        # kalshi_trades has data, so it should report some freshness
        assert status.max_freshness is not None or status.health_status == "no_data"

        # Cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_markets.dataset_health_cache WHERE dataset_id = '_test_health'")
            cur.execute("DELETE FROM prediction_markets.dataset_registry WHERE dataset_id = '_test_health'")
        db_conn.commit()


@pytest.mark.db
class TestRetryHTTPError:
    """Tests that retry correctly handles HTTPError from client wrappers."""

    def test_retryable_httperror_is_retried(self):
        """429 HTTPError from fn() that does raise_for_status should retry."""
        from data.ingestion.retry import with_retry
        import requests

        call_count = 0

        def rate_limited_client():
            nonlocal call_count
            call_count += 1
            resp = requests.Response()
            resp.status_code = 429 if call_count < 3 else 200
            resp._content = b'{"ok": true}'
            resp.raise_for_status()
            return resp.json()

        result = with_retry(rate_limited_client, max_retries=3, base_delay=0.01)
        assert result == {"ok": True}
        assert call_count == 3

    def test_non_retryable_httperror_propagates(self):
        """403 HTTPError from fn() that does raise_for_status should NOT retry."""
        from data.ingestion.retry import with_retry
        import requests

        call_count = 0

        def forbidden_client():
            nonlocal call_count
            call_count += 1
            resp = requests.Response()
            resp.status_code = 403
            resp._content = b'Forbidden'
            resp.raise_for_status()

        with pytest.raises(requests.HTTPError):
            with_retry(forbidden_client, max_retries=3, base_delay=0.01)
        assert call_count == 1  # no retry

    def test_404_httperror_propagates(self):
        """404 HTTPError should NOT retry."""
        from data.ingestion.retry import with_retry
        import requests

        call_count = 0

        def not_found_client():
            nonlocal call_count
            call_count += 1
            resp = requests.Response()
            resp.status_code = 404
            resp._content = b'Not Found'
            resp.raise_for_status()

        with pytest.raises(requests.HTTPError):
            with_retry(not_found_client, max_retries=3, base_delay=0.01)
        assert call_count == 1


class TestTradesParsing:
    """Tests for collectors.kalshi.trades row mapping."""

    def test_parse_trade_normal(self):
        from collectors.kalshi.trades import _parse_trade

        t = {
            "trade_id": "abc123",
            "ticker": "KXBTCD-26APR04-T100000",
            "created_time": "2026-04-04T12:00:00Z",
            "count_fp": "5",
            "yes_price_dollars": "0.85",
            "no_price_dollars": "0.15",
            "taker_side": "yes",
        }
        row = _parse_trade(t, "historical")
        assert row is not None
        assert row[0] == "abc123"
        assert row[1] == "KXBTCD-26APR04-T100000"
        assert row[3] == 5           # count: integer
        assert row[4] == 85          # yes_price: cents
        assert row[5] == 15          # no_price: cents
        assert row[7] == "historical"

    def test_parse_trade_cents_conversion(self):
        from collectors.kalshi.trades import _parse_trade

        t = {
            "trade_id": "x", "ticker": "y", "created_time": "z",
            "yes_price_dollars": "0.03", "no_price_dollars": "0.97",
            "count_fp": "1",
        }
        row = _parse_trade(t, "live")
        assert row[4] == 3    # 0.03 -> 3 cents
        assert row[5] == 97   # 0.97 -> 97 cents

    def test_parse_trade_missing_id(self):
        from collectors.kalshi.trades import _parse_trade

        t = {"ticker": "FOO", "created_time": "2026-01-01T00:00:00Z"}
        assert _parse_trade(t, "live") is None

    def test_parse_trade_missing_ticker(self):
        from collectors.kalshi.trades import _parse_trade

        t = {"trade_id": "abc", "created_time": "2026-01-01T00:00:00Z"}
        assert _parse_trade(t, "live") is None

    def test_parse_trade_empty_strings(self):
        from collectors.kalshi.trades import _parse_trade

        t = {"trade_id": "", "ticker": "", "created_time": ""}
        assert _parse_trade(t, "live") is None

    def test_parse_trade_origin_passthrough(self):
        from collectors.kalshi.trades import _parse_trade

        t = {"trade_id": "x", "ticker": "y", "created_time": "z"}
        assert _parse_trade(t, "live")[7] == "live"
        assert _parse_trade(t, "historical")[7] == "historical"

    def test_dollars_to_cents(self):
        from collectors.kalshi.trades import _dollars_to_cents

        assert _dollars_to_cents("0.85") == 85
        assert _dollars_to_cents("0.03") == 3
        assert _dollars_to_cents("0.97") == 97
        assert _dollars_to_cents("1.00") == 100
        assert _dollars_to_cents("0") == 0


class TestFlushSplice:
    """Tests for splice-aware flush functions (DB-dependent)."""

    @pytest.fixture
    def splice_conn(self, db_conn):
        """Setup: insert a test trade, clean up after."""
        cur = db_conn.cursor()
        # Insert a live trade (prices in integer cents per spec 2.3)
        cur.execute("""
            INSERT INTO prediction_markets.kalshi_trades
                (trade_id, ticker, created_time, count, yes_price, no_price, taker_side, origin)
            VALUES ('_test_splice_1', 'TEST-TICKER', '2026-01-01T00:00:00Z',
                    1, 50, 50, 'yes', 'live')
            ON CONFLICT (trade_id) DO UPDATE SET origin = 'live',
                ticker = EXCLUDED.ticker, created_time = EXCLUDED.created_time,
                count = EXCLUDED.count, yes_price = EXCLUDED.yes_price,
                no_price = EXCLUDED.no_price, taker_side = EXCLUDED.taker_side
        """)
        db_conn.commit()
        yield db_conn
        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_trades WHERE trade_id LIKE '_test_splice_%'")
        db_conn.commit()
        cur.close()

    def test_historical_replaces_live(self, splice_conn):
        from collectors.kalshi.trades import _flush_trades_historical

        cur = splice_conn.cursor()
        buffer = [("_test_splice_1", "TEST-TICKER", "2026-01-01T00:00:00Z",
                    1, 85, 15, "yes", "historical")]
        _flush_trades_historical(cur, buffer)
        splice_conn.commit()

        cur.execute("SELECT origin, yes_price FROM prediction_markets.kalshi_trades WHERE trade_id = '_test_splice_1'")
        row = cur.fetchone()
        assert row[0] == "historical"
        assert row[1] == 85  # cents
        cur.close()

    def test_historical_does_not_replace_historical(self, splice_conn):
        from collectors.kalshi.trades import _flush_trades_historical

        cur = splice_conn.cursor()
        # First: make the existing row historical
        cur.execute("UPDATE prediction_markets.kalshi_trades SET origin = 'historical' WHERE trade_id = '_test_splice_1'")
        splice_conn.commit()

        # Now try to overwrite with different data
        buffer = [("_test_splice_1", "TEST-TICKER", "2026-01-01T00:00:00Z",
                    1, 99, 1, "historical", "historical")]
        _flush_trades_historical(cur, buffer)
        splice_conn.commit()

        cur.execute("SELECT yes_price FROM prediction_markets.kalshi_trades WHERE trade_id = '_test_splice_1'")
        row = cur.fetchone()
        # Should NOT have been overwritten — original was already historical
        assert row[0] == 50  # cents, unchanged
        cur.close()

    def test_live_does_not_replace_existing(self, splice_conn):
        from collectors.kalshi.trades import _flush_trades_live

        cur = splice_conn.cursor()
        buffer = [("_test_splice_1", "TEST-TICKER", "2026-01-01T00:00:00Z",
                    1, 99, 1, "no", "live")]
        inserted = _flush_trades_live(cur, buffer)
        splice_conn.commit()

        # Should not have inserted (conflict, DO NOTHING)
        assert inserted == 0

        cur.execute("SELECT yes_price FROM prediction_markets.kalshi_trades WHERE trade_id = '_test_splice_1'")
        row = cur.fetchone()
        assert row[0] == 50  # cents, unchanged
        cur.close()

    def test_live_inserts_new(self, splice_conn):
        from collectors.kalshi.trades import _flush_trades_live

        cur = splice_conn.cursor()
        buffer = [("_test_splice_2", "TEST-TICKER", "2026-01-02T00:00:00Z",
                    1, 75, 25, "yes", "live")]
        inserted = _flush_trades_live(cur, buffer)
        splice_conn.commit()

        assert inserted == 1
        cur.execute("SELECT origin FROM prediction_markets.kalshi_trades WHERE trade_id = '_test_splice_2'")
        assert cur.fetchone()[0] == "live"
        cur.close()


class TestRateLimiterDB:
    """RateLimiter against a real database."""

    def test_acquire_creates_row(self, db_conn):
        from data.ingestion.rate_limiter import RateLimiter

        limiter = RateLimiter("_test_source", qps=100.0, conn=db_conn)
        limiter.acquire()

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT qps_limit FROM prediction_markets.rate_limit_state WHERE source = '_test_source'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 100.0

        # Cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_markets.rate_limit_state WHERE source = '_test_source'")
        db_conn.commit()

    def test_acquire_throttles(self, db_conn):
        """Two rapid acquire() calls should be separated by at least min_interval."""
        import time
        from data.ingestion.rate_limiter import RateLimiter

        limiter = RateLimiter("_test_throttle", qps=10.0, conn=db_conn)  # 100ms interval

        start = time.time()
        limiter.acquire()
        limiter.acquire()
        elapsed = time.time() - start

        # Should have waited ~100ms between the two calls
        assert elapsed >= 0.08  # some tolerance

        # Cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_markets.rate_limit_state WHERE source = '_test_throttle'")
        db_conn.commit()


# --- Candle ingestion tests ---


class TestCandleParsing:
    """Tests for collectors.kalshi.candles row mapping."""

    def test_parse_candle_historical_format(self):
        """Historical API uses plain field names (no _dollars suffix)."""
        from collectors.kalshi.candles import _parse_candle

        c = {
            "end_period_ts": 1765602000,
            "yes_bid": {"open": "0.22", "close": "0.21", "high": "0.24", "low": "0.20"},
            "yes_ask": {"open": "0.23", "close": "0.23", "high": "0.25", "low": "0.23"},
            "price": {"open": "0.22", "close": "0.21", "high": "0.25", "low": "0.21",
                       "mean": "0.23", "previous": "0.23"},
            "volume": "13683.00",
            "open_interest": "379323.00",
        }
        row = _parse_candle("KXTEST-26-FOO", c, 60, "historical")
        assert row is not None
        ticker, period_end, resolution, origin = row[:4]
        assert ticker == "KXTEST-26-FOO"
        assert resolution == 60
        assert origin == "historical"

        # Bid OHLC in cents
        bid_o, bid_c, bid_h, bid_l = row[4:8]
        assert bid_o == 22
        assert bid_c == 21
        assert bid_h == 24
        assert bid_l == 20

        # Ask OHLC in cents
        ask_o, ask_c, ask_h, ask_l = row[8:12]
        assert ask_o == 23
        assert ask_c == 23

        # Price OHLC in cents
        price_o, price_c, price_h, price_l = row[12:16]
        assert price_o == 22
        assert price_c == 21

        # Volume and OI as integers
        volume, oi = row[16:18]
        assert volume == 13683
        assert oi == 379323

    def test_parse_candle_batch_format(self):
        """Batch/live API uses _dollars and _fp suffixes."""
        from collectors.kalshi.candles import _parse_candle

        c = {
            "end_period_ts": 1775412000,
            "yes_bid": {"open_dollars": "0.08", "close_dollars": "0.08",
                        "high_dollars": "0.08", "low_dollars": "0.08"},
            "yes_ask": {"open_dollars": "0.10", "close_dollars": "0.10",
                        "high_dollars": "0.10", "low_dollars": "0.10"},
            "price": {"open_dollars": "0.08", "close_dollars": "0.08",
                       "high_dollars": "0.08", "low_dollars": "0.08",
                       "mean_dollars": "0.08", "previous_dollars": "0.08"},
            "volume_fp": "1.00",
            "open_interest_fp": "25575.00",
        }
        row = _parse_candle("KXTEST-BATCH", c, 60, "live")
        assert row is not None
        assert row[3] == "live"
        # Bid close should be 8 cents
        assert row[5] == 8  # bid_close
        # Ask close should be 10 cents
        assert row[9] == 10  # ask_close
        # Volume and OI
        assert row[16] == 1
        assert row[17] == 25575

    def test_parse_candle_null_price(self):
        """Price fields can be null at minute resolution (no trade in period)."""
        from collectors.kalshi.candles import _parse_candle

        c = {
            "end_period_ts": 1775408400,
            "yes_bid": {"open_dollars": "0.08", "close_dollars": "0.08",
                        "high_dollars": "0.08", "low_dollars": "0.08"},
            "yes_ask": {"open_dollars": "0.10", "close_dollars": "0.10",
                        "high_dollars": "0.10", "low_dollars": "0.10"},
            "price": {"previous_dollars": "0.08"},  # only previous, no OHLC
            "volume_fp": "0.00",
            "open_interest_fp": "25575.00",
        }
        row = _parse_candle("KXTEST-NULL", c, 1, "live")
        assert row is not None
        # Price OHLC should be None
        assert row[12] is None  # price_open
        assert row[13] is None  # price_close
        assert row[14] is None  # price_high
        assert row[15] is None  # price_low
        # But bid/ask should be populated
        assert row[4] == 8   # bid_open
        assert row[5] == 8   # bid_close

    def test_parse_candle_no_timestamp(self):
        """Candle without end_period_ts returns None."""
        from collectors.kalshi.candles import _parse_candle

        c = {"volume_fp": "0.00"}
        assert _parse_candle("TICKER", c, 60, "live") is None

    def test_parse_candle_empty_sub_objects(self):
        """Candle with null/missing sub-objects doesn't crash."""
        from collectors.kalshi.candles import _parse_candle

        c = {"end_period_ts": 1775412000, "volume_fp": "0.00"}
        row = _parse_candle("TICKER", c, 60, "live")
        assert row is not None
        # All bid/ask/price should be None
        for i in range(4, 16):
            assert row[i] is None
        assert row[16] == 0   # volume
        assert row[17] is None  # OI not provided

    def test_dollars_to_cents(self):
        from collectors.kalshi.candles import _dollars_to_cents

        assert _dollars_to_cents("0.85") == 85
        assert _dollars_to_cents("0.03") == 3
        assert _dollars_to_cents("0.9300") == 93
        assert _dollars_to_cents("1.00") == 100
        assert _dollars_to_cents(None) is None
        assert _dollars_to_cents("") is None

    def test_resolution_dataset_mapping(self):
        from collectors.kalshi.candles import RESOLUTION_DATASET

        assert RESOLUTION_DATASET[1] == "kalshi_candles_minute"
        assert RESOLUTION_DATASET[60] == "kalshi_candles_hourly"
        assert RESOLUTION_DATASET[1440] == "kalshi_candles_daily"

    def test_parse_candle_historical_minute_has_bid_ask(self):
        """Historical API at 1-min resolution returns bid/ask (regression test).

        The original backfill produced 62M rows with all-NULL bid/ask because
        the parser was buggy at the time. This test ensures the parser correctly
        extracts bid/ask from the historical API format at minute resolution.
        """
        from collectors.kalshi.candles import _parse_candle

        # Exact format returned by Kalshi historical API at resolution=1
        c = {
            "end_period_ts": 1761673500,
            "open_interest": "3945458.00",
            "price": {"close": None, "high": None, "low": None,
                       "mean": None, "open": None, "previous": "0.0200"},
            "volume": "0.00",
            "yes_ask": {"close": "0.0200", "high": "0.0200",
                         "low": "0.0200", "open": "0.0200"},
            "yes_bid": {"close": "0.0100", "high": "0.0100",
                         "low": "0.0100", "open": "0.0100"},
        }
        row = _parse_candle("KXTEST-MINREG", c, 1, "historical")
        assert row is not None
        # bid and ask must be populated even though volume=0 and price=NULL
        assert row[4] == 1   # bid_open
        assert row[5] == 1   # bid_close
        assert row[8] == 2   # ask_open
        assert row[9] == 2   # ask_close
        # price should be NULL (no trades)
        assert row[12] is None
        assert row[13] is None
        # volume and OI populated
        assert row[16] == 0
        assert row[17] == 3945458


class TestCandleFlushSplice:
    """Tests for candle splice-aware flush functions (DB-dependent)."""

    @pytest.fixture
    def candle_conn(self, db_conn):
        """Setup: insert a test candle, clean up after."""

        cur = db_conn.cursor()
        # Insert a live hourly candle
        cur.execute("""
            INSERT INTO prediction_markets.kalshi_candles
                (ticker, period_end, resolution, origin,
                 yes_bid_open, yes_bid_close, yes_bid_high, yes_bid_low,
                 yes_ask_open, yes_ask_close, yes_ask_high, yes_ask_low,
                 price_open, price_close, price_high, price_low,
                 volume, open_interest)
            VALUES ('_TEST_CANDLE', '2026-01-01 12:00:00+00', 60, 'live',
                    40, 42, 45, 38,
                    45, 47, 50, 43,
                    41, 43, 46, 39,
                    100, 5000)
            ON CONFLICT (ticker, period_end, resolution) DO UPDATE SET
                origin = 'live', yes_bid_close = 42, price_close = 43
        """)
        db_conn.commit()
        yield db_conn
        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_candles WHERE ticker LIKE '_TEST_CANDLE%'")
        db_conn.commit()
        cur.close()

    def test_historical_replaces_live(self, candle_conn):
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_historical

        cur = candle_conn.cursor()
        period_end = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        buffer = [("_TEST_CANDLE", period_end, 60, "historical",
                    50, 52, 55, 48,
                    55, 57, 60, 53,
                    51, 53, 56, 49,
                    200, 6000)]
        _flush_candles_historical(cur, buffer)
        candle_conn.commit()

        cur.execute("""
            SELECT origin, yes_bid_close, price_close, volume
            FROM prediction_markets.kalshi_candles
            WHERE ticker = '_TEST_CANDLE' AND resolution = 60
        """)
        row = cur.fetchone()
        assert row[0] == "historical"
        assert row[1] == 52  # updated
        assert row[2] == 53  # updated
        assert row[3] == 200  # updated
        cur.close()

    def test_historical_does_not_replace_historical(self, candle_conn):
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_historical

        cur = candle_conn.cursor()
        # Make existing row historical
        cur.execute("""
            UPDATE prediction_markets.kalshi_candles
            SET origin = 'historical'
            WHERE ticker = '_TEST_CANDLE' AND resolution = 60
        """)
        candle_conn.commit()

        period_end = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        buffer = [("_TEST_CANDLE", period_end, 60, "historical",
                    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 999, 9999)]
        _flush_candles_historical(cur, buffer)
        candle_conn.commit()

        cur.execute("""
            SELECT yes_bid_close FROM prediction_markets.kalshi_candles
            WHERE ticker = '_TEST_CANDLE' AND resolution = 60
        """)
        assert cur.fetchone()[0] == 42  # unchanged
        cur.close()

    def test_live_does_not_replace_existing(self, candle_conn):
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_live

        cur = candle_conn.cursor()
        period_end = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        buffer = [("_TEST_CANDLE", period_end, 60, "live",
                    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 999, 9999)]
        inserted = _flush_candles_live(cur, buffer)
        candle_conn.commit()
        assert inserted == 0

        cur.execute("""
            SELECT yes_bid_close FROM prediction_markets.kalshi_candles
            WHERE ticker = '_TEST_CANDLE' AND resolution = 60
        """)
        assert cur.fetchone()[0] == 42  # unchanged
        cur.close()

    def test_live_inserts_new(self, candle_conn):
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_live

        cur = candle_conn.cursor()
        period_end = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)  # different period
        buffer = [("_TEST_CANDLE", period_end, 60, "live",
                    30, 32, 35, 28, 35, 37, 40, 33, 31, 33, 36, 29, 50, 4000)]
        inserted = _flush_candles_live(cur, buffer)
        candle_conn.commit()
        assert inserted == 1

        cur.execute("""
            SELECT origin, yes_bid_close FROM prediction_markets.kalshi_candles
            WHERE ticker = '_TEST_CANDLE' AND period_end = %s AND resolution = 60
        """, (period_end,))
        row = cur.fetchone()
        assert row[0] == "live"
        assert row[1] == 32
        cur.close()

    def test_different_resolutions_coexist(self, candle_conn):
        """Minute and hourly candles for the same ticker/time don't conflict."""
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_live

        cur = candle_conn.cursor()
        period_end = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Insert minute candle at same period_end (different resolution)
        buffer = [("_TEST_CANDLE", period_end, 1, "live",
                    40, 41, 42, 39, 45, 46, 47, 44, None, None, None, None, 5, 5000)]
        inserted = _flush_candles_live(cur, buffer)
        candle_conn.commit()
        assert inserted == 1

        # Should now have 2 rows: one at res=60, one at res=1
        cur.execute("""
            SELECT resolution, origin FROM prediction_markets.kalshi_candles
            WHERE ticker = '_TEST_CANDLE' AND period_end = %s
            ORDER BY resolution
        """, (period_end,))
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == 1    # minute
        assert rows[1][0] == 60   # hourly
        cur.close()

    @pytest.mark.db
    def test_flush_historical_deduplicates_batch(self, candle_conn):
        """Flush handles duplicate (ticker, period_end, resolution) in same batch.

        The Kalshi API can return overlapping candles at chunk boundaries.
        Without dedup, execute_values raises CardinalityViolation.
        """
        from datetime import datetime, timezone
        from collectors.kalshi.candles import _flush_candles_historical

        cur = candle_conn.cursor()
        period_end = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ticker = "_TEST_DEDUP"

        cur.execute("DELETE FROM prediction_markets.kalshi_candles WHERE ticker = %s", (ticker,))
        candle_conn.commit()

        # Two rows with same (ticker, period_end, resolution) — second should win
        buffer = [
            (ticker, period_end, 60, "historical",
             10, 11, 12, 9, 20, 21, 22, 19, 15, 16, 17, 14, 100, 5000),
            (ticker, period_end, 60, "historical",
             30, 31, 32, 29, 40, 41, 42, 39, 35, 36, 37, 34, 200, 6000),
        ]
        _flush_candles_historical(cur, buffer)
        candle_conn.commit()

        cur.execute("""
            SELECT yes_bid_open, volume FROM prediction_markets.kalshi_candles
            WHERE ticker = %s AND resolution = 60
        """, (ticker,))
        row = cur.fetchone()
        assert row is not None
        # Second row should have won (last-wins dedup)
        assert row[0] == 30
        assert row[1] == 200

        cur.execute("DELETE FROM prediction_markets.kalshi_candles WHERE ticker = %s", (ticker,))
        candle_conn.commit()
        cur.close()


# --- Snapshot ingestion tests ---


class TestSnapshotParsing:
    """Tests for collectors.kalshi.snapshots row mapping."""

    def test_parse_orderbook_fp_format(self):
        """New API format: orderbook_fp with dollar strings."""
        from collectors.kalshi.snapshots import _parse_orderbook

        data = {
            "orderbook_fp": {
                "yes_dollars": [["0.85", "100.00"], ["0.84", "200.00"]],
                "no_dollars": [["0.18", "150.00"], ["0.17", "50.00"]],
            }
        }
        ob = _parse_orderbook(data)
        assert ob["yes"] == [[85, 100], [84, 200]]
        assert ob["no"] == [[18, 150], [17, 50]]

    def test_parse_orderbook_old_format(self):
        """Old API format: orderbook with integer cents."""
        from collectors.kalshi.snapshots import _parse_orderbook

        data = {"orderbook": {"yes": [[85, 100]], "no": [[18, 150]]}}
        ob = _parse_orderbook(data)
        assert ob["yes"] == [[85, 100]]
        assert ob["no"] == [[18, 150]]

    def test_parse_orderbook_empty(self):
        """Empty orderbook in both formats."""
        from collectors.kalshi.snapshots import _parse_orderbook

        assert _parse_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}) == {"yes": [], "no": []}
        assert _parse_orderbook({"orderbook": {"yes": [], "no": []}}) == {"yes": [], "no": []}
        assert _parse_orderbook({}) == {"yes": [], "no": []}

    def test_parse_snapshot_both_sides(self):
        """Orderbook with both yes and no levels produces valid snapshot."""
        from collectors.kalshi.snapshots import _parse_snapshot

        orderbook = {
            "yes": [[85, 100], [84, 200]],
            "no": [[18, 150], [17, 50]],
        }
        row = _parse_snapshot("KXTEST-SNAP", orderbook, 5000, 1000)
        assert row is not None
        (ticker, ts, yes_bid, yes_ask, bid_depth, ask_depth, vol, oi, origin,
         yes_json, no_json) = row
        assert ticker == "KXTEST-SNAP"
        assert yes_bid == 85          # best yes bid
        assert yes_ask == 82          # 100 - 18 (best no bid)
        assert bid_depth == 300       # 100 + 200
        assert ask_depth == 200       # 150 + 50
        assert vol == 5000
        assert oi == 1000
        assert origin == "live"
        # JSONB level arrays round-trip the full orderbook
        import json
        assert json.loads(yes_json) == [[85, 100], [84, 200]]
        assert json.loads(no_json) == [[18, 150], [17, 50]]

    def test_parse_snapshot_yes_only(self):
        """Orderbook with only yes levels."""
        from collectors.kalshi.snapshots import _parse_snapshot

        orderbook = {"yes": [[90, 50]], "no": []}
        row = _parse_snapshot("KXTEST", orderbook, 0, 0)
        assert row is not None
        assert row[2] == 90      # yes_bid
        assert row[3] is None    # yes_ask (no "no" levels)
        assert row[4] == 50      # bid_depth
        assert row[5] == 0       # ask_depth

    def test_parse_snapshot_no_only(self):
        """Orderbook with only no levels."""
        from collectors.kalshi.snapshots import _parse_snapshot

        orderbook = {"yes": [], "no": [[25, 75]]}
        row = _parse_snapshot("KXTEST", orderbook, 0, 0)
        assert row is not None
        assert row[2] is None    # yes_bid
        assert row[3] == 75      # yes_ask = 100 - 25
        assert row[4] == 0       # bid_depth
        assert row[5] == 75      # ask_depth

    def test_parse_snapshot_empty_orderbook(self):
        """Completely empty orderbook returns None."""
        from collectors.kalshi.snapshots import _parse_snapshot

        assert _parse_snapshot("KXTEST", {"yes": [], "no": []}, 0, 0) is None
        assert _parse_snapshot("KXTEST", {}, 0, 0) is None

    def test_parse_snapshot_timestamp_is_utc(self):
        """Snapshot timestamp should be UTC."""
        from collectors.kalshi.snapshots import _parse_snapshot
        from datetime import timezone

        orderbook = {"yes": [[50, 10]], "no": [[50, 10]]}
        row = _parse_snapshot("KXTEST", orderbook, 0, 0)
        ts = row[1]
        assert ts.tzinfo == timezone.utc


class TestSnapshotFlush:
    """Tests for snapshot flush (dedup behavior, DB-dependent)."""

    @pytest.fixture
    def snap_conn(self, db_conn):
        """Clean up test snapshots after each test."""
        yield db_conn
        cur = db_conn.cursor()
        cur.execute("""
            DELETE FROM prediction_markets.kalshi_snapshots
            WHERE ticker LIKE '_TEST_SNAP%'
        """)
        db_conn.commit()
        cur.close()

    def test_insert_new_snapshot(self, snap_conn):
        from collectors.kalshi.snapshots import _flush_snapshots
        from datetime import datetime, timezone

        cur = snap_conn.cursor()
        now = datetime.now(timezone.utc)
        buffer = [("_TEST_SNAP_1", now, 85, 88, 100, 200, 5000, 1000, "live")]
        inserted = _flush_snapshots(cur, buffer)
        snap_conn.commit()
        assert inserted == 1

        cur.execute("""
            SELECT yes_bid, yes_ask, origin FROM prediction_markets.kalshi_snapshots
            WHERE ticker = '_TEST_SNAP_1'
        """)
        row = cur.fetchone()
        assert row[0] == 85
        assert row[1] == 88
        assert row[2] == "live"
        cur.close()

    def test_dedup_on_conflict(self, snap_conn):
        """Same (ticker, timestamp) should not duplicate."""
        from collectors.kalshi.snapshots import _flush_snapshots
        from datetime import datetime, timezone

        cur = snap_conn.cursor()
        now = datetime.now(timezone.utc)
        buffer = [("_TEST_SNAP_2", now, 85, 88, 100, 200, 5000, 1000, "live")]

        # Insert first
        inserted1 = _flush_snapshots(cur, buffer)
        snap_conn.commit()
        assert inserted1 == 1

        # Insert same again — should be ignored
        buffer2 = [("_TEST_SNAP_2", now, 90, 92, 150, 250, 6000, 2000, "live")]
        inserted2 = _flush_snapshots(cur, buffer2)
        snap_conn.commit()
        assert inserted2 == 0

        # Original values preserved
        cur.execute("""
            SELECT yes_bid FROM prediction_markets.kalshi_snapshots
            WHERE ticker = '_TEST_SNAP_2'
        """)
        assert cur.fetchone()[0] == 85
        cur.close()


class TestSnapshotAnomalyChecks:
    """Snapshot anomaly checks run without errors."""

    @pytest.mark.db
    def test_snapshot_checks_structure(self, db_conn):
        from data.health.checks.kalshi_snapshots import run_checks

        results = run_checks(db_conn, "kalshi_snapshots")
        assert len(results) == 2
        names = {r.check_name for r in results}
        assert "no_inverted_spreads" in names
        assert "recent_inserts" in names
        for r in results:
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)


class TestDiscoveryMarketStructure:
    """Tests for collectors.kalshi.discovery.derive_market_structure_with_mutex"""

    def test_standalone_single_market(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [{"status": "active", "strike_type": "less"}]
        assert derive_market_structure_with_mutex(markets, False) == "standalone"

    def test_standalone_no_markets(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        assert derive_market_structure_with_mutex([], False) == "standalone"

    def test_exhaustive_partition_between(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active", "strike_type": "less"},
            {"status": "active", "strike_type": "between"},
            {"status": "active", "strike_type": "between"},
            {"status": "active", "strike_type": "greater"},
        ]
        assert derive_market_structure_with_mutex(markets, False) == "exhaustive_partition"

    def test_exhaustive_partition_mutex(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active"},
            {"status": "active"},
            {"status": "active"},
        ]
        assert derive_market_structure_with_mutex(markets, True) == "exhaustive_partition"

    def test_monotone_threshold(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active", "strike_type": "greater"},
            {"status": "active", "strike_type": "greater_or_equal"},
        ]
        assert derive_market_structure_with_mutex(markets, False) == "monotone_threshold"

    def test_standalone_multiple_no_strikes(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active"},
            {"status": "active"},
        ]
        # Multiple markets, not mutex, no strike types → standalone
        assert derive_market_structure_with_mutex(markets, False) == "standalone"

    def test_inactive_markets_ignored(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active", "strike_type": "between"},
            {"status": "closed", "strike_type": "between"},
        ]
        # Only one active market → standalone
        assert derive_market_structure_with_mutex(markets, False) == "standalone"

    def test_between_takes_priority_over_mutex(self):
        from collectors.kalshi.discovery import derive_market_structure_with_mutex
        markets = [
            {"status": "active", "strike_type": "between"},
            {"status": "active", "strike_type": "between"},
        ]
        # 'between' detected → exhaustive_partition, regardless of mutex
        assert derive_market_structure_with_mutex(markets, False) == "exhaustive_partition"


class TestDiscoveryUpsert:
    """Tests for discovery upsert functions (require DB)."""

    @pytest.mark.db
    def test_upsert_event_insert_and_update(self, db_conn):
        from collectors.kalshi.discovery import _upsert_event
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-DISCOVERY-EVT-001"

        # Clean up from any prior run
        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE event_ticker = %s", (ticker,))
        cur.execute("DELETE FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()

        event = {
            "event_ticker": ticker,
            "title": "Test Event",
            "category": "Financials",
            "series_ticker": "KXTEST",
            "sub_title": "Test Sub",
            "strike_period": None,
            "mutually_exclusive": True,
        }

        # Insert
        _upsert_event(cur, event, "exhaustive_partition", now)
        db_conn.commit()

        cur.execute("SELECT title, category, market_structure, origin FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == "Test Event"
        assert row[1] == "Financials"
        assert row[2] == "exhaustive_partition"
        assert row[3] == "live"

        # Update (upsert with changed title)
        event["title"] = "Updated Event"
        _upsert_event(cur, event, "standalone", now)
        db_conn.commit()

        cur.execute("SELECT title, market_structure FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == "Updated Event"
        assert row[1] == "standalone"

        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_market_insert_and_update(self, db_conn):
        from collectors.kalshi.discovery import _upsert_market
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-DISC-MKT-001"

        # Clean up
        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()

        market = {
            "ticker": ticker,
            "event_ticker": "TEST-DISC-EVT",
            "title": "Test Market",
            "status": "active",
            "close_time": "2026-12-31T23:59:00Z",
            "volume_fp": "1000.00",
            "open_interest_fp": "500.00",
            "rules_primary": "Test rules",
            "rules_secondary": None,
            "strike_type": "between",
            "floor_strike": 100.5,
            "market_type": "binary",
            "expected_expiration_time": "2026-12-31T23:59:00Z",
            "open_time": "2026-01-01T00:00:00Z",
            "created_time": "2025-12-15T12:00:00Z",
            "can_close_early": True,
            "result": "",
            "yes_sub_title": "Yes side",
            "no_sub_title": "No side",
        }

        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("""
            SELECT volume, open_interest, strike_type, origin, close_time
            FROM prediction_markets.kalshi_markets WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        assert row[0] == 1000  # volume from _fp
        assert row[1] == 500   # OI from _fp
        assert row[2] == "between"
        assert row[3] == "live"
        assert row[4] is not None  # timestamptz, not text

        # Update
        market["volume_fp"] = "2000.00"
        market["status"] = "closed"
        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("SELECT volume, status FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == 2000
        assert row[1] == "closed"

        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_market_fp_fallback(self, db_conn):
        """When _fp fields missing, falls back to integer fields."""
        from collectors.kalshi.discovery import _upsert_market
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-DISC-MKT-FB"

        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()

        market = {
            "ticker": ticker,
            "event_ticker": "TEST-EVT",
            "title": "Fallback Test",
            "status": "active",
            "close_time": "2026-12-31T23:59:00Z",
            "volume": 750,  # integer, not _fp
            "open_interest": 300,
        }

        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("SELECT volume, open_interest FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == 750
        assert row[1] == 300

        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_event_clears_superseded(self, db_conn):
        """Re-upserting an event clears its superseded_at (it's active again)."""
        from collectors.kalshi.discovery import _upsert_event
        from datetime import datetime, timezone, timedelta

        cur = db_conn.cursor()
        ticker = "TEST-DISC-EVT-SUP"
        now = datetime.now(timezone.utc)

        cur.execute("DELETE FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()

        event = {
            "event_ticker": ticker, "title": "Test", "category": "Financials",
            "series_ticker": "KXTEST", "sub_title": None,
            "strike_period": None, "mutually_exclusive": False,
        }

        # Insert, then manually set superseded_at (simulating disappearance)
        _upsert_event(cur, event, "standalone", now - timedelta(hours=1))
        db_conn.commit()
        cur.execute("UPDATE prediction_markets.kalshi_events SET superseded_at = %s WHERE event_ticker = %s",
                    (now - timedelta(minutes=30), ticker))
        db_conn.commit()

        cur.execute("SELECT superseded_at FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        assert cur.fetchone()[0] is not None  # superseded

        # Re-upsert (event reappeared in API)
        _upsert_event(cur, event, "standalone", now)
        db_conn.commit()

        cur.execute("SELECT superseded_at, recorded_at FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] is None  # superseded_at cleared
        assert row[1] >= now   # recorded_at updated

        cur.execute("DELETE FROM prediction_markets.kalshi_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_market_clears_superseded(self, db_conn):
        """Re-upserting a market clears its superseded_at."""
        from collectors.kalshi.discovery import _upsert_market
        from datetime import datetime, timezone, timedelta

        cur = db_conn.cursor()
        ticker = "TEST-DISC-MKT-SUP"
        now = datetime.now(timezone.utc)

        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()

        market = {
            "ticker": ticker, "event_ticker": "TEST-EVT",
            "title": "Test", "status": "active",
            "close_time": "2026-12-31T23:59:00Z",
            "volume_fp": "100.00", "open_interest_fp": "50.00",
        }

        # Insert, then manually supersede
        _upsert_market(cur, market, now - timedelta(hours=1))
        db_conn.commit()
        cur.execute("UPDATE prediction_markets.kalshi_markets SET superseded_at = %s WHERE ticker = %s",
                    (now - timedelta(minutes=30), ticker))
        db_conn.commit()

        # Re-upsert
        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("SELECT superseded_at FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        assert cur.fetchone()[0] is None  # cleared

        cur.execute("DELETE FROM prediction_markets.kalshi_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()


class TestDiscoveryAnomalyChecks:
    """Tests for discovery anomaly checks (require DB)."""

    @pytest.mark.db
    def test_events_checks_structure(self, db_conn):
        from data.health.checks.kalshi_events import run_checks
        results = run_checks(db_conn, "kalshi_events")
        assert len(results) == 2
        for r in results:
            assert isinstance(r.check_name, str)
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)

    @pytest.mark.db
    def test_markets_checks_structure(self, db_conn):
        from data.health.checks.kalshi_markets import run_checks
        results = run_checks(db_conn, "kalshi_markets")
        assert len(results) == 2
        for r in results:
            assert isinstance(r.check_name, str)
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)


class TestHealthCacheMultiResolution:
    """Tests for resolution-aware health cache updates."""

    @pytest.mark.db
    def test_filter_column_in_cache_update(self, db_conn):
        """Health cache with filter_column/filter_value queries correct resolution."""
        from data.health.check import check_all

        statuses = check_all(db_conn)
        candle_statuses = {s.dataset_id: s for s in statuses if 'candle' in s.dataset_id}

        # Minute has data (historical), hourly may have live data
        if 'kalshi_candles_minute' in candle_statuses:
            minute = candle_statuses['kalshi_candles_minute']
            assert minute.health_status == 'backfill_only'  # no stale interval
            assert minute.max_freshness is not None  # has historical data

        if 'kalshi_candles_daily' in candle_statuses:
            daily = candle_statuses['kalshi_candles_daily']
            # Daily has no data yet unless we collected some
            assert daily.health_status in ('no_data', 'stale', 'healthy')

    @pytest.mark.db
    def test_candle_anomaly_checks(self, db_conn):
        """Candle anomaly checks run without errors and return correct structure."""
        from data.health.checks.kalshi_candles import run_checks

        results = run_checks(db_conn, "kalshi_candles_minute")
        assert len(results) == 3
        # Verify structure — each result has name, passed, message
        for r in results:
            assert isinstance(r.check_name, str)
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)

        # Check via the resolution-specific entry point too
        from data.health.checks.kalshi_candles_hourly import run_checks as run_hourly
        results_h = run_hourly(db_conn, "kalshi_candles_hourly")
        assert len(results_h) == 3


# --- Settled collector tests ---


class TestSettledMarketStructure:
    """Tests for collectors.kalshi.settled.derive_market_structure"""

    def test_standalone_single_market(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [{"strike_type": "less"}]
        assert derive_market_structure(markets, False) == "standalone"

    def test_standalone_no_markets(self):
        from collectors.kalshi.settled import derive_market_structure
        assert derive_market_structure([], False) == "standalone"

    def test_exhaustive_partition_between(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [
            {"strike_type": "less"},
            {"strike_type": "between"},
            {"strike_type": "between"},
            {"strike_type": "greater"},
        ]
        assert derive_market_structure(markets, False) == "exhaustive_partition"

    def test_exhaustive_partition_mutex(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [{}, {}, {}]
        assert derive_market_structure(markets, True) == "exhaustive_partition"

    def test_monotone_threshold(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [
            {"strike_type": "greater"},
            {"strike_type": "greater_or_equal"},
        ]
        assert derive_market_structure(markets, False) == "monotone_threshold"

    def test_standalone_multiple_no_strikes(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [{}, {}]
        assert derive_market_structure(markets, False) == "standalone"

    def test_no_status_filter(self):
        """Unlike discovery, settled doesn't filter by status — all markets count."""
        from collectors.kalshi.settled import derive_market_structure
        markets = [
            {"status": "settled", "strike_type": "between"},
            {"status": "settled", "strike_type": "between"},
        ]
        assert derive_market_structure(markets, False) == "exhaustive_partition"

    def test_between_takes_priority_over_mutex(self):
        from collectors.kalshi.settled import derive_market_structure
        markets = [
            {"strike_type": "between"},
            {"strike_type": "between"},
        ]
        assert derive_market_structure(markets, False) == "exhaustive_partition"


class TestSettledUpsert:
    """Tests for settled upsert functions (require DB)."""

    @pytest.mark.db
    def test_upsert_event_insert_and_update(self, db_conn):
        from collectors.kalshi.settled import _upsert_event
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-SETTLED-EVT-001"

        # Clean up
        cur.execute("DELETE FROM prediction_markets.kalshi_settled_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()

        event = {
            "event_ticker": ticker,
            "title": "Test Settled Event",
            "category": "Financials",
            "series_ticker": "KXTEST",
            "mutually_exclusive": True,
        }
        markets = [
            {"close_time": "2026-01-15T20:00:00Z"},
            {"close_time": "2026-01-15T22:00:00Z"},
        ]

        _upsert_event(cur, event, markets, "exhaustive_partition", now)
        db_conn.commit()

        cur.execute("""
            SELECT title, category, market_structure, origin, series_ticker,
                   mutually_exclusive, num_markets, settled_at
            FROM prediction_markets.kalshi_settled_events WHERE event_ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        assert row[0] == "Test Settled Event"
        assert row[1] == "Financials"
        assert row[2] == "exhaustive_partition"
        assert row[3] == "live"
        assert row[4] == "KXTEST"
        assert row[5] is True
        assert row[6] == 2
        assert row[7] is not None  # settled_at derived from max(close_time)

        # Update
        event["title"] = "Updated Settled Event"
        _upsert_event(cur, event, markets, "standalone", now)
        db_conn.commit()

        cur.execute("SELECT title, market_structure FROM prediction_markets.kalshi_settled_events WHERE event_ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == "Updated Settled Event"
        assert row[1] == "standalone"

        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_settled_events WHERE event_ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_market_insert_and_update(self, db_conn):
        from collectors.kalshi.settled import _upsert_market
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-SETTLED-MKT-001"

        cur.execute("DELETE FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()

        market = {
            "ticker": ticker,
            "event_ticker": "TEST-SETTLED-EVT",
            "title": "Test Settled Market",
            "result": "Yes",
            "volume_fp": "1500.00",
            "close_time": "2026-01-15T22:00:00Z",
            "strike_type": "between",
            "floor_strike": 5500.0,
        }

        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("""
            SELECT result, volume, origin, strike_type, floor_strike, settled_at, close_time
            FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        assert row[0] == "yes"    # lowercased
        assert row[1] == 1500     # from volume_fp
        assert row[2] == "live"
        assert row[3] == "between"
        assert row[4] == 5500.0
        assert row[5] is not None  # settled_at from close_time
        assert row[6] is not None  # close_time stored

        # Update — volume 0 should not overwrite existing
        market["volume_fp"] = "0"
        market["result"] = "no"
        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("SELECT result, volume FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == "no"   # result updated
        assert row[1] == 1500   # volume preserved (0 doesn't overwrite)

        # Cleanup
        cur.execute("DELETE FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()

    @pytest.mark.db
    def test_upsert_market_fp_fallback(self, db_conn):
        """When _fp field missing, falls back to integer volume."""
        from collectors.kalshi.settled import _upsert_market
        from datetime import datetime, timezone

        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        ticker = "TEST-SETTLED-MKT-FB"

        cur.execute("DELETE FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()

        market = {
            "ticker": ticker,
            "event_ticker": "TEST-EVT",
            "title": "Fallback Test",
            "result": "yes",
            "volume": 750,  # integer, not _fp
            "close_time": "2026-01-15T22:00:00Z",
        }

        _upsert_market(cur, market, now)
        db_conn.commit()

        cur.execute("SELECT volume FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        row = cur.fetchone()
        assert row[0] == 750

        cur.execute("DELETE FROM prediction_markets.kalshi_settled_markets WHERE ticker = %s", (ticker,))
        db_conn.commit()
        cur.close()


class TestSettledAnomalyChecks:
    """Tests for settled anomaly checks (require DB)."""

    @pytest.mark.db
    def test_settled_events_checks_structure(self, db_conn):
        from data.health.checks.kalshi_settled_events import run_checks
        results = run_checks(db_conn, "kalshi_settled_events")
        assert len(results) == 2
        for r in results:
            assert isinstance(r.check_name, str)
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)

    @pytest.mark.db
    def test_settled_markets_checks_structure(self, db_conn):
        from data.health.checks.kalshi_settled_markets import run_checks
        results = run_checks(db_conn, "kalshi_settled_markets")
        assert len(results) == 2
        for r in results:
            assert isinstance(r.check_name, str)
            assert isinstance(r.passed, bool)
            assert isinstance(r.message, str)
