"""Rate limiting and retry for Coinbase REST calls.

The SDK (coinbase-advanced-py 1.8.4, coinbase/rest/rest_base.py) raises
``requests.exceptions.HTTPError`` with the response attached for every 4xx and
5xx, and lets ``requests`` connection and timeout errors propagate. This module
wraps each call with:

* a client side throttle, so the bot stays under Coinbase's documented limit of
  10,000 requests per hour per API key before the server has to refuse it;
* bounded exponential backoff with full jitter on 429, 5xx, timeouts and
  dropped connections, honouring a ``Retry-After`` header when one is sent;
* no retry on other 4xx (bad request, auth, permission), which will not heal.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import requests

from cof_bot.errors import ExchangeError, RateLimitError

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0

    def delay(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Full jitter backoff for the given zero based retry attempt."""
        ceiling = min(self.backoff_cap_s, self.backoff_base_s * (2**attempt))
        return ceiling * rng()


class Throttle:
    """Minimum spacing between calls. Thread safe."""

    def __init__(self, max_per_second: float, clock=time.monotonic, sleep=time.sleep):
        if max_per_second <= 0:
            raise ValueError("max_per_second must be > 0")
        self._interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next_at:
                self._sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self._interval


def _status_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _retry_after_s(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        return _status_of(exc) in RETRYABLE_STATUS
    return False


def call_with_retry(
    fn: Callable[..., T],
    *args,
    policy: RetryPolicy,
    throttle: Throttle | None = None,
    sleep: Callable[[float], None] = time.sleep,
    op_name: str | None = None,
    **kwargs,
) -> T:
    """Call ``fn`` with throttling and retry. Raises ExchangeError on final failure."""
    name = op_name or getattr(fn, "__name__", "call")
    attempt = 0
    while True:
        if throttle is not None:
            throttle.wait()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001, classified below
            status = _status_of(exc)
            if not is_retryable(exc):
                raise ExchangeError(f"{name} failed: {exc}", status_code=status) from exc
            if attempt >= policy.max_retries:
                cls = RateLimitError if status == 429 else ExchangeError
                raise cls(
                    f"{name} failed after {attempt + 1} attempts: {exc}", status_code=status
                ) from exc
            wait_s = _retry_after_s(exc)
            if wait_s is None:
                wait_s = policy.delay(attempt)
            wait_s = min(wait_s, policy.backoff_cap_s)
            log.warning(
                "%s attempt %d failed (%s); retrying in %.2fs",
                name,
                attempt + 1,
                status if status is not None else type(exc).__name__,
                wait_s,
            )
            sleep(wait_s)
            attempt += 1
