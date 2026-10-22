import argparse
import hashlib
import json
import os
import re
import socket
import socketserver
import struct
import tempfile
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from studio_core.config import Config
from studio_core.jobs import LeaseLost, Repository
from studio_core.jobs.gateway import CommandGateway
from studio_core.network import NetworkClient, UnixHTTPConnection
from studio_core.network.egress import EgressServer
from studio_core.network.resilience import ResilientClient
from studio_core.observability import Redactor
from studio_core.repair.adoption import claim_attempt, expire_pending_adoptions, mark_pending
from studio_core.storage import Storage

from .agent_contract_map import RENDER_CHECKS
from .agent_telemetry import TelemetryReconciliationRequired, observe
from .arabic_agent import check_content
from .content_agents import CHECKS, run_check
from .did_you_know import choose_fact
from .owned_contract_adapter import (
    ContractReconciliationRequired,
    ContractViolation,
    contract_status,
    reconcile_stage,
    run_stage,
)
from .render import render
from .render_agents import run_render_check
from .render_commit import AdoptionPending, adopt_render, observe_owned, owned_views, stage_input
from .telegram_egress import DeliveryConfirmed
from .weather import fetch_weather


def render_request(request):
    # The lease token and attempt are fencing identities, not credentials; the socket stays 0600.
    if not isinstance(request, dict) or set(request) != {"job_id", "content_hash", "attempt"}:
        raise ValueError("Invalid request")
    if not all(
        isinstance(request[key], str) and re.fullmatch(pattern, request[key])
        for key, pattern in (("job_id", "[0-9a-f]{32}"), ("content_hash", "[0-9a-f]{64}"))
    ):
        raise ValueError("Invalid request")
    if type(request["attempt"]) is not int or request["attempt"] < 1:
        raise ValueError("Invalid request")
    return request


class RenderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            peer = struct.unpack(
                "iII", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )[1]
            if peer != self.server.settings["worker_uid"]:
                raise PermissionError("Unexpected render peer")
            n = int(self.headers.get("Content-Length", "0"))
            if self.path != "/render" or not 0 < n <= 2048 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid request")
            request = render_request(json.loads(self.rfile.read(n)))
            result = render(
                self.server.config,
                self.server.settings,
                request["job_id"],
                request["content_hash"],
                request["attempt"],
            )
            code = 200
        except Exception as exc:
            result = {"error": type(exc).__name__}
            code = 422
        body = json.dumps(result).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def renderer(config, settings):
    path = Path(settings["render_socket"])
    if path.exists():
        import stat

        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError("Unsafe render socket")
        path.unlink()
    # Single request at a time enforces one render regardless of queue caller count.
    with socketserver.UnixStreamServer(str(path), RenderHandler) as server:
        server.config = config
        server.settings = settings
        path.chmod(0o660)
        server.serve_forever()


