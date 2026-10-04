"""Client-side pacing so we stay under a provider's published limits."""

import threading
import time
from collections import deque
from typing import Callable

from trader.data_providers.errors import ProviderRateLimited

HTTP_TOO_MANY_REQUESTS = 429
MAX_RETRY_AFTER_SECS = 60.0


class RateLimiter:
    """At most `calls` acquisitions in any `period`-second window, across threads."""

    def __init__(self, calls: int, period: float, clock=time.monotonic, sleep=time.sleep):
        self._calls = calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._recent and now - self._recent[0] >= self._period:
                    self._recent.popleft()
                if len(self._recent) < self._calls:
                    self._recent.append(now)
                    return
                wait = self._period - (now - self._recent[0])
            self._sleep(wait)


def call_with_retry(send: Callable, *, provider: str, max_tries: int = 3,
                    base_delay: float = 1.0, sleep=time.sleep):
    for attempt in range(1, max_tries + 1):
        response = send()
        if response.status_code != HTTP_TOO_MANY_REQUESTS:
            return response
        if attempt == max_tries:
            break
        sleep(_retry_delay(response, base_delay, attempt))
    raise ProviderRateLimited(f'{provider} rate limit hit after {max_tries} tries; retry later')


def _retry_delay(response, base_delay: float, attempt: int) -> float:
    retry_after = response.headers.get('Retry-After', '')
    if retry_after.isdigit():
        return min(float(retry_after), MAX_RETRY_AFTER_SECS)
    return base_delay * 2 ** (attempt - 1)
