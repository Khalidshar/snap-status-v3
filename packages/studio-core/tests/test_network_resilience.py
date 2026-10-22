"""Retry, circuit breaker and cache (§8). Ported from the lab with the same guards."""

import random

import pytest

from studio_core.network import (
    CircuitBreaker,
    NetworkError,
    NetworkResponse,
    ResilientClient,
    RetryPolicy,
    TTLCache,
    transient,
)

OK = NetworkResponse(200, "application/json", b"{}", "0" * 64)


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class FakeClient:
    """Answers from a script: an exception instance is raised, anything else returned."""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def get(self, url: str) -> NetworkResponse:
        self.calls.append(url)
        item = self.script.pop(0) if self.script else OK
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, NetworkResponse)
        return item


class Harness:
    def __init__(self) -> None:
        self.clock = Clock()
        self.waits: list[float] = []
        self.breakers: dict[str, CircuitBreaker] = {}
        self.cache = TTLCache(clock=self.clock)

    def client(
        self, *script: object, cache_ttl: float = 0, retry: RetryPolicy | None = None
    ) -> tuple[FakeClient, ResilientClient]:
        fake = FakeClient(*script)
        resilient = ResilientClient(
            fake,
            app="snap-bot",
            retry=retry,
            cache=self.cache,
            cache_ttl=cache_ttl,
            breaker_for=lambda host: self.breakers.setdefault(
                host, CircuitBreaker(clock=self.clock)
            ),
            sleep=self.waits.append,
            rng=random.Random(7),  # noqa: S311 - deterministic jitter in tests
        )
        return fake, resilient


@pytest.fixture
def h() -> Harness:
    return Harness()


def test_transient_failures_retry_with_growing_jittered_waits(h: Harness) -> None:
    fake, client = h.client(NetworkError("FETCH_FAILED"), NetworkError("UPSTREAM_HTTP_503"))
    try:
        assert client.get("https://a.example/x") is OK
    except NetworkError as error:
        pytest.fail(f"guard:retry-transient {error}")
    assert len(fake.calls) == 3, "guard:retry-transient"
    assert 1 <= h.waits[0] <= 1.5 and 2 <= h.waits[1] <= 3, f"guard:backoff {h.waits}"


def test_attempts_are_capped_at_three(h: Harness) -> None:
    fake, client = h.client(*[NetworkError("TimeoutError")] * 5)
    with pytest.raises(NetworkError, match="TimeoutError"):
        client.get("https://a.example/x")
    assert len(fake.calls) == 3, "guard:attempt-cap"
    assert len(h.waits) == 2


@pytest.mark.parametrize(
    "code",
    [
        "HOST_NOT_ALLOWED",
        "NON_PUBLIC_ADDRESS",
        "REDIRECT_DENIED",
        "UPSTREAM_HTTP_404",
        "Proxy response integrity failed",
        "PROXY_REJECTED",
        "CIRCUIT_OPEN",
    ],
)
def test_final_errors_are_never_retried(h: Harness, code: str) -> None:
    fake, client = h.client(NetworkError(code))
    with pytest.raises(NetworkError, match=code):
        client.get("https://a.example/x")
    assert len(fake.calls) == 1, "guard:no-retry-final"
    assert h.waits == [], "guard:no-retry-final"


def test_429_and_5xx_are_transient_but_other_4xx_are_not() -> None:
    assert transient(NetworkError("UPSTREAM_HTTP_429"))
    assert transient(NetworkError("UPSTREAM_HTTP_500"))
    assert not transient(NetworkError("UPSTREAM_HTTP_400")), "guard:4xx-final"
    assert not transient(NetworkError("UPSTREAM_HTTP_X"))


def test_wait_budget_stops_retries_early(h: Harness) -> None:
    fake, client = h.client(
        *[NetworkError("FETCH_FAILED")] * 3, retry=RetryPolicy(max_wait_seconds=0.5)
    )
    with pytest.raises(NetworkError):
        client.get("https://a.example/x")
    assert len(fake.calls) == 1, "guard:wait-budget"


