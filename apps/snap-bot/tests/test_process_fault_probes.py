"""Real process death and Unix socket races; encoding/transport are synthetic."""

import errno
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[3]


@unittest.skipUnless(
    sys.platform == "linux", "Real SIGKILL and Unix socket fault probes require Linux"
)
class ProcessFaultProbes(unittest.TestCase):
    def setUp(self):
        from test_owned_contract_adapter import AdapterServiceTests

        self.a = AdapterServiceTests()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.f = self.a.f
        self.repo = self.f.repo
        self.children = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def prepare(self):
        from snap_bot.did_you_know import choose_fact
        from snap_bot.owned_contract_adapter import run_stage
        from snap_bot.render_commit import stage_input

        f = self.f
        claim = self.a.claim
        content, _ = run_stage(
            self.repo,
            f.job,
            claim["lease_token"],
            1,
            "fact_source",
            {"command": "did_you_know"},
            lambda: choose_fact(self.repo, f.job, claim["lease_token"], 1),
        )
        f.content = content
        f.digest = content["content_hash"]
        stage_input(f.settings, self.repo, f.job, claim["lease_token"], 1, content)
        f.rendering()
        from snap_bot.arabic_agent import check_content

        run_stage(
            self.repo,
            f.job,
            claim["lease_token"],
            1,
            "arabic",
            content,
            lambda: check_content(content),
        )
        from snap_bot.content_agents import CHECKS, run_check

        for role in CHECKS:
            run_stage(
                self.repo,
                f.job,
                claim["lease_token"],
                1,
                role,
                content,
                lambda role=role: run_check(
                    role, self.repo, f.job, claim["lease_token"], 1, content
                ),
            )
        result = run_stage(
            self.repo, f.job, claim["lease_token"], 1, "design", content, self.a.fixture.fake_render
        )
        from snap_bot.render_agents import RENDER_CHECKS, run_render_check

        for role in RENDER_CHECKS:
            run_stage(
                self.repo,
                f.job,
                claim["lease_token"],
                1,
                role,
                result,
                lambda role=role: run_render_check(
                    role, self.repo, f.job, claim["lease_token"], 1, result, f.settings
                ),
            )
        # Establish the actual core schema, then leave no adopted receipt.
        self.repo.record_stage(
            f.job, claim["lease_token"], 1, "design", result["video_sha256"], result["artifact"]
        )
        with self.repo.transaction() as db:
            db.execute("DELETE FROM stage_results WHERE job_id=?", (f.job,))
        self.result = result
        self.video = Path(f.settings["spool_root"]).parent / result["artifact"]
        self.control = f.root / "fault-control"
        self.control.mkdir()
        state = {
            "config": str(ROOT / "config/snap/weather.toml"),
            "data_root": str(f.config.data_root),
            "settings": f.settings,
            "claim": claim,
            "result": result,
            "video": str(self.video),
            "video_hex": f.video.hex(),
            "control": str(self.control),
        }
        self.state = f.root / "process-state.json"
        self.state.write_text(json.dumps(state))

    def spawn(self, mode):
        # Preserve mutation-copy PYTHONPATH in every subprocess.
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(Path(__file__).parent / "process_fault_child.py"),
                str(self.state),
                mode,
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.children.append(process)
        return process

    def ready(self, process, expected, timeout=10):
        readable, _, _ = select.select([process.stdout], [], [], timeout)
        self.assertTrue(readable, "Checkpoint was never reached: " + expected)
        value = process.stdout.readline().strip()
        if not value:
            _, error = process.communicate(timeout=5)
            self.fail("Child exited before checkpoint " + expected + ": " + error[-2200:])
        self.assertEqual(value, expected, "Wrong checkpoint; child may have failed")

    def kill_at(self, process, expected):
        self.ready(process, expected)
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def new_owner(self):
        with self.repo.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, self.f.job))
        claim = self.repo.claim(job_id=self.f.job)
        self.assertEqual(claim["attempt"], 2)
        return claim

    def no_delivery(self):
        with self.repo.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE job_id=?", (self.f.job,)
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.a.calls, [])

    def reconcile(self, claim):
        from snap_bot.owned_contract_adapter import reconcile_stage

        with (
            patch(
                "snap_bot.service.UnixHTTPConnection",
                side_effect=AssertionError("reconciliation rendered"),
            ),
            patch("snap_bot.render.run", side_effect=AssertionError("reconciliation encoded")),
        ):
            return reconcile_stage(
                self.repo,
                self.f.job,
                claim["lease_token"],
                2,
                "quality",
                1,
                settings=self.f.settings,
                result=self.result,
            )

    def test_sigkill_before_fsync_leaves_no_final_and_same_attempt_retries(self):
        from snap_bot.owned_contract_adapter import ContractViolation
        from snap_bot.render import write_once

        self.prepare()
        self.video.unlink()
        self.kill_at(self.spawn("write-before-fsync"), "CUT:write-before-fsync")
        self.assertFalse(self.video.exists(), msg="guard:atomic-final-publication")
        temporary = list(self.video.parent.glob(".tmp-*"))
        self.assertEqual(len(temporary), 1)
        self.assertEqual(temporary[0].read_bytes(), self.f.video[: len(self.f.video) // 2])
        write_once(self.video, self.f.video)
        self.assertEqual(self.video.read_bytes(), self.f.video)
        claim = self.new_owner()
        # This cut precedes quality-ledger creation, so reconciliation refuses
        # missing contract evidence before checking an adoption receipt.
        with self.assertRaisesRegex(ContractViolation, "CONTRACT_RECONCILIATION_EVIDENCE_MISSING"):
            self.reconcile(claim)
        self.assertIsNone(self.repo.stage_result(self.f.job, "design"))
        self.no_delivery()

    def test_sigkill_after_fsync_before_db_rejects_uncommitted_adoption(self):
        from snap_bot.owned_contract_adapter import ContractViolation, contract_status

        self.prepare()
        self.kill_at(self.spawn("fsynced-before-db"), "CUT:fsynced-before-db")
        self.assertTrue(list(self.f.legacy().glob(".adopt-*")))
        self.assertIsNone(self.repo.stage_result(self.f.job, "design"))
        self.assertEqual(contract_status(self.repo, self.f.job, 1, "quality"), "RUNNING")
        with self.assertRaisesRegex(
            ContractViolation,
            "CONTRACT_ADOPTION_UNCONFIRMED",
            msg="guard:kill-uncommitted-adoption",
        ):
            self.reconcile(self.new_owner())
        self.assertEqual(self.f.render_calls, 1)
        self.no_delivery()

    def test_sigkill_after_db_before_telemetry_reconciles_without_render_or_send(self):
        from snap_bot.owned_contract_adapter import contract_status

        self.prepare()
        self.kill_at(self.spawn("db-before-telemetry"), "CUT:db-before-telemetry")
        self.assertEqual(
            contract_status(self.repo, self.f.job, 1, "quality"),
            "SUCCEEDED",
            msg="guard:kill-committed-outcome",
        )
        events = [
            json.loads(e["details"])
            for e in self.repo.timeline(self.f.job)
            if e["event"] == "agent.run"
        ]
        self.assertEqual([e["status"] for e in events if e["agent"] == "quality"], ["RUNNING"])
        claim = self.new_owner()
        proof = self.reconcile(claim)
        self.assertEqual(proof["status"], "RECONCILED")
        self.assertFalse(proof["execution_retried"])
        self.assertFalse(proof["delivery_attempted"])
        self.assertEqual(self.f.render_calls, 1)
        self.assertEqual(self.repo.get(self.f.job)["state"], "RUNNING")
        self.no_delivery()

    def test_enospc_partial_input_fails_and_releases_unused_fact(self):
        from test_atomic_attempt_writes import disk_full

        from snap_bot.did_you_know import FactsExhausted, choose_fact, load_facts

        with disk_full(), self.assertRaises(OSError) as error:
            self.a.execute()
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        job = self.repo.get(self.f.job)
        events = [
            json.loads(e["details"])
            for e in self.repo.timeline(self.f.job)
            if e["event"] == "job.state"
        ]
        self.assertEqual(
            (job["state"], events[-1].get("code")),
            ("FAILED", "VERTICAL_SLICE_FAILED"),
            msg="guard:enospc-terminal-failure",
        )
        self.assertIsNone(self.repo.stage_result(self.f.job, "design"))
        self.assertEqual(self.a.render_calls, 0)
        self.no_delivery()
        with self.repo.connect() as db:
            content = json.loads(
                db.execute(
                    "SELECT content_json FROM fact_reservations WHERE job_id=?", (self.f.job,)
                ).fetchone()[0]
            )
        self.assertFalse(self.a.input.exists(), msg="guard:enospc-no-final")
        catalog = self.f.root / "single-fact.json"
        catalog.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "language": "ar",
                    "sacred": False,
                    "facts": [next(f for f in load_facts() if f["id"] == content["fact_id"])],
                }
            )
        )
        job, _ = self.repo.enqueue(
            "after-enospc", "owner", "did_you_know", "NORMAL", "input", time.time() + 600
        )
        claim = self.repo.claim(job_id=job["id"])
        try:
            selected, _ = choose_fact(
                self.repo, job["id"], claim["lease_token"], claim["attempt"], catalog=catalog
            )
        except FactsExhausted:
            self.fail("guard:enospc-reservation-release: failed unused fact remained reserved")
        self.assertEqual(
            selected["fact_id"], content["fact_id"], msg="guard:enospc-reservation-release"
        )

    def test_two_worker_processes_on_real_socket_encode_only_once(self):
        self.prepare()
        # Remove only this disposable attempt's renderer outputs and admission.
        work = self.video.parent.parent
        shutil.rmtree(work)
        server = self.spawn("server")
        self.ready(server, "SOCKET_READY")
        first = self.spawn("client")
        self.ready(first, "REQUEST_SENT")
        self.ready(server, "ENCODE:1")
        second = self.spawn("client")
        self.ready(second, "REQUEST_SENT")
        # Keep the first encoder blocked while the second request is queued.
        # A threaded-server deletion can enter a second encoder during this window.
        select.select([server.stdout], [], [], 1.0)
        (self.control / "release").write_text("release")
        replies = []
        for process in (first, second):
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
            replies.append(json.loads(out.strip()))
        count = int((self.control / "encode-count").read_text())
        self.assertEqual(count, 1, msg="guard:socket-single-encoder")
        self.assertEqual([r["status"] for r in replies], [200, 200])
        self.assertEqual(replies[0]["result"]["video_sha256"], replies[1]["result"]["video_sha256"])
        self.assertIsNone(self.repo.stage_result(self.f.job, "design"))
        self.no_delivery()