def execute(config, settings, secret, claim=None, delivery=None, watchdog=None):
    repo = Repository(config, Redactor([secret]))
    gateway = CommandGateway(config, repo, secret)
    expire_pending_adoptions(repo)
    if claim is None:
        now = time.time()
        body = {
            "channel": "scheduler",
            "actor": config.owners["scheduler"][0],
            "event_id": uuid.uuid4().hex,
            "command": "weather",
            "priority": "NORMAL",
        }
        job, _ = gateway.accept(body, now, gateway.signature(body, now))
        claim = repo.claim()
        if not claim or claim["id"] != job["id"]:
            raise RuntimeError("Queue busy")
    else:
        job = claim
    if job["command"] not in {"weather", "did_you_know"}:
        raise ValueError("Unsupported slice command")
    if job["command"] == "did_you_know" and delivery is not None:
        raise ValueError("Fact preview requires separate manual delivery")
    token = claim["lease_token"]
    attempt = claim["attempt"]
    stop = threading.Event()
    lease_failed = []

    def renew():
        while not stop.wait(config.lease_seconds / 3):
            try:
                repo.heartbeat(job["id"], token)
                if watchdog:
                    watchdog()
            except Exception:
                lease_failed.append(True)
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    root = Storage(config).workspace(job["id"])
    try:
        if claim.get("recovered_from") is not None:
            raise TelemetryReconciliationRequired("Recovery is not activated for this service")
        repo.transition(job["id"], token, "RESEARCHING")
        source = lambda: (
            choose_fact(repo, job["id"], token, attempt)
            if job["command"] == "did_you_know"
            else fetch_weather(
                ResilientClient(
                    NetworkClient(config),
                    app=config.app,
                    cache_ttl=settings.get("network_cache_seconds", 0),
                ),
                settings,
                time.time(),
            )
        )
        fact_contracts = job["command"] == "did_you_know"
        if fact_contracts:
            content, failures = run_stage(
                repo, job["id"], token, attempt, "fact_source", {"command": "did_you_know"}, source
            )
            content = run_stage(
                repo, job["id"], token, attempt, "arabic", content, lambda: check_content(content)
            )
            for role in CHECKS:
                content = run_stage(
                    repo,
                    job["id"],
                    token,
                    attempt,
                    role,
                    content,
                    lambda role=role, content=content: run_check(
                        role, repo, job["id"], token, attempt, content
                    ),
                )
        else:
            content, failures = observe_owned(
                repo, job["id"], token, attempt, "fact_source", source
            )
        repo.transition(
            job["id"], token, "PROCESSING", {"source": content["source"], "fallbacks": failures}
        )
        stage_input(settings, repo, job["id"], token, attempt, content)
        for state in ("DOWNLOADING", "DESIGNING", "RENDERING"):
            repo.transition(job["id"], token, state)

        def remote_render():
            owned = repo.assert_owner(job["id"], token, attempt=attempt)
            if owned["state"] != "RENDERING":
                raise LeaseLost("Render requires RENDERING ownership")
            connection = UnixHTTPConnection(
                settings["render_socket"], settings["profile"]["timeout_seconds"] + 10
            )
            try:
                connection.connect()
                peer = struct.unpack(
                    "iII", connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )[1]
                if peer != settings["renderer_uid"]:
                    raise PermissionError("Unexpected renderer UID")
                connection.request(
                    "POST",
                    "/render",
                    json.dumps(
                        {
                            "job_id": job["id"],
                            "content_hash": content["content_hash"],
                            "attempt": attempt,
                        }
                    ),
                )
                response = connection.getresponse()
                result = json.loads(response.read(65536))
                if response.status != 200:
                    raise RuntimeError("Render failed: " + result.get("error", "UNKNOWN"))
                if result.get("content_hash") != content["content_hash"]:
                    raise ValueError("Render content mismatch")
                return result
            finally:
                connection.close()

        if fact_contracts:
            result = run_stage(repo, job["id"], token, attempt, "design", content, remote_render)
            for role in RENDER_CHECKS:
                result = run_stage(
                    repo,
                    job["id"],
                    token,
                    attempt,
                    role,
                    result,
                    lambda role=role, result=result: run_render_check(
                        role, repo, job["id"], token, attempt, result, settings
                    ),
                )
        else:
            result = observe_owned(repo, job["id"], token, attempt, "design", remote_render)
        if lease_failed:
            raise LeaseLost("Lease lost")
        adopt = lambda: adopt_render(config, repo, job["id"], token, attempt, result, settings)
        if fact_contracts:
            run_stage(
                repo,
                job["id"],
                token,
                attempt,
                "quality",
                result,
                adopt,
                retryable_errors=(AdoptionPending,),
            )
        else:
            observe_owned(repo, job["id"], token, attempt, "quality", adopt)
        manifest = {
            "app": config.app,
            "job_id": job["id"],
            "content": content,
            "render": result,
            "external_delivery": False,
            "delivery": "AWAITING_TEST_BOT_AND_VISUAL_REVIEW",
        }
        output = {
            "job_id": job["id"],
            "state": "VALIDATING",
            "source": content["source"],
            "fallbacks": failures,
            "render": result,
            "external_delivery": False,
        }
        owned_views(config, repo, job["id"], token, attempt, manifest, output, validating=True)
        if fact_contracts:
            from .final_agent import evaluate_final

            output["final_decision"] = evaluate_final(repo, job["id"], token, attempt, result)
        if delivery:
            output["delivery_receipt"] = delivery(config, job["id"], result["video_sha256"], token)
            output["state"] = repo.get(job["id"])["state"]
            output["external_delivery"] = output["delivery_receipt"]["delivery"] == "PUBLISHED"
            # Terminal delivery views are reconciled from ACK, never regressed to awaiting.
        return output
    except AdoptionPending:
        # Same-owner retry is allowed only while this lease is valid. The
        # pre-claim maintenance closes an expired wait; it never renders/sends.
        mark_pending(repo, job["id"], token, attempt)
        expire_pending_adoptions(repo)
        raise
    except DeliveryConfirmed:
        # A durable external ACK is authoritative; maintenance finishes local state.
        raise
    except ContractReconciliationRequired:
        # No callback replay. Explicit ledger/adoption reconciliation is required.
        raise
    except Exception as error:
        code = (
            "AGENT_CONTRACT_FAILED"
            if isinstance(error, ContractViolation)
            else "TELEMETRY_RECONCILIATION_REQUIRED"
            if isinstance(error, TelemetryReconciliationRequired)
            else "VERTICAL_SLICE_FAILED"
        )
        try:
            repo.transition(job["id"], token, "FAILED", {"code": code})
        except Exception:
            pass
        raise
    finally:
        stop.set()
        thread.join(timeout=2)


