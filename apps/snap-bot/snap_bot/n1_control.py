"""Authenticated authority-only control seam; not enabled by existing CLI/bots."""

import socket
from pathlib import Path

from . import n1_ipc as ipc
from .n1_writer_service import WriterService


class ControlHandler:
    def __init__(self, authority):
        self.authority = authority

    def __call__(self, body):
        if type(body) is not dict:
            raise ipc.ProtocolError("CONTROL_SCHEMA")
        if set(body) == {"action", "descriptor"} and body["action"] == "register":
            state = self.authority.register(body["descriptor"])
            gen = body["descriptor"]["body"]["gen"]
        elif set(body) == {"action", "parent", "descriptor"} and body["action"] == "seal":
            state = self.authority.seal(body["parent"], body["descriptor"])
            gen = body["descriptor"]["body"]["gen"]
        else:
            raise ipc.ProtocolError("CONTROL_OPERATION")
        return {"gen": gen, "state": state}


def request(value, app):
    if type(value) is not dict or set(value) != {"v", "app", "request_id", "op", "body"}:
        raise ipc.ProtocolError("CONTROL_SCHEMA")
    ipc.identity(value["app"], value["request_id"])
    if (
        type(value["v"]) is not int
        or value["v"] != 1
        or value["app"] != app
        or value["op"] != "control"
        or type(value["body"]) is not dict
    ):
        raise ipc.ProtocolError("CONTROL_BINDING")
    return value


def serve(sock, authority_uid, app, handler, timeout=2):
    deadline = ipc.budget(timeout)
    with sock:
        ipc.authenticate(sock, authority_uid)
        value = request(ipc.receive(sock, deadline), app)
        base = {"v": 1, "app": app, "request_id": value["request_id"]}
        try:
            response = {**base, "ok": True, "result": handler(value["body"])}
        except Exception:
            response = {**base, "ok": False, "error": "CONTROL_REFUSED"}
        ipc.send(sock, response, deadline)


class ControlService(WriterService):
    def __init__(self, path, authority_uid, app, handler, **kwargs):
        super().__init__(path, authority_uid, app, handler, **kwargs)

    def _serve(self, conn):
        try:
            serve(conn, self.worker_uid, self.app, self.handler, self.network_timeout)
        except Exception:
            with self._lock:
                self.stats["protocol_errors"] += 1
        finally:
            conn.close()
            with self._lock:
                self._threads.discard(__import__("threading").current_thread())


class ControlClient:
    def __init__(self, path, server_uid, app, timeout=2):
        self.path = Path(path)
        self.server_uid = ipc.uid(server_uid)
        self.app = app
        self.timeout = timeout
        if not self.path.is_absolute():
            raise ValueError("Absolute control socket required")

    def call(self, body, request_id):
        ipc.identity(self.app, request_id)
        deadline = ipc.budget(self.timeout)
        wire = ipc.encode(
            {"v": 1, "app": self.app, "request_id": request_id, "op": "control", "body": body}
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            try:
                ipc.remaining(sock, deadline)
                sock.connect(str(self.path))
            except OSError:
                raise ipc.RpcUnavailable("CONTROL_UNAVAILABLE") from None
            ipc.authenticate(sock, self.server_uid)
            try:
                ipc.remaining(sock, deadline)
                sock.sendall(wire)
                value = ipc.receive(sock, deadline)
                if (
                    type(value) is not dict
                    or type(value.get("v")) is not int
                    or value.get("v") != 1
                    or value.get("app") != self.app
                    or value.get("request_id") != request_id
                ):
                    raise ipc.ProtocolError("CONTROL_REPLY_BINDING")
                if value.get("ok") is True and set(value) == {
                    "v",
                    "app",
                    "request_id",
                    "ok",
                    "result",
                }:
                    return value["result"]
                if (
                    value.get("ok") is False
                    and set(value) == {"v", "app", "request_id", "ok", "error"}
                    and value["error"] == "CONTROL_REFUSED"
                ):
                    raise ipc.RpcRefused("CONTROL_REFUSED")
                raise ipc.ProtocolError("CONTROL_REPLY_SCHEMA")
            except ipc.RpcRefused:
                raise
            except (OSError, ipc.ProtocolError):
                raise ipc.RpcUncertain("CONTROL_OUTCOME_UNKNOWN") from None

    def register(self, descriptor):
        return self.call(
            {"action": "register", "descriptor": descriptor},
            "register_" + descriptor["body"]["gen"],
        )

    def seal(self, parent, descriptor):
        return self.call(
            {"action": "seal", "parent": parent, "descriptor": descriptor},
            "seal_" + descriptor["body"]["gen"],
        )
