"""Consumer tests for durable ownership, no replay, component locks and monitor."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository
from studio_core.jobs.component_lock import ComponentBusy, ComponentLocks, ComponentOwnershipLost
from studio_core.observability.agent_runs import (
    AgentBlocked,
    AgentBudgetExceeded,
    AgentContract,
    AgentRejected,
    digest,
    run_agent,
)
from studio_core.observability.watchdog import inspect_runtime, main


class CoreAgentRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.cfg = replace(
            Config.load(Path(__file__).parent / "fixtures/foundation.toml"),
            data_root=self.root / "data",
        )
        self.repo = Repository(self.cfg, clock=lambda: self.now)
        self.job, _ = self.repo.enqueue(
            "runtime", "owner-hash", "demo", "NORMAL", "input-hash", self.now + 600
        )
        self.claim = self.repo.claim(self.job["id"])
        self.plan = digest({"scope": "runtime-fixture-v1"})
        self.stage = AgentContract(self.cfg.app, "demo", "fact_source", "1.0.0", self.plan)

    def run_stage(self, stage=None, payload=None, callback=None, **kwargs):
        payload = {"fixture": "input"} if payload is None else payload
        return run_agent(
            self.repo,
            self.job["id"],
            self.claim["lease_token"],
            self.claim["attempt"],
            stage or self.stage,
            payload,
            callback or (lambda: {"fixture": "output"}),
            kwargs.pop("validate_input", lambda value: True),
            kwargs.pop("validate_output", lambda value, original: True),
            **kwargs,
        )

    def rows(self):
        with self.repo.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agent_runs")]

    def test_success_records_hashes_and_duration_without_payloads(self):
        self.run_stage(
            payload={"credential_field": "PRIVATE_FIXTURE"},
            callback=lambda: {"private": "PRIVATE_OUTPUT"},
        )
        row = self.rows()[0]
        self.assertEqual(row["status"], "SUCCEEDED")
        self.assertEqual(row["input_sha"], digest({"credential_field": "PRIVATE_FIXTURE"}))
        self.assertEqual(row["output_sha"], digest({"private": "PRIVATE_OUTPUT"}))
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(row))
        self.assertNotIn("PRIVATE_OUTPUT", json.dumps(row))
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["version"], "1.0.0")
        self.assertGreaterEqual(row["duration_ms"], 0)

    def test_replay_never_calls_callback(self):
        self.run_stage()
        callback = Mock()
        with self.assertRaises(AgentBlocked):
            self.run_stage(callback=callback)
        callback.assert_not_called()

    def test_failed_callback_is_durable_and_not_retried(self):
        def fail():
            raise RuntimeError("PRIVATE_ERROR_DETAIL")

        with self.assertRaises(RuntimeError):
            self.run_stage(callback=fail)
        self.assertEqual(self.rows()[0]["status"], "FAILED")
        self.assertNotIn("PRIVATE_ERROR_DETAIL", json.dumps(self.rows()))
        with self.assertRaises(AgentBlocked):
            self.run_stage()

    def test_invalid_input_does_not_call_callback(self):
        callback = Mock()
        with self.assertRaises(AgentRejected):
            self.run_stage(callback=callback, validate_input=lambda value: False)
        callback.assert_not_called()

    def test_invalid_output_records_failure(self):
        with self.assertRaises(AgentRejected):
            self.run_stage(validate_output=lambda value, original: False)
        self.assertEqual(self.rows()[0]["error_code"], "AGENT_OUTPUT_INVALID")

    def test_callback_mutating_input_cannot_forge_lineage(self):
        payload = {"fixture": "original"}

        def mutate():
            payload["fixture"] = "changed"
            return payload

        with self.assertRaises(AgentRejected):
            self.run_stage(payload=payload, callback=mutate)
        self.assertEqual(self.rows()[0]["status"], "FAILED")

    def test_missing_upstream_blocks_callback(self):
        callback = Mock()
        stage = replace(self.stage, role="arabic", dependencies=("fact_source",))
        with self.assertRaises(AgentBlocked):
            self.run_stage(stage, callback=callback)
        callback.assert_not_called()

    def test_dependency_digest_binds_actual_output(self):
        output = self.run_stage()
        stage = replace(self.stage, role="arabic", dependencies=("fact_source",))
        with self.assertRaises(AgentBlocked):
            self.run_stage(stage, payload={"foreign": "input"})
        result = self.run_stage(stage, payload=output, callback=lambda: output)
        self.assertEqual(result, output)

    def test_changed_plan_rejects_upstream(self):
        output = self.run_stage()
        stage = replace(
            self.stage, role="arabic", dependencies=("fact_source",), contract_sha="a" * 64
        )
        with self.assertRaises(AgentBlocked):
            self.run_stage(stage, payload=output)

    def test_failed_upstream_blocks_downstream(self):
        with self.assertRaises(AgentRejected):
            self.run_stage(validate_output=lambda *_: False)
        stage = replace(self.stage, role="arabic", dependencies=("fact_source",))
        with self.assertRaises(AgentBlocked):
            self.run_stage(stage)

    def test_budget_rejects_late_output_without_sleep(self):
        def late():
            self.now += 2
            return {"fixture": "late"}

        with self.assertRaises(AgentBudgetExceeded):
            self.run_stage(replace(self.stage, budget_seconds=1), callback=late)
        self.assertEqual(self.rows()[0]["status"], "FAILED")
        self.assertIsNone(self.rows()[0]["output_sha"])

    def test_output_validation_cannot_commit_success_after_stage_deadline(self):
        def slow_validation(value, original):
            self.now += 2
            return True

        with self.assertRaises(AgentBudgetExceeded):
            self.run_stage(replace(self.stage, budget_seconds=1), validate_output=slow_validation)
        row = self.rows()[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["error_code"], "AGENT_BUDGET_EXCEEDED")
        self.assertIsNone(row["output_sha"])

    def test_stale_owner_cannot_record_success(self):
        def lose():
            self.now += self.cfg.lease_seconds + 1
            return {"fixture": "late"}

        with self.assertRaises(AgentBudgetExceeded):
            self.run_stage(callback=lose)
        self.assertEqual(self.rows()[0]["status"], "RUNNING")
        monitor = inspect_runtime(self.repo.path, self.cfg.app, now=self.now)
        self.assertIn("AGENT_STALE", {item["kind"] for item in monitor["incidents"]})

    def test_reclaimed_epoch_cannot_adopt_late_success(self):
        def reclaim():
            self.now += self.cfg.lease_seconds + 1
            self.new_claim = self.repo.claim(self.job["id"])
            return {"fixture": "late"}

        with self.assertRaises(AgentBudgetExceeded):
            self.run_stage(callback=reclaim)
        self.assertEqual(self.rows()[0]["status"], "RUNNING")
        self.assertEqual(self.new_claim["attempt"], 2)
        with self.assertRaises(LeaseLost):
            self.run_stage()

    def test_foreign_app_or_command_is_rejected_before_callback(self):
        callback = Mock()
        foreign = "status-bot-v2" if self.cfg.app == "snap-bot-v2" else "snap-bot-v2"
        for stage in (replace(self.stage, app=foreign), replace(self.stage, command="weather")):
            with self.assertRaises(AgentRejected):
                self.run_stage(stage, callback=callback)
        callback.assert_not_called()

    def test_nan_and_oversized_payload_rejected(self):
        for value in ({"value": float("nan")}, {"value": "x" * (1024 * 1024)}):
            with self.assertRaises(AgentRejected):
                self.run_stage(payload=value)

    def test_concurrent_workers_invoke_callback_exactly_once(self):
        callbacks = []

        def work():
            try:
                return self.run_stage(
                    callback=lambda: callbacks.append("ran") or {"fixture": "one"}
                )
            except AgentBlocked:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: work(), range(2)))
        self.assertEqual(callbacks, ["ran"])
        self.assertIn("blocked", results)

    def test_all_fixed_role_contracts_are_supported_without_acceptance(self):
        from studio_core.observability.agent_inventory import ROLE_IDS

        for role in ROLE_IDS:
            replace(self.stage, role=role)
        with self.assertRaises(AgentRejected):
            replace(self.stage, role="new_identity")
        with self.assertRaises(AgentRejected):
            replace(self.stage, budget_seconds=True)

    def test_component_use_excludes_maintenance_and_repair(self):
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "USE")
        for purpose in ("REPAIR", "MAINTENANCE", "USE"):
            with self.assertRaises(ComponentBusy):
                locks.acquire("render", purpose)
        locks.release(lease)
        with locks.hold("render", "MAINTENANCE"):
            pass

    def test_expiry_is_not_permission_to_steal(self):
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "USE", 1)
        self.now += 2
        with self.assertRaises(ComponentBusy):
            locks.acquire("render", "REPAIR")
        with self.assertRaises(ComponentOwnershipLost):
            locks.assert_holder(lease)
        locks.release(lease)  # Only the returning exact holder can attest completion.
        with locks.hold("render", "REPAIR"):
            pass

    def test_wrong_token_cannot_release_component(self):
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "USE")
        with self.assertRaises(ComponentOwnershipLost):
            locks.release(replace(lease, token="foreign"))  # noqa: S106  (synthetic lease token)
        locks.assert_holder(lease)
        locks.release(lease)

    def test_component_lock_is_independent_per_app(self):
        other = "status-bot-v2" if self.cfg.app == "snap-bot-v2" else "snap-bot-v2"
        cfg = replace(self.cfg, app=other, data_root=self.root / "other")
        other_locks = ComponentLocks(Repository(cfg, clock=lambda: self.now))
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "USE")
        with other_locks.hold("render", "USE") as second:
            self.assertNotEqual(second.token, lease.token)
            with self.assertRaises(ComponentOwnershipLost):
                other_locks.release(lease)
        locks.release(lease)

    def test_monitor_does_not_create_missing_database_or_tables(self):
        missing = self.root / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            inspect_runtime(missing, self.cfg.app, now=self.now)
        self.assertFalse(missing.exists())
        value = inspect_runtime(self.repo.path, self.cfg.app, now=self.now)
        self.assertEqual(len(value["agents"]), 18)
        with self.repo.connect() as db:
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='agent_runs'").fetchone()
            )

    def test_monitor_is_read_only_and_redacts_lock_token(self):
        self.run_stage()
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "REPAIR", 1)
        self.now += 2
        with self.repo.connect() as db:
            before = "\n".join(db.iterdump())
        value = inspect_runtime(self.repo.path, self.cfg.app, now=self.now)
        with self.repo.connect() as db:
            self.assertEqual(before, "\n".join(db.iterdump()))
        self.assertNotIn(lease.token, json.dumps(value))
        self.assertFalse(value["automatic_repair"])
        self.assertFalse(value["delivery_authority"])
        self.assertFalse(value["production_ready"])
        self.assertIn("COMPONENT_STALE", {i["kind"] for i in value["incidents"]})
        self.assertTrue(all(row["runtime_health"] == "UNKNOWN" for row in value["agents"]))

    def test_monitor_rejects_foreign_app_and_symlink(self):
        foreign = "status-bot-v2" if self.cfg.app == "snap-bot-v2" else "snap-bot-v2"
        with self.assertRaises(ValueError):
            inspect_runtime(self.repo.path, foreign, now=self.now)
        link = self.root / "link.sqlite"
        link.symlink_to(self.repo.path)
        with self.assertRaises(ValueError):
            inspect_runtime(link, self.cfg.app, now=self.now)

    def test_unknown_delivery_is_reported_without_reclaim_or_replay(self):
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET state='DELIVERING' WHERE id=?", (self.job["id"],))
        self.now += self.cfg.lease_seconds + 1
        value = inspect_runtime(self.repo.path, self.cfg.app, now=self.now)
        self.assertTrue(value["incidents"][0]["delivery_uncertain"])
        self.assertEqual(self.repo.get(self.job["id"])["state"], "DELIVERING")

    def test_backup_preserves_uncertain_execution_and_lock_without_replay(self):
        from studio_core.storage.backup import create_snapshot, probe_restore

        def stop():
            raise KeyboardInterrupt("fixture-crash")

        with self.assertRaises(KeyboardInterrupt):
            self.run_stage(callback=stop)
        locks = ComponentLocks(self.repo)
        lease = locks.acquire("render", "REPAIR", 1)
        with self.repo.transaction() as db:
            db.execute("UPDATE agent_runs SET status='RUNNING',finished_at=NULL,error_code=NULL")
        snapshot = create_snapshot(
            self.repo.path, self.root / "backup", self.cfg.app, "a" * 40, now=self.now
        )
        self.assertEqual(
            probe_restore(snapshot, self.cfg.app, self.root / "probe")["result"], "PASS"
        )
        with sqlite3.connect(snapshot / "jobs.sqlite") as db:
            self.assertEqual(db.execute("SELECT status FROM agent_runs").fetchone()[0], "RUNNING")
            self.assertEqual(
                db.execute("SELECT token FROM component_locks").fetchone()[0], lease.token
            )
        self.assertEqual(self.rows()[0]["status"], "RUNNING")
        with self.assertRaises(ComponentBusy):
            locks.acquire("render", "MAINTENANCE")

    def test_shared_database_misconfiguration_is_rejected(self):
        other = "status-bot-v2" if self.cfg.app == "snap-bot-v2" else "snap-bot-v2"
        wrong = Repository(replace(self.cfg, app=other), clock=lambda: self.now)
        with self.assertRaises(ValueError):
            ComponentLocks(wrong)

    def test_monitor_cli_error_does_not_echo_private_path(self):
        with patch("builtins.print") as printer:
            self.assertEqual(
                main(["--database", str(self.root / "private-name.sqlite"), "--app", self.cfg.app]),
                2,
            )
        self.assertNotIn("private-name", str(printer.call_args))


if __name__ == "__main__":
    unittest.main()
