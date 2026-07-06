"""Per-source rate limiting with cross-process coordination.

Uses a PG advisory lock + timestamp table to coordinate across processes.
Each acquire() checks the last request timestamp for this source and sleeps
if needed to maintain the configured QPS across all processes.

Usage:
    limiter = RateLimiter("kalshi", qps=25.0, conn=conn)
    for page in pages:
        limiter.acquire()
        response = api_call(page)
"""

import time


class RateLimiter:
    """Per-source rate limiting with cross-process coordination via PostgreSQL.

    The coordination table (prediction_markets.rate_limit_state) stores the last
    request timestamp per source. Each acquire() does a SELECT ... FOR UPDATE,
    checks elapsed time, sleeps if needed, then updates last_request_at. The row
    lock is held only for the duration of the timestamp check (~microseconds).
    """

    def __init__(self, source: str, qps: float, conn):
        """
        Args:
            source: Source identifier (e.g., 'kalshi', 'fred'). Must match
                    a row in rate_limit_state.
            qps: Requests per second limit. Used to initialize the row if
                 it doesn't exist.
            conn: psycopg2 connection (must NOT be in autocommit mode for
                  the row-level lock to work).
        """
        self.source = source
        self.min_interval = 1.0 / qps
        self.conn = conn
        self._ensure_row(qps)

    def _ensure_row(self, qps: float) -> None:
        """Insert the rate limit row if it doesn't exist."""
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO prediction_markets.rate_limit_state (source, qps_limit)
                   VALUES (%s, %s)
                   ON CONFLICT (source) DO UPDATE SET qps_limit = EXCLUDED.qps_limit""",
                (self.source, qps),
            )
        self.conn.commit()

    def acquire(self) -> None:
        """Block until a request slot is available.

        Coordinates across all processes using the same source by serializing
        on the rate_limit_state row. The lock scope is a single transaction
        that reads the timestamp, sleeps if needed, and updates it.
        """
        with self.conn.cursor() as cur:
            # Lock this source's row (blocks other processes doing the same)
            cur.execute(
                """SELECT last_request_at FROM prediction_markets.rate_limit_state
                   WHERE source = %s FOR UPDATE""",
                (self.source,),
            )
            row = cur.fetchone()
            if row is None:
                # Shouldn't happen after _ensure_row, but be safe
                self.conn.commit()
                return

            last_at = row[0]
            now = time.time()
            # Convert DB timestamp to epoch for comparison
            last_epoch = last_at.timestamp()
            elapsed = now - last_epoch
            wait = self.min_interval - elapsed

            if wait > 0:
                time.sleep(wait)

            cur.execute(
                """UPDATE prediction_markets.rate_limit_state
                   SET last_request_at = now()
                   WHERE source = %s""",
                (self.source,),
            )
        self.conn.commit()
