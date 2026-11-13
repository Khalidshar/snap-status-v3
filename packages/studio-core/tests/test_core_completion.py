"""Shared core acceptance tests: real SQLite/WAL, no external transport."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

from studio_core.observability.agent_inventory import APPLICATIONS, ROLE_IDS, inventory

ROOT = Path(__file__).parents[1]
REVISION = "a" * 40


def registry():
    return {
        "agents": [
            {
                "id": role,
                "applications": list(APPLICATIONS),
                "state_isolated_per_application": True,
                "execution_policy": "WHEN_APPLICABLE",
                "acceptance_status": "VERIFIED",
                "error": "private-example-input",
            }
            for role in ROLE_IDS
        ]
    }


class InventoryTests(unittest.TestCase):
    def test_two_apps_have_thirty_six_distinct_unaccepted_identities(self):
        snap = inventory(
            registry(),
            APPLICATIONS[0],
            REVISION,
            {"fact_source": {"scope": "weather", "kind": "OBSERVER"}},
        )
        status = inventory(registry(), APPLICATIONS[1], REVISION, {})
        rows = snap["agents"] + status["agents"]
        self.assertEqual(len(rows), 36)
        self.assertEqual(len({row["instance_id"] for row in rows}), 36)
        self.assertTrue(all(row["function_acceptance"] == "NOT_VERIFIED" for row in rows))
        self.assertTrue(all(row["runtime_state"] == "UNKNOWN" for row in rows))
        self.assertFalse(snap["production_ready"])
        self.assertFalse(status["delivery_authority"])
        self.assertEqual(
            next(row for row in status["agents"] if row["role_id"] == "fact_source")["binding"],
            "NOT_BOUND",
        )
        self.assertNotIn("private-example-input", json.dumps(rows))

    def test_inputs_are_not_mutated_and_output_hash_is_reproducible(self):
        source = registry()
        original = copy.deepcopy(source)
        bindings = {"arabic": {"scope": "did_you_know", "kind": "LOCAL_RULE_ADAPTER"}}
        result = inventory(source, APPLICATIONS[0], REVISION, bindings)
        self.assertEqual(result, inventory(source, APPLICATIONS[0], REVISION, bindings))
        self.assertEqual(source, original)
        digest = result.pop("sha256")
        self.assertEqual(
            digest,
            hashlib.sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def test_missing_duplicate_renamed_and_cross_app_roles_fail(self):
        variants = []
        value = registry()
        value["agents"].pop()
        variants.append(value)
        value = registry()
        value["agents"][0]["id"] = ROLE_IDS[1]
        variants.append(value)
        value = registry()
        value["agents"][0]["id"] = "new_role"
        variants.append(value)
        value = registry()
        value["agents"][0]["applications"] = [APPLICATIONS[0]]
        variants.append(value)
        value = registry()
        value["agents"][0]["state_isolated_per_application"] = False
        variants.append(value)
        value = registry()
        value["agents"][0]["execution_policy"] = "ALWAYS"
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                inventory(value, APPLICATIONS[0], REVISION, {})

    def test_forged_binding_acceptance_and_unknown_fields_fail(self):
        for bindings in (
            {"new_role": {}},
            {"voice": {"scope": "weather", "kind": "AI"}},
            {"voice": {"scope": "weather", "kind": "OBSERVER", "accepted": True}},
            {"voice": {"scope": "private/token", "kind": "OBSERVER"}},
            {"voice": {"scope": None, "kind": "OBSERVER"}},
        ):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                inventory(registry(), APPLICATIONS[0], REVISION, bindings)

    def test_bad_identity_revision_and_registry_fail(self):
        for app, revision, source in (
            ("legacy", REVISION, registry()),
            (APPLICATIONS[0], "main", registry()),
            (APPLICATIONS[0], REVISION, {}),
            (APPLICATIONS[0], REVISION, None),
        ):
            with self.subTest(app=app), self.assertRaises(ValueError):
                inventory(source, app, revision, {})


if __name__ == "__main__":
    unittest.main()
