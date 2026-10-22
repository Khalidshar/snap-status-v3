"""Opt-in durable generation registration around the DB-free physical writer.
Authority methods are trusted local calls, not an exposed worker RPC.
Existing bot services and writer CLI do not enable this contract.
"""

import json
import os
import stat
import tempfile
from collections.abc import Iterable
from typing import Any

from .n1_partial_tail import retain
from .n1_writer import PhysicalWriter, WriterRefused, canonical, sync_dir


class GenerationAuthority:
    LIMIT = 1_000_000
    CAP = 1024

    def __init__(self, writer: PhysicalWriter) -> None:
        self.writer = writer
        self.path = writer.root / "generations.json"

    def load(self) -> Any:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"version": 1, "app": self.writer.app, "generations": {}}
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_nlink != 1
                or meta.st_uid != os.geteuid()
                or meta.st_mode & 0o077
                or meta.st_size > self.LIMIT
            ):
                raise WriterRefused("REGISTRY_FILE")
            raw = stream.read(self.LIMIT + 1)

        def unique(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        try:
            state = json.loads(raw, object_pairs_hook=unique)
            if (
                type(state) is not dict
                or set(state) != {"version", "app", "generations"}
                or type(state["version"]) is not int
                or state["version"] != 1
                or state["app"] != self.writer.app
            ):
                raise ValueError()
            rows = state["generations"]
            if type(rows) is not dict or len(rows) > self.CAP:
                raise ValueError()
            for gen, row in rows.items():
                if type(row) is not dict or set(row) != {"descriptor", "state", "child"}:
                    raise ValueError()
                desc = self.writer.descriptor(row["descriptor"])
                if gen != desc["gen"] or row["state"] not in ("ACTIVE", "SEALED"):
                    raise ValueError()
                parent = desc["parent"]
                if parent is not None and (parent not in rows or rows[parent]["child"] != gen):
                    raise ValueError()
                if row["state"] == "ACTIVE" and row["child"] is not None:
                    raise ValueError()
                if row["state"] == "SEALED":
                    child = row["child"]
                    if child not in rows or rows[child]["descriptor"]["body"]["parent"] != gen:
                        raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise WriterRefused("REGISTRY_CORRUPT") from None
        return state

    def save(self, state: Any) -> None:
        raw = (canonical(state) + "\n").encode()
        if len(raw) > self.LIMIT:
            raise WriterRefused("REGISTRY_LIMIT")
        fd, name = tempfile.mkstemp(dir=self.writer.root, prefix=".registry-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            sync_dir(self.writer.root)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def durable_existing(self) -> None:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_dir(self.writer.root)

    def register(self, signed: Any) -> Any:
        desc = self.writer.descriptor(signed)
        if desc["parent"] is not None:
            raise WriterRefused("REGISTER_ROOT_ONLY")
        with self.writer.lock(self.path):
            state = self.load()
            rows = state["generations"]
            gen = desc["gen"]
            if gen in rows:
                if rows[gen]["descriptor"] != signed:
                    raise WriterRefused("REGISTER_CONFLICT")
                self.durable_existing()
                return rows[gen]["state"]
            if not self.path.exists() and list(self.writer.root.glob("events-*.jsonl")):
                raise WriterRefused("REGISTRY_MISSING_WITH_EVIDENCE")
            if len(rows) >= self.CAP:
                raise WriterRefused("REGISTRY_CAP")
            rows[gen] = {"descriptor": signed, "state": "ACTIVE", "child": None}
            self.save(state)
            return "ACTIVE"

    def seal(self, parent: Any, child: Any) -> str:
        desc = self.writer.descriptor(child)
        if desc["parent"] != parent:
            raise WriterRefused("SEAL_PARENT")
        with self.writer.lock(self.path):
            state = self.load()
            rows = state["generations"]
            gen = desc["gen"]
            old = rows.get(parent)
            if old is None:
                raise WriterRefused("PARENT_UNREGISTERED")
            if old["state"] == "SEALED":
                if old["child"] != gen or rows[gen]["descriptor"] != child:
                    raise WriterRefused("SEAL_CONFLICT")
                self.durable_existing()
                return "SEALED"
            if gen in rows:
                raise WriterRefused("CHILD_ALREADY_REGISTERED")
            if len(rows) >= self.CAP:
                raise WriterRefused("REGISTRY_CAP")
            header = self.writer.descriptor(old["descriptor"])
            path = self.writer.path(header)
            partial: list[dict[str, Any]] = []
            entries = self.writer.scan(path, header, partial_tail=partial) if path.exists() else []
            anchors = {(header["base_seq"], header["base_digest"])}
            anchors.update((entry["envelope"]["body"]["seq"], entry["digest"]) for entry in entries)
            if (desc["base_seq"], desc["base_digest"]) not in anchors:
                raise WriterRefused("SEAL_ANCHOR")
            if partial:
                retain(self.writer, header, partial[0])
            # Authority chooses its confirmed prefix; a later physical orphan is retained.
            old["state"] = "SEALED"
            old["child"] = gen
            rows[gen] = {"descriptor": child, "state": "ACTIVE", "child": None}
            self.save(state)
            return "SEALED"


class RegisteredWriter:
    def __init__(self, writer: PhysicalWriter) -> None:
        self.writer = writer
        self.authority = GenerationAuthority(writer)

    def append(self, signed: Any, envelope: Any) -> dict[str, Any]:
        desc = self.writer.descriptor(signed)
        # One registry lock spans admission through fsync; sealing cannot overtake append.
        with self.writer.lock(self.authority.path):
            state = self.authority.load()
            row = state["generations"].get(desc["gen"])
            if row is None:
                raise WriterRefused("GENERATION_UNREGISTERED")
            if row["descriptor"] != signed:
                raise WriterRefused("GENERATION_REGISTERED_CONFLICT")
            if row["state"] != "ACTIVE":
                raise WriterRefused("GENERATION_SEALED")
            return self.writer.append(signed, envelope)
