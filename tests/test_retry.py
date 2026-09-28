import pytest
import requests

from cof_bot.errors import ExchangeError, RateLimitError
from cof_bot.exchange.retry import RetryPolicy, Throttle, call_with_retry, is_retryable
from conftest import make_http_error


class Flaky:
    def __init__(self, failures, result="ok"):
        self.failures = list(failures)
        self.result = result
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


POLICY = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_cap_s=8.0)


@pytest.mark.parametrize(
    "exc",
    [
        make_http_error(429),
        make_http_error(500),
        make_http_error(503),
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ReadTimeout("slow"),
    ],
)
def test_transient_errors_are_retried(exc, no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([exc, exc])
    assert call_with_retry(fn, policy=POLICY, sleep=sleep) == "ok"
    assert fn.calls == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_not_retried(status, no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([make_http_error(status)])
    with pytest.raises(ExchangeError) as info:
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert info.value.status_code == status
    assert fn.calls == 1 and sleeps == []


def test_persistent_429_raises_rate_limit_error(no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([make_http_error(429)] * 10)
    with pytest.raises(RateLimitError):
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert fn.calls == POLICY.max_retries + 1
    assert all(0 <= s <= POLICY.backoff_cap_s for s in sleeps)


def test_retry_after_header_honoured_and_capped(no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([make_http_error(429, {"Retry-After": "3"}), make_http_error(429, {"Retry-After": "999"})])
    call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert sleeps == [3.0, POLICY.backoff_cap_s]


def test_zero_retries_fails_fast(no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([make_http_error(503)])
    with pytest.raises(ExchangeError):
        call_with_retry(fn, policy=RetryPolicy(max_retries=0), sleep=sleep)
    assert sleeps == []


def test_unknown_exception_wrapped_not_retried(no_sleep):
    sleeps, sleep = no_sleep
    fn = Flaky([ValueError("bad pem")])
    with pytest.raises(ExchangeError) as info:
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert info.value.status_code is None
    assert isinstance(info.value.__cause__, ValueError)
    assert not is_retryable(ValueError())


def test_backoff_is_bounded():
    p = RetryPolicy(max_retries=10, backoff_base_s=1, backoff_cap_s=30)
    assert p.delay(0, rng=lambda: 1.0) == 1
    assert p.delay(3, rng=lambda: 1.0) == 8
    assert p.delay(20, rng=lambda: 1.0) == 30
    assert p.delay(20, rng=lambda: 0.0) == 0


def test_throttle_spaces_calls():
    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    t = Throttle(2.0, clock=lambda: now[0], sleep=sleep)
    t.wait()
    t.wait()
    t.wait()
    assert slept == [0.5, 0.5]
    with pytest.raises(ValueError):
        Throttle(0)
