import pytest

from trader.data_providers.errors import ProviderRateLimited
from trader.data_providers.rate_limit import RateLimiter, call_with_retry


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_limiter_allows_burst_up_to_limit_without_sleeping():
    fake = FakeClock()
    limiter = RateLimiter(3, 60.0, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        limiter.acquire()
    assert fake.sleeps == []


def test_limiter_waits_for_oldest_call_to_leave_window():
    fake = FakeClock()
    limiter = RateLimiter(2, 60.0, clock=fake.clock, sleep=fake.sleep)
    limiter.acquire()
    fake.now = 10.0
    limiter.acquire()
    limiter.acquire()
    assert fake.sleeps == [50.0]


def test_retry_returns_first_non_429_response():
    responses = iter([FakeResponse(429), FakeResponse(200)])
    fake = FakeClock()
    result = call_with_retry(lambda: next(responses), provider='alpaca', sleep=fake.sleep)
    assert result.status_code == 200
    assert fake.sleeps == [1.0]


def test_retry_honours_retry_after_header():
    responses = iter([FakeResponse(429, {'Retry-After': '7'}), FakeResponse(200)])
    fake = FakeClock()
    call_with_retry(lambda: next(responses), provider='alpaca', sleep=fake.sleep)
    assert fake.sleeps == [7.0]


def test_retry_gives_up_with_rate_limited_error():
    fake = FakeClock()
    with pytest.raises(ProviderRateLimited, match='alpaca'):
        call_with_retry(lambda: FakeResponse(429), provider='alpaca', max_tries=3, sleep=fake.sleep)
    assert fake.sleeps == [1.0, 2.0]


def test_non_429_errors_are_not_retried():
    calls = []

    def send():
        calls.append(1)
        return FakeResponse(500)

    assert call_with_retry(send, provider='alpaca', sleep=lambda s: None).status_code == 500
    assert len(calls) == 1
