import unittest
from unittest.mock import patch

from snap_bot.agent_telemetry import TelemetryUnavailable, observe


class ObservationTests(unittest.TestCase):
    def test_preserves_result_and_callable_count(self):
        sentinel = object()
        calls = []
        with patch("snap_bot.agent_telemetry._record") as recorder:
            result = observe(object(), "quality", "job1", lambda: calls.append(1) or sentinel)
        self.assertIs(result, sentinel)
        self.assertEqual(calls, [1])
        events = [call.args[1] for call in recorder.call_args_list]
        self.assertEqual([e["status"] for e in events], ["RUNNING", "SUCCEEDED"])
        self.assertEqual(events[0]["run"], events[1]["run"])

    def test_original_exception_propagates_without_its_message(self):
        error = ValueError("PRIVATE TOKEN")

        def fail():
            raise error

        with patch("snap_bot.agent_telemetry._record") as recorder:
            with self.assertRaises(ValueError) as caught:
                observe(object(), "quality", "job2", fail)
        self.assertIs(caught.exception, error)
        self.assertNotIn("PRIVATE TOKEN", str(recorder.call_args_list))
        self.assertEqual(recorder.call_args_list[-1].args[1]["status"], "FAILED")

    def test_missing_journal_prevents_unobserved_execution(self):
        calls = []
        with patch(
            "snap_bot.agent_telemetry._record", side_effect=TelemetryUnavailable("unavailable")
        ):
            with self.assertRaises(TelemetryUnavailable):
                observe(object(), "quality", "job3", lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_failure_after_computation_is_not_success(self):
        with patch(
            "snap_bot.agent_telemetry._record",
            side_effect=[None, TelemetryUnavailable("unavailable")],
        ):
            with self.assertRaises(TelemetryUnavailable):
                observe(object(), "quality", "job4", lambda: 42)


if __name__ == "__main__":
    import json
    import time
    from pathlib import Path
    from types import SimpleNamespace

    config = SimpleNamespace(
        data_root=Path("/var/lib/studio-v2-snap-weather/madar-integration-probe"), app="snap-bot-v2"
    )
    job = "observer-integration-" + str(time.time_ns())
    assert observe(config, "quality", job, lambda: 42) == 42
    journal = config.data_root / "madar/snap-bot-v2/events.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    selected = [event for event in events if event["job"] == job]
    assert [event["status"] for event in selected] == ["RUNNING", "SUCCEEDED"]
    print(
        json.dumps(
            {
                "integration": "PASS",
                "network_policy": "AF_UNIX",
                "application": config.app,
                "events": len(selected),
            }
        )
    )
