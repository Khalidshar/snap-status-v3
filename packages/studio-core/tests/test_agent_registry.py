import unittest

from studio_core.observability.agent_registry import (
    CHAIN,
    STATIONS,
    TOOL_IDS,
    command_binding,
    load_registry,
    panel_roles,
    project,
)


class AgentRegistryTests(unittest.TestCase):
    def test_seven_stations_cover_all_eighteen_roles_once(self):
        rows = panel_roles()
        self.assertEqual(len(STATIONS), 7)
        self.assertEqual(len(rows), 18)
        self.assertEqual(len({r["id"] for r in rows}), 18)
        self.assertEqual(len(CHAIN), 12)
        self.assertEqual(len(TOOL_IDS), 6)
        self.assertEqual({r["station"] for r in rows}, {s["id"] for s in STATIONS})
        self.assertEqual([r["id"] for r in rows if r["chain"]], list(CHAIN))

    def test_unbound_command_is_not_promoted_by_old_success(self):
        self.assertIsNone(command_binding("snap-bot-v2", "writer"))
        self.assertIsNotNone(command_binding("status-bot-v2", "writer"))
        rows = project("snap-bot-v2", {"writer": {"state": "SUCCEEDED", "at": 10}})
        writer = next(r for r in rows if r["id"] == "writer")
        self.assertEqual(writer["command_binding"], "NOT_BOUND")
        self.assertEqual(writer["command_label"], "غير مربوط")
        self.assertEqual(writer["scope"], "production_source_checks")
        self.assertFalse(writer["accepted"])

    def test_binding_does_not_claim_run_and_apps_remain_isolated(self):
        snap = project("snap-bot-v2", {"design": {"state": "SUCCEEDED", "at": 10}})
        status = project("status-bot-v2", {})
        self.assertEqual(next(r for r in snap if r["id"] == "design")["state"], "SUCCEEDED")
        self.assertEqual(next(r for r in status if r["id"] == "design")["state"], "UNKNOWN")
        self.assertTrue(all(not r["accepted"] for r in snap + status))
        with self.assertRaises(ValueError):
            project("foreign", {})

    def test_registry_reads_are_independent_and_have_readable_arabic(self):
        first = load_registry()
        first["agents"][0]["name"] = "changed"
        self.assertNotEqual(load_registry()["agents"][0]["name"], "changed")
        self.assertTrue(all("\ufffd" not in r["name"] for r in panel_roles()))


if __name__ == "__main__":
    unittest.main()
