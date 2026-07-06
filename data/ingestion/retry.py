"""Retry with exponential backoff for transient API errors.

Extracts the retry pattern already implemented in kalshi_collector.py:make_request
and historical_downloader.py into a reusable utility.

Usage:
    response = with_retry(lambda: requests.get(url, headers=headers))
"""

import random
import time
from typing import Callable, TypeVar

import requests

T = TypeVar("T")

# HTTP status codes that are safe to retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RetryExhausted(Exception):
    """All retry attempts failed."""

    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Failed after {attempts} attempts: {last_error}")


def with_retry(
    fn: Callable[[], T],
    max_retries: int = 3,
    retryable: set[int] = RETRYABLE_STATUS_CODES,
    base_delay: float = 1.0,
) -> T:
    """Execute fn with exponential backoff + jitter on retryable errors.

    - Retries on HTTP status codes in retryable set, plus network errors.
    - Does NOT retry on 4xx (except 429) or authentication errors.
    - Returns the successful response or raises RetryExhausted.

    Args:
        fn: Zero-argument callable that makes the request. Should return
            a requests.Response or any value.
        max_retries: Maximum number of attempts (including the first).
        retryable: Set of HTTP status codes to retry on.
        base_delay: Base delay in seconds (doubles each retry).

    Returns:
        The return value of fn on success.

    Raises:
        RetryExhausted: After all retries are exhausted.
        Exception: Non-retryable errors are raised immediately.
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            result = fn()
        except (requests.RequestException, OSError, ConnectionError) as e:
            # If fn() did its own raise_for_status(), we get HTTPError.
            # Only retry if the status code is retryable; otherwise propagate.
            if isinstance(e, requests.HTTPError) and e.response is not None:
                if e.response.status_code not in retryable:
                    raise
            last_error = e
        else:
            # fn() succeeded — check the response if it's HTTP
            if isinstance(result, requests.Response):
                if result.status_code < 400:
                    return result
                if result.status_code in retryable:
                    last_error = RuntimeError(
                        f"HTTP {result.status_code}: {result.text[:200]}"
                    )
                else:
                    # Non-retryable HTTP error — raise immediately
                    result.raise_for_status()
            else:
                return result

        # Exponential backoff with jitter
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)

    raise RetryExhausted(max_retries, last_error)
