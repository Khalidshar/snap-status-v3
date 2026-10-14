"""Guard deletions for the batch 1 core, each in an isolated temporary source copy.

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
