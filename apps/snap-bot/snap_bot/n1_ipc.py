"""Bounded Linux Unix-socket transport. Not enabled by any bot service.
UID authentication is required on BOTH ends before request bytes are consumed/sent.
One request per connection. A lost reply is uncertain and is NEVER retried here.
"""

import json
import math
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

MAX_FRAME = 65536


class ProtocolError(RuntimeError):
    pass


class PeerDenied(RuntimeError):
    pass


class RpcUnavailable(RuntimeError):
    pass


class RpcUncertain(RuntimeError):
    pass


class RpcRefused(RuntimeError):
    pass


def uid(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("Invalid expected UID")
    return value


def authenticate(sock: socket.socket, expected_uid: object) -> tuple[int, int, int]:
    expected_uid = uid(expected_uid)
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise PeerDenied("PEER_CREDENTIALS_UNAVAILABLE")
    pid, peer_uid, gid = struct.unpack(
        "iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
    )
    if peer_uid != expected_uid:
        raise PeerDenied("PEER_UID_REJECTED")
    return pid, peer_uid, gid


def budget(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 30
    ):
        raise ValueError("Invalid IPC deadline")
    return time.monotonic() + timeout


def remaining(sock: socket.socket, deadline: float) -> None:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("IPC_DEADLINE")
    sock.settimeout(left)


def exact(sock: socket.socket, count: int, deadline: float) -> bytes:
    parts: list[bytes] = []
    total = 0
    while total < count:
        remaining(sock, deadline)
        part = sock.recv(count - total)
        if not part:
            raise ProtocolError("TRUNCATED_FRAME")
        parts.append(part)
        total += len(part)
    return b"".join(parts)


def pairs(values: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ProtocolError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def encode(value: object) -> bytes:
    try:
        data = json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=True).encode()
    except (TypeError, ValueError, RecursionError):
        raise ProtocolError("INVALID_JSON") from None
    if not 0 < len(data) <= MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    return struct.pack("!I", len(data)) + data


def receive(sock: socket.socket, deadline: float) -> Any:
    length = struct.unpack("!I", exact(sock, 4, deadline))[0]
    if not 0 < length <= MAX_FRAME:
        raise ProtocolError("FRAME_TOO_LARGE")
    raw = exact(sock, length, deadline)
    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("NONFINITE_JSON")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("INVALID_JSON") from None


def send(sock: socket.socket, value: object, deadline: float) -> None:
    data = encode(value)
    remaining(sock, deadline)
    sock.sendall(data)


def identity(app: object, request_id: object) -> None:
    if not isinstance(app, str) or not re.fullmatch("[a-z0-9-]{1,64}", app):
        raise ProtocolError("INVALID_APP")
    if not isinstance(request_id, str) or not re.fullmatch("[A-Za-z0-9_-]{1,64}", request_id):
        raise ProtocolError("INVALID_REQUEST_ID")


def request(value: Any, app: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"v", "app", "request_id", "op", "body"}:
        raise ProtocolError("REQUEST_SCHEMA")
    identity(value["app"], value["request_id"])
    if type(value["v"]) is not int or value["v"] != 1 or value["app"] != app:
        raise ProtocolError("REQUEST_BINDING")
    if value["op"] != "append" or type(value["body"]) is not dict:
        raise ProtocolError("REQUEST_OPERATION")
    return value


def serve_connection(
    sock: socket.socket,
    *,
    worker_uid: int,
    app: str,
    handler: Callable[[Any], Any],
    timeout: float = 2,
) -> None:
    """Caller owns accepting/listener lifecycle; unauthorized peers are closed unread."""
    deadline = budget(timeout)
    with sock:
        authenticate(sock, worker_uid)  # must precede any receive or handler call
        value = request(receive(sock, deadline), app)
        base = {"v": 1, "app": app, "request_id": value["request_id"]}
        try:
            result = handler(value["body"])
            response = {**base, "ok": True, "result": result}
        except Exception:
            # Do not send exception text, paths, tokens, keys or SQL back to a client.
            response = {**base, "ok": False, "error": "APPEND_REFUSED"}
        send(sock, response, deadline)


def call(
    path: str | os.PathLike[str],
    *,
    server_uid: int,
    app: str,
    request_id: str,
    envelope: Any,
    timeout: float = 2,
) -> Any:
    """Connect failure is unavailable; any failure after sending may be committed."""
    uid(server_uid)
    identity(app, request_id)
    deadline = budget(timeout)
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Absolute Unix socket path required")
    value = {"v": 1, "app": app, "request_id": request_id, "op": "append", "body": envelope}
    wire = encode(value)  # validation/size failure occurs before any connection
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with sock:
        try:
            remaining(sock, deadline)
            sock.connect(str(path))
        except OSError:
            raise RpcUnavailable("IPC_UNAVAILABLE") from None
        authenticate(sock, server_uid)  # must precede sending envelope
        try:
            remaining(sock, deadline)
            sock.sendall(wire)
            response = receive(sock, deadline)
            if (
                type(response) is not dict
                or response.get("v") != 1
                or type(response.get("v")) is not int
                or response.get("app") != app
                or response.get("request_id") != request_id
            ):
                raise ProtocolError("RESPONSE_BINDING")
            if response.get("ok") is True and set(response) == {
                "v",
                "app",
                "request_id",
                "ok",
                "result",
            }:
                return response["result"]
            if (
                response.get("ok") is False
                and set(response) == {"v", "app", "request_id", "ok", "error"}
                and response["error"] == "APPEND_REFUSED"
            ):
                raise RpcRefused("APPEND_REFUSED")
            raise ProtocolError("RESPONSE_SCHEMA")
        except RpcRefused:
            raise
        except (OSError, ProtocolError):
            raise RpcUncertain("APPEND_OUTCOME_UNKNOWN") from None


class SegmentAppendHandler:
    """Reference seam for JournalSegments; not the final DB-isolated writer service."""

    def __init__(self, segments: Any) -> None:
        self.segments = segments

    def __call__(self, envelope: Any) -> Any:
        if type(envelope) is not dict or set(envelope) != {"body", "mac"}:
            raise ProtocolError("ENVELOPE_SCHEMA")
        body = envelope["body"]
        if type(body) is not dict or body.get("app") != self.segments.repo.config.app:
            raise ProtocolError("ENVELOPE_APP")
        return self.segments.append(envelope)
