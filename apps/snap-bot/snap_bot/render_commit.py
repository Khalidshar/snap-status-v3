"""Worker-owned input, stage adoption and rebuildable views.
The DB-free renderer never imports this module.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository
from studio_core.repair.adoption import clear_pending
from studio_core.storage import Storage

from .did_you_know import validate_content
from .n1_artifacts import verified_bytes
from .render import cleanup_attempt_temps, sync_dir, video_filename, write_once
from .weather import content_hash

T = TypeVar("T")


class AdoptionPending(OSError):
    """Immutable spool retained; explicit SAME-owner adoption retry is required."""

    pass


def stage_input(
    settings: dict[str, Any],
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    content: dict[str, Any],
) -> Path:
    repo.assert_owner(job, token, attempt=attempt)
    root = Path(settings["input_root"])
    if not root.is_absolute() or root.name != "renders":
        raise ValueError("Invalid input root")
    target = root / job / "attempts" / str(attempt) / "input/content.json"
    cursor = Path(root.anchor)
    for part in target.parent.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Unsafe input directory")
        cursor.mkdir(exist_ok=True, mode=0o750)
    cleanup_attempt_temps(target.parent.parent)
    write_once(target, json.dumps(content, ensure_ascii=False, sort_keys=True).encode())
    repo.assert_owner(job, token, attempt=attempt)
    return target


def artifact_root(settings: dict[str, Any]) -> Path:
    root = Path(settings["spool_root"])
    if not root.is_absolute() or root.name != "renders":
        raise ValueError("Invalid spool root")
    return root.parent


def record_stages(
    repo: Repository,
    db: sqlite3.Connection,
    job: str,
    attempt: int,
    result: dict[str, Any],
    expected: str,
    result_path: str,
    quality_sha: str,
) -> None:
    # Core 0.7.0 record_stage has no db parameter. Preserve its schema and
    # invariants in this SAME owned transaction, without changing the wheel.
    db.execute(
        "CREATE TABLE IF NOT EXISTS stage_results(job_id TEXT NOT NULL,stage TEXT NOT NULL,attempt INTEGER NOT NULL,sha256 TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(job_id,stage))"
    )
    if {row["name"] for row in db.execute("PRAGMA table_info(stage_results)")} != {
        "job_id",
        "stage",
        "attempt",
        "sha256",
        "path",
    }:
        raise ValueError("Unsupported stage_results schema")
    for stage, sha, path in (
        ("design", result["video_sha256"], expected),
        ("quality", quality_sha, result_path),
    ):
        prior = db.execute(
            "SELECT * FROM stage_results WHERE job_id=? AND stage=?", (job, stage)
        ).fetchone()
        if (
            prior
            and prior["attempt"] == attempt
            and (prior["sha256"] != sha or prior["path"] != path)
        ):
            raise ValueError("Stage receipt immutable within attempt")
        if prior and prior["attempt"] > attempt:
            raise LeaseLost("Stage attempt regressed")
        db.execute(
            "INSERT INTO stage_results(job_id,stage,attempt,sha256,path) VALUES(?,?,?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET attempt=excluded.attempt,sha256=excluded.sha256,path=excluded.path",
            (job, stage, attempt, sha, path),
        )
        repo.event(db, job, "stage.recorded", {"stage": stage, "attempt": attempt, "sha256": sha})


def adopt_render(
    config: Config,
    repo: Repository,
    job_id: str,
    token: str,
    attempt: int,
    result: dict[str, Any],
    settings: dict[str, Any],
) -> Any:
    filename = video_filename(result.get("content_type", "weather"))
    expected = f"renders/{job_id}/attempts/{attempt}/output/{filename}"
    if (
        result.get("attempt") != attempt
        or result.get("artifact") != expected
        or result.get("job_id") != job_id
        or result.get("qa", {}).get("result") != "PASS"
    ):
        raise ValueError("Render result not bound to attempt")
    repo.assert_owner(job_id, token, attempt=attempt)
    input_path = (
        Path(settings["input_root"]) / job_id / "attempts" / str(attempt) / "input/content.json"
    )
    content = json.loads(input_path.read_bytes())
    if filename != video_filename(content.get("type")):
        raise ValueError("Render content type mismatch")
    if content["type"] == "did_you_know":
        # validate_content allows sacred only for a sacred kind with its verbatim text.
        validate_content(content)
        if (
            result.get("content_type") != "did_you_know"
            or result.get("safe_area", {}).get("result") != "PASS"
        ):
            raise ValueError("Missing fact QA")
    elif content.get("sacred") is not False:
        raise ValueError("Unverified render input")
    root = artifact_root(settings)
    source = root / expected
    cursor = root
    if root.is_symlink():
        raise ValueError("Unsafe spool root")
    for part in Path(expected).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Unsafe spool path")
    data = verified_bytes(source, result["video_sha256"])
    result_path = source.parent / "render-result.json"
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode()
    result_bytes = verified_bytes(result_path, hashlib.sha256(encoded).hexdigest())
    if content_hash(content) != result.get("content_hash"):
        raise ValueError("Render content mismatch")
    input_bytes = verified_bytes(
        input_path,
        hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    )
    quality_sha = hashlib.sha256(result_bytes).hexdigest()
    # Verification above is deliberately outside the retryable I/O boundary.
    output = Storage(config).workspace(job_id) / "output"
    staged: list[str] = []
    try:
        # Long copy and file fsync happen outside the short DB transaction.
        for value in (data, result_bytes, input_bytes):
            fd, temp = tempfile.mkstemp(dir=output, prefix=".adopt-")
            staged.append(temp)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        with repo.owned_transaction(job_id, token, attempt=attempt) as db:
            record_stages(
                repo,
                db,
                job_id,
                attempt,
                result,
                expected,
                result_path.relative_to(root).as_posix(),
                quality_sha,
            )
            os.replace(staged[0], output / filename)
            os.replace(staged[1], output / "render-result.json")
            os.replace(staged[2], output.parent / "input/content.json")
            sync_dir(output)
            sync_dir(output.parent / "input")
        staged = []
    except OSError as error:
        raise AdoptionPending("RENDER_ADOPTION_PENDING") from error
    finally:
        for temp in staged:
            if os.path.exists(temp):
                os.unlink(temp)
    return repo.stage_result(job_id, "design", token=token, attempt=attempt)


def owned_views(
    config: Config,
    repo: Repository,
    job: str,
    token: str,
    attempt: int,
    manifest: dict[str, Any],
    output: dict[str, Any],
    *,
    validating: bool = False,
) -> None:
    storage = Storage(config)
    work = storage.workspace(job)
    storage.metadata.mkdir(parents=True, exist_ok=True, mode=0o700)
    if storage.metadata.is_symlink():
        raise ValueError("Unsafe metadata directory")
    targets = [
        (work / "manifest.json", manifest),
        (storage.metadata / (job + ".json"), manifest),
        (work / "output/slice-result.json", output),
    ]
    staged: list[tuple[str, Path]] = []
    try:
        for target, value in targets:
            fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".view-")
            staged.append((temp, target))
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
                stream.flush()
                os.fsync(stream.fileno())
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            if validating:
                repo.transition(job, token, "VALIDATING", {"qa": "PASS"}, db=db)
            repo.assert_owner(job, token, attempt=attempt, db=db)
            if validating:
                clear_pending(db, job)
            for temp, target in staged:
                os.replace(temp, target)
            for parent in {target.parent for _, target in staged}:
                sync_dir(parent)
        staged = []
    finally:
        for temp, _ in staged:
            if os.path.exists(temp):
                os.unlink(temp)


def observe_owned(
    repo: Repository, job: str, token: str, attempt: int, agent: str, callback: Callable[[], T]
) -> T:
    # These are owned DB events, not claims of physical N1 journal integration.
    if agent not in ("fact_source", "design", "quality"):
        raise ValueError("Invalid observed stage")
    run = uuid.uuid4().hex
    started = time.monotonic()

    def record(status: str, code: str | None = None) -> None:
        with repo.owned_transaction(job, token, attempt=attempt) as db:
            details = {
                "agent": agent,
                "attempt": attempt,
                "run": run,
                "status": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            if code:
                details["error_code"] = code
            repo.event(db, job, "agent.run", details)

    record("RUNNING")
    try:
        result = callback()
    except BaseException:
        try:  # noqa: SIM105  # explicit best-effort block
            record("FAILED", "STAGE_FAILED")
        except LeaseLost:
            pass
        raise
    record("SUCCEEDED")
    return result
