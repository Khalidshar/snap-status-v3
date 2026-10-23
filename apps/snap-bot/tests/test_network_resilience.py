import random
import unittest
from unittest.mock import patch

from studio_core.network import NetworkError, NetworkResponse
from studio_core.network.resilience import (
    CircuitBreaker,
    ResilientClient,
    RetryPolicy,
    TTLCache,
    transient,
)

OK = NetworkResponse(200, "application/json", b"{}", "0" * 64)


def compact(text):
    """Code shape without formatting: no whitespace, one quote style, no trailing commas."""
    import re

    return re.sub(r",(?=[)\]])", "", re.sub(r"\s+", "", text).replace('"', "'"))


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class FakeClient:
    """Answers from a script: an exception instance is raised, anything else returned."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def get(self, url):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else OK
        if isinstance(item, Exception):
            raise item
        return item


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.waits = Clock(), []
        self.breakers = {}
        self.cache = TTLCache(clock=self.clock)

    def client(self, *script, cache_ttl=0, retry=RetryPolicy()):  # noqa: B008  # frozen policy
        fake = FakeClient(*script)
        resilient = ResilientClient(
            fake,
            app="snap-bot-v2",
            retry=retry,
            cache=self.cache,
            cache_ttl=cache_ttl,
            breaker_for=lambda host: self.breakers.setdefault(
                host, CircuitBreaker(clock=self.clock)
            ),
            sleep=self.waits.append,
            rng=random.Random(7),
        )
        return fake, resilient

    def test_transient_failures_retry_with_growing_jittered_waits(self):
        fake, client = self.client(NetworkError("FETCH_FAILED"), NetworkError("UPSTREAM_HTTP_503"))
        try:
            self.assertIs(client.get("https://a.example/x"), OK)
        except NetworkError as error:
            self.fail(f"guard:retry-transient {error}")
        self.assertEqual(len(fake.calls), 3, "guard:retry-transient")
        self.assertTrue(
            1 <= self.waits[0] <= 1.5 and 2 <= self.waits[1] <= 3, f"guard:backoff {self.waits}"
        )

    def test_attempts_are_capped_at_three(self):
        fake, client = self.client(*[NetworkError("TimeoutError")] * 5)
        with self.assertRaisesRegex(NetworkError, "TimeoutError", msg="guard:attempt-cap"):
            client.get("https://a.example/x")
        self.assertEqual(len(fake.calls), 3, "guard:attempt-cap")
        self.assertEqual(len(self.waits), 2)

    def test_final_errors_are_never_retried(self):
        for code in (
            "HOST_NOT_ALLOWED",
            "NON_PUBLIC_ADDRESS",
            "REDIRECT_DENIED",
            "UPSTREAM_HTTP_404",
            "Proxy response integrity failed",
            "PROXY_REJECTED",
            "CIRCUIT_OPEN",
        ):
            with self.subTest(code=code):
                fake, client = self.client(NetworkError(code))
                with self.assertRaisesRegex(NetworkError, code, msg="guard:no-retry-final"):
                    client.get("https://a.example/x")
                self.assertEqual(len(fake.calls), 1, "guard:no-retry-final")
        self.assertEqual(self.waits, [], "guard:no-retry-final")

    def test_429_and_5xx_are_transient_but_other_4xx_are_not(self):
        self.assertTrue(transient(NetworkError("UPSTREAM_HTTP_429")))
        self.assertTrue(transient(NetworkError("UPSTREAM_HTTP_500")))
        self.assertFalse(transient(NetworkError("UPSTREAM_HTTP_400")), "guard:4xx-final")
        self.assertFalse(transient(NetworkError("UPSTREAM_HTTP_X")))

    def test_wait_budget_stops_retries_early(self):
        fake, client = self.client(
            *[NetworkError("FETCH_FAILED")] * 3, retry=RetryPolicy(max_wait_seconds=0.5)
        )
        with self.assertRaises(NetworkError):
            client.get("https://a.example/x")
        self.assertEqual(len(fake.calls), 1, "guard:wait-budget")

    def test_breaker_opens_after_five_failed_requests_and_rejects_without_calling(self):
        fake, client = self.client(*[NetworkError("FETCH_FAILED")] * 15)
        for _ in range(5):
            with self.assertRaises(NetworkError):
                client.get("https://a.example/x")
        calls = len(fake.calls)
        with self.assertRaisesRegex(NetworkError, "CIRCUIT_OPEN", msg="guard:breaker-opens"):
            client.get("https://a.example/x")
        self.assertEqual(len(fake.calls), calls, "guard:open-no-call")
        _, other = self.client()
        self.assertIs(other.get("https://b.example/x"), OK, "breakers are per host")

    def test_half_open_trial_closes_on_success_and_reopens_on_failure(self):
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=300, clock=self.clock)
        breaker.failure()
        self.assertEqual(breaker.state, "OPEN")
        self.clock.value += 300
        self.assertEqual(breaker.state, "HALF_OPEN")
        breaker.acquire()
        with self.assertRaisesRegex(NetworkError, "CIRCUIT_OPEN", msg="guard:single-trial"):
            breaker.acquire()
        breaker.failure()
        self.assertEqual(breaker.state, "OPEN", "guard:trial-failure-reopens")
        self.clock.value += 300
        breaker.acquire()
        breaker.success()
        self.assertEqual(breaker.state, "CLOSED", "guard:trial-success-closes")

    def test_final_errors_and_success_do_not_trip_the_breaker(self):
        _fake, client = self.client(*[NetworkError("HOST_NOT_ALLOWED")] * 6)
        for _ in range(6):
            with self.assertRaises(NetworkError):
                client.get("https://a.example/x")
        self.assertEqual(self.breakers["a.example"].state, "CLOSED", "guard:policy-not-counted")
        breaker = CircuitBreaker(clock=self.clock)
        for _ in range(4):
            breaker.failure()
        breaker.success()
        breaker.failure()
        self.assertEqual(breaker.state, "CLOSED", "guard:success-resets")

    def test_cache_serves_within_ttl_only_and_never_caches_failures(self):
        fake, client = self.client(NetworkError("HOST_NOT_ALLOWED"), OK, cache_ttl=60)
        with self.assertRaises(NetworkError):
            client.get("https://a.example/x")
        self.assertIs(client.get("https://a.example/x"), OK)
        self.assertIs(client.get("https://a.example/x"), OK)
        self.assertEqual(len(fake.calls), 2, "guard:cache-hit")
        self.clock.value += 61
        client.get("https://a.example/x")
        self.assertEqual(len(fake.calls), 3, "guard:cache-expiry")
        fake2, plain = self.client()
        plain.get("https://c.example/x")
        plain.get("https://c.example/x")
        self.assertEqual(len(fake2.calls), 2, "cache off by default")

    def test_invalid_policies_are_refused(self):
        for values in ({"attempts": 0}, {"attempts": 9}, {"base_seconds": 0}, {"jitter": 2}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RetryPolicy(**values)
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0)


class WeatherFallbackTests(unittest.TestCase):
    def test_open_circuit_on_primary_falls_back_to_the_second_source(self):
        from snap_bot.weather import fetch_weather

        settings = {
            "city": "الرياض",
            "ttl_seconds": 3600,
            "sources": [
                {"id": "open-meteo", "url": "https://api.open-meteo.com/x"},
                {"id": "met-no", "url": "https://api.met.no/y"},
            ],
        }
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.failure()
        fake = FakeClient()
        client = ResilientClient(
            fake,
            app="snap-bot-v2",
            cache=TTLCache(),
            sleep=lambda s: None,
            breaker_for=lambda host: breaker if host == "api.open-meteo.com" else CircuitBreaker(),
        )
        with patch("studio_core.weather.parse", return_value={"source": "met-no"}):
            content, failures = fetch_weather(client, settings, 0)
        self.assertEqual(content, {"source": "met-no"})
        self.assertEqual(
            failures,
            [{"source": "open-meteo", "error": "NetworkError"}],
            "guard:fallback-on-open-circuit",
        )
        self.assertEqual(fake.calls, ["https://api.met.no/y"])

    def test_execute_wraps_the_weather_client(self):
        from pathlib import Path

        source = Path(__import__("snap_bot.service", fromlist=["x"]).__file__).read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "fetch_weather(ResilientClient(NetworkClient(config),app=config.app",
            compact(source),
            "guard:weather-wired",
        )


if __name__ == "__main__":
    unittest.main()