def resume_render_adoption(config, settings, claim):
    """Explicit local retry only; no renderer/source call, recovery or delivery."""
    repo = Repository(config)
    job = claim["id"]
    token = claim["lease_token"]
    attempt = claim["attempt"]
    expire_pending_adoptions(repo)
    owned = repo.assert_owner(job, token, attempt=attempt)
    if owned["state"] != "RENDERING":
        raise ValueError("Adoption retry requires current RENDERING owner")
    work = Path(settings["spool_root"]) / job / "attempts" / str(attempt)
    result = json.loads((work / "output/render-result.json").read_text())
    if (
        owned["command"] == "did_you_know"
        and contract_status(repo, job, attempt, "quality") is not None
    ):
        status = contract_status(repo, job, attempt, "quality")
        if status in ("RUNNING", "SUCCEEDED"):
            reconcile_stage(
                repo, job, token, attempt, "quality", attempt, settings=settings, result=result
            )
        else:
            run_stage(
                repo,
                job,
                token,
                attempt,
                "quality",
                result,
                lambda: adopt_render(config, repo, job, token, attempt, result, settings),
                retry_adoption=True,
                retryable_errors=(AdoptionPending,),
            )
    else:
        observe_owned(
            repo,
            job,
            token,
            attempt,
            "quality",
            lambda: adopt_render(config, repo, job, token, attempt, result, settings),
        )
    content = json.loads(
        (
            Path(settings["input_root"]) / job / "attempts" / str(attempt) / "input/content.json"
        ).read_text()
    )
    # Read diagnostics only from this claim epoch's authoritative history.
    epoch = 0
    legacy_epoch = 0
    failures = []
    for event in repo.timeline(job):
        details = json.loads(event["details"])
        if event["event"] == "job.claimed":
            legacy_epoch += 1
            epoch = claim_attempt(details, legacy_epoch)
        if (
            epoch == attempt
            and event["event"] == "job.state"
            and details.get("state") == "PROCESSING"
        ):
            failures = details.get("fallbacks", [])
    manifest = {
        "app": config.app,
        "job_id": job,
        "content": content,
        "render": result,
        "external_delivery": False,
        "delivery": "AWAITING_TEST_BOT_AND_VISUAL_REVIEW",
    }
    output = {
        "job_id": job,
        "state": "VALIDATING",
        "source": content.get("source", "NOT_RECORDED"),
        "fallbacks": failures,
        "render": result,
        "external_delivery": False,
    }
    owned_views(config, repo, job, token, attempt, manifest, output, validating=True)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=["proxy", "renderer", "weather", "did_you_know"])
    args = parser.parse_args()
    config = Config.load(args.config)
    settings = tomllib.loads(args.config.read_text())["slice"]
    if args.action == "proxy":
        with EgressServer(config) as server:
            server.serve_forever()
    elif args.action == "renderer":
        renderer(config, settings)
    elif args.action == "did_you_know":
        secret = os.environ["STUDIO_GATEWAY_SECRET"]
        repo = Repository(config)
        gateway = CommandGateway(config, repo, secret)
        if (
            "did_you_know" not in config.command_allowlist
            or len(config.owners.get("telegram", ())) != 1
        ):
            raise ValueError("Manual owner configuration required")
        now = time.time()
        body = {
            "channel": "telegram",
            "actor": config.owners["telegram"][0],
            "event_id": uuid.uuid4().hex,
            "command": "did_you_know",
            "priority": "NORMAL",
        }
        job, _ = gateway.accept(body, now, gateway.signature(body, now))
        claim = repo.claim(job_id=job["id"])
        if claim is None:
            raise RuntimeError("Queue busy")
        print(json.dumps(execute(config, settings, secret, claim=claim), ensure_ascii=False))
    else:
        print(
            json.dumps(
                execute(config, settings, os.environ["STUDIO_GATEWAY_SECRET"]), ensure_ascii=False
            )
        )


if __name__ == "__main__":
    main()
