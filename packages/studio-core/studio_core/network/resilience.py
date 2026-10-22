"""Retry, circuit breaker and TTL cache around NetworkClient (agreed design §8).

Only transient transport failures are retried. Policy denials, integrity
failures and ordinary 4xx answers are final and never retried. A breaker
opens per (application, host) after consecutive failed requests and rejects
calls until its cooldown ends; then one half-open trial decides. Only GET
requests exist on this path, so a retry never repeats a side effect.

Graduated from the lab core 0.10.0 (snap-bot-v2 #51) with types added; the
behaviour is unchanged.
"""

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from studio_core.network.client import NetworkError, NetworkResponse

# Transport failures that may succeed on a later attempt.
TRANSIENT = frozenset(
    {
        "FETCH_FAILED",
        "TimeoutError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "RemoteDisconnected",
        "IncompleteRead",
        "FileNotFoundError",
    }
)

Clock = Callable[[], float]


class Getter(Protocol):
    def get(self, url: str) -> NetworkResponse: ...


def transient(error: BaseException) -> bool:
    code = str(error)
    if code in TRANSIENT:
        return True
    if code.startswith("UPSTREAM_HTTP_"):
        status = code.rsplit("_", 1)[-1]
        return status.isdigit() and (status == "429" or 500 <= int(status) <= 599)
    return False


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_seconds: float = 1.0
    jitter: float = 0.5
    max_wait_seconds: float = 10.0

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or not 1 <= self.attempts <= 5:
            raise ValueError("Invalid retry attempts")
        if (
            not 0 < self.base_seconds <= 10
            or not 0 <= self.jitter <= 1
            or not 0 <= self.max_wait_seconds <= 60
        ):
            raise ValueError("Invalid retry timing")

    def delay(self, retry: int, rng: random.Random) -> float:
        """Exponential backoff (1s, 2s, 4s...) plus up to `jitter` of that delay."""
        base = self.base_seconds * 2.0**retry
        return base + rng.uniform(0, self.jitter * base)


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 300,
        clock: Clock = time.monotonic,
    ) -> None:
        if (
            type(failure_threshold) is not int
            or failure_threshold < 1
            or not 0 < cooldown_seconds <= 3600
        ):
            raise ValueError("Invalid breaker policy")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.trial = False
        self.lock = threading.Lock()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return self.CLOSED
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            return self.HALF_OPEN
        return self.OPEN

    def acquire(self) -> None:
        """Admit a request, or raise CIRCUIT_OPEN. Half-open admits a single trial."""
        with self.lock:
            state = self.state
            if state == self.OPEN or (state == self.HALF_OPEN and self.trial):
                raise NetworkError("CIRCUIT_OPEN")
            if state == self.HALF_OPEN:
                self.trial = True

    def success(self) -> None:
        with self.lock:
            self.failures, self.opened_at, self.trial = 0, None, False

    def failure(self) -> None:
        with self.lock:
            self.trial = False
            if self.opened_at is not None:
                self.opened_at = self.clock()
                return
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = self.clock()

    def release(self) -> None:
        """End a half-open trial that hit a final, non-transport error."""
        with self.lock:
            self.trial = False


class TTLCache:
    def __init__(self, max_entries: int = 128, clock: Clock = time.monotonic) -> None:
        self.max_entries = max_entries
        self.clock = clock
        self.entries: dict[object, tuple[float, NetworkResponse]] = {}
        self.lock = threading.Lock()

    def get(self, key: object) -> NetworkResponse | None:
        with self.lock:
            item = self.entries.get(key)
            if item and self.clock() < item[0]:
                return item[1]
            self.entries.pop(key, None)
            return None

    def put(self, key: object, value: NetworkResponse, ttl: float) -> None:
        if ttl <= 0:
            return
        with self.lock:
            if len(self.entries) >= self.max_entries and key not in self.entries:
                self.entries.pop(min(self.entries, key=lambda k: self.entries[k][0]))
            self.entries[key] = (self.clock() + ttl, value)


# Breakers and cache outlive one job inside a long-running worker process.
_SHARED_LOCK = threading.Lock()
_BREAKERS: dict[tuple[str, str], CircuitBreaker] = {}
_CACHE = TTLCache()


def shared_breaker(app: str, host: str) -> CircuitBreaker:
    with _SHARED_LOCK:
        return _BREAKERS.setdefault((app, host), CircuitBreaker())


class ResilientClient:
    """Same get(url) contract as NetworkClient, plus retry, breaker and cache."""

    def __init__(
        self,
        client: Getter,
        *,
        app: str,
        retry: RetryPolicy | None = None,
        breaker_for: Callable[[str], CircuitBreaker] | None = None,
        cache: TTLCache | None = None,
        cache_ttl: float = 0,
        sleep: Callable[[float], object] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.client = client
        self.app = app
        self.retry = retry or RetryPolicy()
        self.breaker_for = breaker_for or (lambda host: shared_breaker(self.app, host))
        self.cache = _CACHE if cache is None else cache
        self.cache_ttl = cache_ttl
        self.sleep = sleep
        # Jitter only spreads retries; it is not a security value.
        self.rng = rng or random.Random()  # noqa: S311

    def get(self, url: str) -> NetworkResponse:
        key = (self.app, url)
        if self.cache_ttl > 0:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        breaker = self.breaker_for(urlsplit(url).hostname or "")
        breaker.acquire()
        waited = 0.0
        for attempt in range(self.retry.attempts):
            try:
                response = self.client.get(url)
            except NetworkError as error:
                if not transient(error):
                    breaker.release()
                    raise
                delay = self.retry.delay(attempt, self.rng)
                if (
                    attempt + 1 >= self.retry.attempts
                    or waited + delay > self.retry.max_wait_seconds
                ):
                    breaker.failure()
                    raise
                self.sleep(delay)
                waited += delay
                continue
            breaker.success()
            self.cache.put(key, response, self.cache_ttl)
            return response
        raise NetworkError("RETRY_EXHAUSTED")  # Unreachable: the loop returns or raises.
