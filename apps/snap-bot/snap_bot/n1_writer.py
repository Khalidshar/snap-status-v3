"""DB-free physical journal writer. No Repository, sqlite or delivery dependencies.
Only signed generation descriptors and signed append intents cross this seam.
Not enabled by bot services. Per-app synthetic shared key is retained for reference
compatibility; production credential provisioning remains a separate acceptance gate.
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

Json = dict[str, Any]


class WriterRefused(RuntimeError):
    pass


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mac(key: bytes, domain: str, value: object) -> str:
    return hmac.new(key, (domain + "\n" + canonical(value)).encode(), hashlib.sha256).hexdigest()


def hexadecimal(value: object, size: int) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(size) + "}", value) is not None


def generation(body: Json, key: bytes) -> Json:
    """Authority-side signing helper; does not read DB or journal files."""
    return {"body": body, "mac": mac(key, "n1.file-generation.v1", body)}


def sync_dir(path: str | os.PathLike[str]) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PhysicalWriter:
    LIMIT = 2_000_000

    def __init__(
        self,
        root: str | os.PathLike[str],
        key: bytes,
        app: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Writer key too short")
        if not isinstance(app, str) or not re.fullmatch("[a-z0-9-]{1,64}", app):
            raise ValueError("Invalid app")
        self.root = Path(root)
        self.key = key
        self.app = app
        self.clock = clock
        if not self.root.is_absolute():
            raise ValueError("Absolute journal root required")
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        meta = self.root.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise WriterRefused("UNSAFE_JOURNAL_ROOT")

    def descriptor(self, signed: Any) -> Json:
        if type(signed) is not dict or set(signed) != {"body", "mac"}:
            raise WriterRefused("GENERATION_SCHEMA")
        body = signed["body"]
        if type(body) is not dict or set(body) != {
            "type",
            "app",
            "gen",
            "parent",
            "base_seq",
            "base_digest",
        }:
            raise WriterRefused("GENERATION_SCHEMA")
        if (
            body["type"] != "segment"
            or body["app"] != self.app
            or not hexadecimal(body["gen"], 32)
            or not hexadecimal(body["base_digest"], 64)
        ):
            raise WriterRefused("GENERATION_BINDING")
        if type(body["base_seq"]) is not int or body["base_seq"] < 0:
            raise WriterRefused("GENERATION_SEQUENCE")
        if body["parent"] is None:
            if body["base_seq"] != 0 or body["base_digest"] != "0" * 64:
                raise WriterRefused("GENERATION_ANCHOR")
        elif not hexadecimal(body["parent"], 32) or body["parent"] == body["gen"]:
            raise WriterRefused("GENERATION_PARENT")
        if not isinstance(signed["mac"], str) or not hmac.compare_digest(
            mac(self.key, "n1.file-generation.v1", body), signed["mac"]
        ):
            raise WriterRefused("GENERATION_MAC")
        return body

    def envelope(self, value: Any, header: Json) -> Json:
        if type(value) is not dict or set(value) != {"body", "mac"}:
            raise WriterRefused("INTENT_SCHEMA")
        body = value["body"]
        fields = {
            "app",
            "job_id",
            "attempt",
            "agent",
            "status",
            "event_id",
            "intent_id",
            "owner_fp",
            "payload_sha256",
            "not_after",
            "kid",
            "gen",
            "seq",
            "prev",
        }
        if type(body) is not dict or set(body) != fields:
            raise WriterRefused("INTENT_SCHEMA")
        if body["app"] != self.app or body["gen"] != header["gen"]:
            raise WriterRefused("INTENT_BINDING")
        if not isinstance(value["mac"], str) or not hmac.compare_digest(
            mac(self.key, "n1.intent.v1", body), value["mac"]
        ):
            raise WriterRefused("INTENT_MAC")
        if (
            type(body["seq"]) is not int
            or body["seq"] <= header["base_seq"]
            or type(body["attempt"]) is not int
            or body["attempt"] < 1
        ):
            raise WriterRefused("INTENT_SEQUENCE")
        if any(
            not hexadecimal(body[name], 64) for name in ("prev", "owner_fp", "payload_sha256")
        ) or not hexadecimal(body["intent_id"], 32):
            raise WriterRefused("INTENT_HASH")
        if body["agent"] not in {"fact_source", "design", "quality"} or body["status"] not in {
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
        }:
            raise WriterRefused("INTENT_STAGE")
        if (
            isinstance(body["not_after"], bool)
            or not isinstance(body["not_after"], (int, float))
            or not math.isfinite(body["not_after"])
            or self.clock() >= body["not_after"]
        ):
            raise WriterRefused("INTENT_EXPIRED")
        return body

    def path(self, header: Json) -> Path:
        segment: Path = self.root / ("events-" + header["gen"] + ".jsonl")
        return segment

    @contextmanager
    def lock(self, path: Path) -> Iterator[None]:
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                raise WriterRefused("UNSAFE_LOCK")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def prepare(self, path: Path, header: Json) -> None:
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".generation-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write((canonical(header) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                pass
            sync_dir(self.root)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def scan(
        self, path: Path, header: Json, *, partial_tail: list[Json] | None = None
    ) -> list[Json]:
        if partial_tail is not None and (type(partial_tail) is not list or partial_tail):
            raise ValueError("Empty partial-tail report list required")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > self.LIMIT:
                raise WriterRefused("UNSAFE_SEGMENT")
            entries: list[Json] = []
            seq = header["base_seq"]
            previous = header["base_digest"]
            file_hash = hashlib.sha256()

            def report(kind: str, offset: int, raw: bytes) -> None:
                cast(list[Json], partial_tail).append(
                    {
                        "app": self.app,
                        "gen": header["gen"],
                        "kind": kind,
                        "offset": offset,
                        "tail_bytes": len(raw),
                        "tail_sha256": hashlib.sha256(raw).hexdigest(),
                        "segment_sha256": file_hash.hexdigest(),
                        "prefix_seq": seq,
                        "prefix_digest": previous,
                    }
                )

            first = stream.readline(4097)
            file_hash.update(first)
            expected = (canonical(header) + "\n").encode()
            if first != expected:
                if (
                    partial_tail is not None
                    and len(first) <= 4096
                    and not first.endswith(b"\n")
                    and expected.startswith(first)
                    and stream.read(1) == b""
                ):
                    report("PARTIAL_HEADER", 0, first)
                    return entries
                raise WriterRefused("GENERATION_CONFLICT")
            for line in iter(lambda: stream.readline(8193), b""):
                offset = stream.tell() - len(line)
                file_hash.update(line)
                if len(line) > 8192:
                    raise WriterRefused("PARTIAL_SEGMENT")
                if not line.endswith(b"\n"):
                    if partial_tail is not None and stream.read(1) == b"":
                        report("PARTIAL_RECORD", offset, line)
                        return entries
                    raise WriterRefused("PARTIAL_SEGMENT")
                try:
                    receipt = json.loads(line)
                    env = receipt["envelope"]
                    body = env["body"]
                    if set(receipt) != {"envelope", "digest", "proof"}:
                        raise ValueError()
                    if (
                        body["app"] != self.app
                        or body["gen"] != header["gen"]
                        or body["seq"] != seq + 1
                        or body["prev"] != previous
                    ):
                        raise ValueError()
                    if env["mac"] != mac(self.key, "n1.intent.v1", body):
                        raise ValueError()
                    unsigned = {"envelope": env, "digest": receipt["digest"]}
                    if receipt["proof"] != mac(self.key, "n1.receipt.v1", unsigned):
                        raise ValueError()
                    if receipt["digest"] != sha(previous + "\n" + canonical(env)):
                        raise ValueError()
                except (ValueError, KeyError, TypeError, UnicodeError):
                    raise WriterRefused("SEGMENT_CORRUPT") from None
                entries.append(receipt)
                seq = body["seq"]
                previous = receipt["digest"]
            return entries

    def append(self, signed: Any, envelope: Any) -> Json:
        header = self.descriptor(signed)
        body = self.envelope(envelope, header)
        path = self.path(header)
        with self.lock(path):
            self.prepare(path, header)
            entries = self.scan(path, header)
            unsigned = {
                "envelope": envelope,
                "digest": sha(body["prev"] + "\n" + canonical(envelope)),
            }
            receipt = {**unsigned, "proof": mac(self.key, "n1.receipt.v1", unsigned)}
            if entries and entries[-1]["envelope"]["body"]["intent_id"] == body["intent_id"]:
                if entries[-1] != receipt:
                    raise WriterRefused("APPEND_CONFLICT")
                # An earlier append could have reached cache but failed fsync before replying.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return receipt
            seq = entries[-1]["envelope"]["body"]["seq"] if entries else header["base_seq"]
            previous = entries[-1]["digest"] if entries else header["base_digest"]
            if body["seq"] != seq + 1 or body["prev"] != previous:
                raise WriterRefused("PHYSICAL_CAS_FAILED")
            self.envelope(envelope, header)  # expiry rechecked after the generation lock wait
            line = (canonical(receipt) + "\n").encode()
            if len(line) > 8192 or path.stat().st_size + len(line) > self.LIMIT:
                raise WriterRefused("SEGMENT_LIMIT")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(fd, "ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            return receipt


class IsolatedAppendHandler:
    def __init__(self, writer: PhysicalWriter) -> None:
        self.writer = writer

    def __call__(self, request: Any) -> Json:
        if type(request) is not dict or set(request) != {"generation", "envelope"}:
            raise WriterRefused("APPEND_SCHEMA")
        return self.writer.append(request["generation"], request["envelope"])
