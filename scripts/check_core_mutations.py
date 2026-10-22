"""Guard deletions for the batch 1 and 2 core, each in an isolated temporary source copy.

Every case removes one protective line. The named test must then fail; a
mutation that the suite does not detect means the guard is untested.
Usage: uv run --frozen python scripts/check_core_mutations.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "packages" / "studio-core"
GUARD = "            raise Unauthorized("
CASES = [
    (
        "gateway signature",
        "jobs/gateway.py",
        GUARD + '"Invalid signature")',
        "            pass",
        "test_forged_signature_from_allowed_owner_does_not_enqueue",
    ),
    (
        "gateway actor allowlist",
        "jobs/gateway.py",
        GUARD + '"Actor not allowed")',
        "            pass",
        "test_other_actor_is_rejected_even_with_valid_signature",
    ),
    (
        "idempotency conflict",
        "jobs/repository.py",
        '                    raise Conflict("Idempotency key reused with different request")',
        "                    pass",
        "test_idempotency_conflicting_payload_rejected",
    ),
    (
        "rate limit",
        "jobs/repository.py",
        '                raise GatewayLimit("Command limit reached")',
        "                pass",
        "test_rate_limit_not_bypassed_by_completed_jobs",
    ),
    (
        "lease ownership",
        "jobs/repository.py",
        '            raise LeaseLost("Stale or missing lease")\n        epoch',
        "            pass\n        epoch",
        "test_expired_lease_recovered_and_old_worker_fenced",
    ),
    (
        "attempt fencing",
        "jobs/repository.py",
        '            raise LeaseLost("Stale attempt")',
        "            pass",
        "test_old_token_and_epoch_rejected",
    ),
    (
        "state machine",
        "jobs/repository.py",
        '            raise ValueError("Invalid state transition")',
        "            pass",
        "test_invalid_jump_cannot_skip_validation",
    ),
    (
        "retention waits for delivery",
        "storage/workspace.py",
        "                if pending:\n                    continue",
        "                if pending:\n                    pass",
        "test_gc_keeps_assets_when_delivery_evidence_appears_after_terminal_mark",
    ),
    (
        "workspace escape",
        "storage/workspace.py",
        '            raise ValueError("Workspace escapes root")',
        "            pass",
        "test_workspace_rejects_traversal_and_symlink",
    ),
    (
        "cross-application backup",
        "storage/backup.py",
        '        raise ValueError("Cross-application snapshot forbidden")',
        "        pass",
        "test_cross_app_backup_and_probe_are_rejected",
    ),
    (
        "egress host allowlist",
        "network/egress.py",
        '        raise PolicyDenied("HOST_NOT_ALLOWED")',
        "        pass",
        "test_exact_host_only",
    ),
    (
        "egress public addresses only",
        "network/egress.py",
        '        raise PolicyDenied("NON_PUBLIC_ADDRESS")',
        "        pass",
        "test_dns_private_metadata_loopback_ipv6_rejected",
    ),
    (
        "egress no redirects",
        "network/egress.py",
        '            raise PolicyDenied("REDIRECT_DENIED")',
        "            pass",
        "test_redirect_not_followed",
    ),
    (
        "secret redaction",
        "observability/redaction.py",
        'value = value.replace(secret, "[redacted]")',
        "value = value",
        "test_redaction_covers_values_keys_and_url_credentials",
    ),
    (
        "retry transient failures",
        "network/resilience.py",
        "                if not transient(error):\n"
        "                    breaker.release()\n"
        "                    raise\n",
        "                breaker.release()\n                raise\n",
        "test_transient_failures_retry_with_growing_jittered_waits",
    ),
    (
        "exponential backoff",
        "network/resilience.py",
        "base = self.base_seconds * 2.0**retry",
        "base = self.base_seconds",
        "test_transient_failures_retry_with_growing_jittered_waits",
    ),
    (
        "attempt cap",
        "network/resilience.py",
        "                    attempt + 1 >= self.retry.attempts\n",
        "                    attempt + 1 >= 9\n",
        "test_attempts_are_capped_at_three",
    ),
    (
        "final errors not retried",
        "network/resilience.py",
        "    if code in TRANSIENT:\n        return True\n",
        "    if code:\n        return True\n",
        "test_final_errors_are_never_retried",
    ),
    (
        "ordinary 4xx is final",
        "network/resilience.py",
        '(status == "429" or 500 <= int(status) <= 599)',
        "int(status) >= 400",
        "test_429_and_5xx_are_transient_but_other_4xx_are_not",
    ),
    (
        "retry wait budget",
        "network/resilience.py",
        "                    or waited + delay > self.retry.max_wait_seconds\n",
        "",
        "test_wait_budget_stops_retries_early",
    ),
    (
        "breaker opens",
        "network/resilience.py",
        "            if self.failures >= self.failure_threshold:\n"
        "                self.opened_at = self.clock()\n",
        "",
        "test_breaker_opens_after_five_failed_requests_and_rejects_without_calling",
    ),
    (
        "open breaker rejects",
        "network/resilience.py",
        '                raise NetworkError("CIRCUIT_OPEN")\n',
        "                pass\n",
        "test_breaker_opens_after_five_failed_requests_and_rejects_without_calling",
    ),
    (
        "single half-open trial",
        "network/resilience.py",
        "state == self.HALF_OPEN and self.trial",
        "False",
        "test_half_open_trial_closes_on_success_and_reopens_on_failure",
    ),
    (
        "trial failure reopens",
        "network/resilience.py",
        "            if self.opened_at is not None:\n"
        "                self.opened_at = self.clock()\n"
        "                return\n",
        "            if self.opened_at is not None:\n                return\n",
        "test_half_open_trial_closes_on_success_and_reopens_on_failure",
    ),
    (
        "policy errors do not trip",
        "network/resilience.py",
        "                    breaker.release()\n                    raise\n",
        "                    breaker.failure()\n                    raise\n",
        "test_final_errors_and_success_do_not_trip_the_breaker",
    ),
    (
        "success resets count",
        "network/resilience.py",
        "            self.failures, self.opened_at, self.trial = 0, None, False",
        "            self.opened_at, self.trial = None, False",
        "test_final_errors_and_success_do_not_trip_the_breaker",
    ),
    (
        "cache hit",
        "network/resilience.py",
        "            if cached is not None:\n                return cached\n",
        "",
        "test_cache_serves_within_ttl_only_and_never_caches_failures",
    ),
    (
        "cache expiry",
        "network/resilience.py",
        "if item and self.clock() < item[0]:",
        "if item:",
        "test_cache_serves_within_ttl_only_and_never_caches_failures",
    ),
    (
        "glyph coverage",
        "renderer/primitives.py",
        "    if any(not any(low <= ord(char) <= high for low, high in ranges) for char in text):",
        "    if False:",
        "test_missing_glyph_rejected",
    ),
    (
        "renderer network isolation",
        "renderer/primitives.py",
        "    if not all(checks.values()):",
        "    if False:",
        "test_proof_refuses_a_renderer_that_still_has_network",
    ),
    (
        "fsync before link",
        "renderer/primitives.py",
        "            stream.flush()\n            os.fsync(stream.fileno())\n",
        "            stream.flush()\n",
        "test_fsync_precedes_link_and_link_never_replaces_inode",
    ),
    (
        "create-once input",
        "renderer/primitives.py",
        "            except FileExistsError:\n                verify_once(path, data)",
        "            except FileExistsError:\n                pass",
        "test_fsync_precedes_link_and_link_never_replaces_inode",
    ),
    (
        "exclusive publication",
        "renderer/primitives.py",
        '            raise ValueError("Attempt artifact immutable") from None',
        "            pass",
        "test_publish_is_exclusive_and_identical_republish_is_allowed",
    ),
    (
        "temporary name fence",
        "renderer/primitives.py",
        '            if not re.fullmatch(r"\\.tmp-[0-9a-f]{32}", name):',
        "            if False:",
        "test_cleanup_keeps_final_fresh_other_attempt_and_symlink",
    ),
    (
        "temporary age fence",
        "renderer/primitives.py",
        " or now - meta.st_mtime < min_age:",
        ":",
        "test_cleanup_keeps_final_fresh_other_attempt_and_symlink",
    ),
    (
        "active writer lock",
        "renderer/primitives.py",
        "                except BlockingIOError:\n                    continue",
        "                except BlockingIOError:\n                    pass",
        "test_cleanup_preserves_old_locked_writer",
    ),
    (
        "cleanup symlink refusal",
        "renderer/primitives.py",
        '    if root.is_symlink():\n        raise ValueError("Unsafe cleanup attempt")',
        "    if root.is_symlink():\n        pass",
        "test_cleanup_refuses_paths_outside_an_attempt",
    ),
    (
        "startup cleanup",
        "renderer/primitives.py",
        "    cleanup_attempt_temps(base / str(attempt))\n",
        "",
        "test_attempt_start_cleans_own_old_temporary_only",
    ),
    (
        "attempt directory symlink",
        "renderer/primitives.py",
        "    for path in (base, base / str(attempt)):\n        if path.is_symlink():",
        "    for path in (base, base / str(attempt)):\n        if False:",
        "test_attempt_workspace_rejects_bad_numbers_and_symlinks",
    ),
    (
        "published artifact size",
        "renderer/primitives.py",
        "        if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 50_000_000:",
        "        if False:",
        "test_file_sha256_refuses_empty_and_non_regular_files",
    ),
]


def main() -> int:
    for name, relative, old, new, test in CASES:
        source = CORE / "studio_core" / relative
        original = source.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"Mutation target not unique: {name}")
            return 1
        with tempfile.TemporaryDirectory(prefix="v3-core-mutation-") as temp:
            copy = Path(temp) / "studio_core"
            shutil.copytree(
                CORE / "studio_core", copy, ignore=shutil.ignore_patterns("__pycache__")
            )
            (copy / relative).write_text(original.replace(old, new), encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": temp, "PYTHONDONTWRITEBYTECODE": "1"}
            # Fixed argv: this interpreter running the repository's own tests.
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(CORE / "tests"),
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
                timeout=600,
            )
            if result.returncode == 0 or f"::{test}" not in result.stdout:
                print(f"Named guard sample not detected: {name}\n{result.stdout[-3000:]}")
                return 1
        print(f"core guard deletion detected: {name}")
    print(f"core guard samples detected: {len(CASES)}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
