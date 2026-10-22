"""Retain signed partial-write evidence before an authority seals a generation."""

import hashlib
import os
import stat
import tempfile

from .n1_writer import WriterRefused, canonical, mac, sync_dir


def retain(writer, header, report):
    fields = {
        "app",
        "gen",
        "kind",
        "offset",
        "tail_bytes",
        "tail_sha256",
        "segment_sha256",
        "prefix_seq",
        "prefix_digest",
    }
    if (
        type(report) is not dict
        or set(report) != fields
        or report["app"] != writer.app
        or report["gen"] != header["gen"]
        or report["kind"] not in ("PARTIAL_HEADER", "PARTIAL_RECORD")
    ):
        raise WriterRefused("TAIL_REPORT_BINDING")
    for field in ("offset", "tail_bytes", "prefix_seq"):
        if type(report[field]) is not int or report[field] < 0:
            raise WriterRefused("TAIL_REPORT_RANGE")
    if report["tail_bytes"] > 8192:
        raise WriterRefused("TAIL_REPORT_RANGE")
    segment = writer.path(header)
    fd = os.open(segment, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or meta.st_nlink != 1
            or meta.st_mode & 0o077
            or meta.st_size > writer.LIMIT
        ):
            raise WriterRefused("TAIL_SOURCE_PERMISSIONS")
        raw = os.read(fd, writer.LIMIT + 1)
        if (
            len(raw) != meta.st_size
            or hashlib.sha256(raw).hexdigest() != report["segment_sha256"]
            or report["offset"] + report["tail_bytes"] != len(raw)
        ):
            raise WriterRefused("TAIL_SOURCE_CHANGED")
        if hashlib.sha256(raw[report["offset"] :]).hexdigest() != report["tail_sha256"]:
            raise WriterRefused("TAIL_SOURCE_CHANGED")
        # Keep the exact old bytes durable; never truncate/repair the segment.
        os.fsync(fd)
    finally:
        os.close(fd)
    body = {"version": 1, **report}
    receipt = {"body": body, "proof": mac(writer.key, "n1.partial-tail.v1", body)}
    expected = (canonical(receipt) + "\n").encode()
    destination = writer.root / ("tail-" + header["gen"] + ".json")
    try:
        fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        try:
            meta = os.fstat(fd)
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_uid != os.geteuid()
                or meta.st_nlink != 1
                or meta.st_mode & 0o077
                or meta.st_size > 4096
            ):
                raise WriterRefused("TAIL_RECEIPT_PERMISSIONS")
            if os.read(fd, 4097) != expected:
                raise WriterRefused("TAIL_RECEIPT_CONFLICT")
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_dir(writer.root)
        return receipt
    # The caller holds the registry lock. A crash before replace leaves only a
    # private temp; a crash after replace is repaired by the existing-file branch.
    fd, name = tempfile.mkstemp(dir=writer.root, prefix=".tail-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
        sync_dir(writer.root)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return receipt