def test_breaker_opens_after_five_failed_requests_and_rejects_without_calling(
    h: Harness,
) -> None:
    fake, client = h.client(*[NetworkError("FETCH_FAILED")] * 15)
    for _ in range(5):
        with pytest.raises(NetworkError):
            client.get("https://a.example/x")
    calls = len(fake.calls)
    with pytest.raises(NetworkError, match="CIRCUIT_OPEN"):
        client.get("https://a.example/x")
    assert len(fake.calls) == calls, "guard:open-no-call"
    _, other = h.client()
    assert other.get("https://b.example/x") is OK, "breakers are per host"


def test_half_open_trial_closes_on_success_and_reopens_on_failure(h: Harness) -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=300, clock=h.clock)
    breaker.failure()
    assert breaker.state == "OPEN"
    h.clock.value += 300
    assert breaker.state == "HALF_OPEN"
    breaker.acquire()
    with pytest.raises(NetworkError, match="CIRCUIT_OPEN"):
        breaker.acquire()
    breaker.failure()
    assert breaker.state == "OPEN", "guard:trial-failure-reopens"
    h.clock.value += 300
    breaker.acquire()
    breaker.success()
    assert breaker.state == "CLOSED", "guard:trial-success-closes"


def test_final_errors_and_success_do_not_trip_the_breaker(h: Harness) -> None:
    _, client = h.client(*[NetworkError("HOST_NOT_ALLOWED")] * 6)
    for _ in range(6):
        with pytest.raises(NetworkError):
            client.get("https://a.example/x")
    assert h.breakers["a.example"].state == "CLOSED", "guard:policy-not-counted"
    breaker = CircuitBreaker(clock=h.clock)
    for _ in range(4):
        breaker.failure()
    breaker.success()
    breaker.failure()
    assert breaker.state == "CLOSED", "guard:success-resets"


def test_cache_serves_within_ttl_only_and_never_caches_failures(h: Harness) -> None:
    fake, client = h.client(NetworkError("HOST_NOT_ALLOWED"), OK, cache_ttl=60)
    with pytest.raises(NetworkError):
        client.get("https://a.example/x")
    assert client.get("https://a.example/x") is OK
    assert client.get("https://a.example/x") is OK
    assert len(fake.calls) == 2, "guard:cache-hit"
    h.clock.value += 61
    client.get("https://a.example/x")
    assert len(fake.calls) == 3, "guard:cache-expiry"
    fake2, plain = h.client()
    plain.get("https://c.example/x")
    plain.get("https://c.example/x")
    assert len(fake2.calls) == 2, "cache off by default"


def test_cache_evicts_the_soonest_expiring_entry_when_full(h: Harness) -> None:
    cache = TTLCache(max_entries=2, clock=h.clock)
    cache.put("a", OK, 10)
    cache.put("b", OK, 60)
    cache.put("c", OK, 60)
    assert cache.get("a") is None and cache.get("b") is OK and cache.get("c") is OK
    cache.put("d", OK, 0)
    assert cache.get("d") is None, "a zero TTL never stores"


@pytest.mark.parametrize(
    "values",
    [{"attempts": 0}, {"attempts": 9}, {"base_seconds": 0}, {"jitter": 2}],
)
def test_invalid_policies_are_refused(values: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**values)  # type: ignore[arg-type]


def test_invalid_breaker_is_refused() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


def test_default_client_shares_one_breaker_per_app_and_host() -> None:
    from studio_core.network import resilience

    first = ResilientClient(FakeClient(), app="v3-shared-test")
    second = ResilientClient(FakeClient(), app="v3-shared-test")
    assert first.breaker_for("x.example") is second.breaker_for("x.example")
    assert first.breaker_for("x.example") is not first.breaker_for("y.example")
    assert first.cache is resilience._CACHE
