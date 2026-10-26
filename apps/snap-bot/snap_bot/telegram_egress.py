"""Owner-only transport. Run in the dedicated egress oneshot, never renderer.

Two channels: "test" (TG_TEST_TOKEN/TG_TEST_CHAT, the pinned lab bot) and "main"
(TG_TOKEN/TG_CHAT, the owner's main bot, pinned by the caller from the release manifest,
never from the environment). Both deliver only to a verified private owner chat.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any

from studio_core import delivery

# Shared receipt layer (core 0.11.0); re-exported for callers and tests.
from studio_core.config import Config
from studio_core.delivery import (
    DeliveryConfirmed as DeliveryConfirmed,
)
from studio_core.delivery import atomic_json as atomic_json
from studio_core.delivery import (
    receipt_metadata,
    remember_review,
)
from studio_core.jobs import LeaseLost, Repository
from studio_core.network.egress import PinnedHTTPSConnection, validate_target
from studio_core.storage import Storage

from .did_you_know import validate_content
from .render import video_filename
from .weather import content_hash

EXPECTED_BOT = "khalid_snap_v2_test_bot"
CHANNELS = ("test", "main")
BOT_NAME = r"[A-Za-z][A-Za-z0-9_]{1,29}[Bb][Oo][Tt]"


def main_bot(value: str) -> str:
    """The main bot's username, never the lab bot's."""
    if not re.fullmatch(BOT_NAME, value) or value.lower() == EXPECTED_BOT:
        raise ValueError("Main bot not pinned")
    return value


def channel_credentials(channel: str, pinned: str | None = None) -> tuple[str, str, str]:
    """Token, chat and expected bot username for one channel, from its own variables only."""
    if channel == "test":
        return os.environ.get("TG_TEST_TOKEN", ""), os.environ.get("TG_TEST_CHAT", ""), EXPECTED_BOT
    if channel == "main":
        bot = main_bot(pinned or "")
        return os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", ""), bot
    raise ValueError("Unknown delivery channel")


def api(token: str, method: str, body: bytes = b"", content_type: str = "application/json") -> Any:
    if method not in {
        "getMe",
        "getChat",
        "sendVideo",
        "sendPhoto",
        "getUpdates",
        "getWebhookInfo",
        "sendMessage",
        "setMyCommands",
    }:
        raise ValueError("Unsupported Telegram method")
    host, address, path = validate_target(
        "https://api.telegram.org/bot" + token + "/" + method, ("api.telegram.org",)
    )
    connection = PinnedHTTPSConnection(host, address, 45)
    try:
        connection.request("POST", path, body, {"Content-Type": content_type})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Telegram response too large")
        result = json.loads(raw)
        if response.status != 200 or result.get("ok") is not True:
            # Never include server descriptions or request URLs in logs.
            raise RuntimeError("Telegram rejected request: " + str(response.status))
        return result["result"]
    finally:
        connection.close()


def multipart(
    chat: str, video: bytes, content_type: str = "weather", channel: str = "test"
) -> tuple[bytes, str]:
    if channel == "main":
        caption = "هل تعلم؟\nتابعني للمزيد 👻" if content_type == "did_you_know" else "طقس الرياض"
    else:
        caption = (
            "اختبار Snap V2 — هل تعلم؟\nتابعني للمزيد 👻"
            if content_type == "did_you_know"
            else "اختبار Snap V2 — طقس الرياض، فيديو تجريبي مستقل"
        )
    return delivery.multipart(chat, video, caption, video_filename(content_type))


def reconcile_evidence_sweep(config: Config, after: str = "", limit: int = 100) -> dict[str, Any]:
    return delivery.reconcile_evidence_sweep(
        config,
        after,
        limit,
        bot=EXPECTED_BOT,
        reconcile=lambda config, job_id, video_sha: reconcile_published(config, job_id, video_sha),
    )


def reconcile_published(
    config: Config,
    job_id: str,
    video_sha: str,
    review_mode: str | None = None,
    bot: str = EXPECTED_BOT,
) -> dict[str, Any]:
    return delivery.reconcile_published(config, job_id, video_sha, review_mode, bot=bot)


def confirmed_result(
    config: Config, job_id: str, digest: str, review_mode: str | None, bot: str = EXPECTED_BOT
) -> dict[str, Any]:
    try:
        return reconcile_published(config, job_id, digest, review_mode, bot)
    except Exception as cause:
        raise DeliveryConfirmed(job_id, digest, review_mode) from cause


from .send_evidence import delivery_evidence  # noqa: E402  # import cycle: send_evidence uses api


@delivery_evidence
def deliver(
    config: Config,
    job_id: str,
    reviewed_sha: str,
    transport: Any = api,
    lease_token: str | None = None,
    review_mode: str | None = None,
    channel: str = "test",
    pinned_bot: str | None = None,
) -> Any:
    token, chat, bot = channel_credentials(channel, pinned_bot)
    if (
        getattr(transport, "__wrapped__", transport) is api
        and not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{30,80}", token)
    ) or not re.fullmatch(r"[0-9]+", chat):
        raise ValueError("Invalid " + channel + " credentials")
    root = Storage(config).workspace(job_id)
    result = json.loads((root / "output/render-result.json").read_text())
    content = json.loads((root / "input/content.json").read_text())
    if content.get("type") == "did_you_know":
        validate_content(content)
        if review_mode != "HUMAN_FRAME" or result.get("safe_area", {}).get("result") != "PASS":
            raise ValueError("Fact needs human preview review")
    path = root / "output" / video_filename(content.get("type", "weather"))
    if path.is_symlink() or not 0 < path.stat().st_size <= 50_000_000:
        raise ValueError("Unsafe video")
    video = path.read_bytes()
    digest = hashlib.sha256(video).hexdigest()
    if (
        digest != result["video_sha256"]
        or digest != reviewed_sha
        or result["qa"]["result"] != "PASS"
        or result["glyph_coverage"] != "PASS"
    ):
        raise ValueError("Unreviewed video")
    if (
        content_hash(content) != result["content_hash"]
        or content["expires_at"] <= time.time()
        or not all(result["network_isolation"].values())
    ):
        raise ValueError("Invalid content or isolation proof")
    repo = Repository(config)
    if content.get("type") == "did_you_know":
        from .owned_contract_adapter import require_delivery_contract

        with repo.connect() as db:
            require_delivery_contract(db, job_id, result)
    identity = transport(token, "getMe")
    if identity.get("username") != bot:
        raise ValueError("Wrong " + channel + " bot")
    owner = transport(token, "getChat", json.dumps({"chat_id": chat}).encode())
    if owner.get("type") != "private" or str(owner.get("id")) != chat:
        raise ValueError("Unverified private owner")
    repo = Repository(config)
    # Durable idempotency before external effects. Unknown outcomes never retry automatically.
    with repo.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_outbox(video_sha TEXT PRIMARY KEY, job_id TEXT NOT NULL, state TEXT NOT NULL, message_id INTEGER, updated_at REAL NOT NULL)"
        )
        try:
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS telegram_outbox_job ON telegram_outbox(job_id)"
            )
        except sqlite3.IntegrityError:
            raise RuntimeError("OUTBOX_RECONCILIATION_REQUIRED") from None
        receipt_metadata(db)
        old = db.execute(
            "SELECT * FROM telegram_outbox WHERE video_sha=? OR job_id=?", (digest, job_id)
        ).fetchone()
        if old:
            if old["job_id"] == job_id and old["video_sha"] != digest:
                if lease_token:
                    repo.transition(
                        job_id, lease_token, "FAILED", {"code": "DELIVERY_ARTIFACT_CHANGED"}, db=db
                    )
                changed_artifact = True
            else:
                changed_artifact = False
            existing: dict[str, Any] | None = {
                "delivery": old["state"],
                "message_id": old["message_id"],
                "replayed": False,
            }
        else:
            existing = None
            changed_artifact = False
    if changed_artifact:
        raise RuntimeError("DELIVERY_ARTIFACT_CHANGED; reconciliation required")
    if existing:
        if (
            lease_token is None
            and existing["delivery"] == "PUBLISHED"
            and repo.get(job_id)["state"] != "COMPLETED"  # type: ignore[index]  # the outbox row proves the job exists
        ):
            claim = repo.claim(job_id=job_id)
            if not claim:
                if repo.get(job_id)["state"] == "COMPLETED":  # type: ignore[index]
                    return confirmed_result(config, job_id, digest, review_mode, bot)
                raise LeaseLost("Reviewed job unavailable")
            lease_token = claim["lease_token"]
            for state in (
                "RESEARCHING",
                "PROCESSING",
                "DOWNLOADING",
                "DESIGNING",
                "RENDERING",
                "VALIDATING",
            ):
                repo.transition(job_id, lease_token, state, {"resume_existing_artifact": True})
        existing.update({"job_id": job_id, "video_sha256": digest, "review_mode": review_mode})
        if lease_token:
            if existing["delivery"] == "PUBLISHED":
                with repo.transaction() as db:
                    repo.transition(
                        job_id,
                        lease_token,
                        "DELIVERING",
                        {"reused_message_id": existing["message_id"]},
                        db=db,
                    )
                    # The transition authenticates ownership before storing provenance.
                    prior_manifest = (
                        json.loads((root / "manifest.json").read_text())
                        if (root / "manifest.json").exists()
                        else {}
                    )
                    prior_mode = prior_manifest.get("delivery_receipt", {}).get("review_mode")
                    remember_review(db, job_id, digest, prior_mode or review_mode)
                    db.execute(
                        "INSERT OR IGNORE INTO deliveries VALUES(?,?,?)",
                        (digest + ":" + job_id, job_id, "PUBLISHED"),
                    )
                    repo.transition(
                        job_id,
                        lease_token,
                        "COMPLETED",
                        {"reused_message_id": existing["message_id"]},
                        db=db,
                    )
                repo.mark_artifacts_terminal(job_id, "PUBLISHED")
            else:
                repo.transition(job_id, lease_token, "FAILED", {"code": "DELIVERY_UNKNOWN"})
            if existing["delivery"] == "PUBLISHED":
                proof = confirmed_result(config, job_id, digest, review_mode, bot)
                proof["replayed"] = False
                return proof
            manifest = (
                json.loads((root / "manifest.json").read_text())
                if (root / "manifest.json").exists()
                else {"job_id": job_id}
            )
            manifest.update(
                {
                    "external_delivery": existing["delivery"] == "PUBLISHED",
                    "delivery": existing["delivery"],
                    "delivery_receipt": existing,
                }
            )
            Storage(config).manifest(job_id, manifest)
        if existing["delivery"] == "PUBLISHED":
            proof = confirmed_result(config, job_id, digest, review_mode, bot)
            proof["replayed"] = False
            return proof
        return existing
    if lease_token is None:
        claim = repo.claim(job_id=job_id)
        if not claim or claim["id"] != job_id:
            raise LeaseLost("Reviewed job unavailable")
        lease = claim["lease_token"]
        for state in (
            "RESEARCHING",
            "PROCESSING",
            "DOWNLOADING",
            "DESIGNING",
            "RENDERING",
            "VALIDATING",
        ):
            repo.transition(job_id, lease, state, {"resume_existing_artifact": True})
    else:
        lease = lease_token
        job = repo.get(job_id)
        if not job or job["state"] != "VALIDATING" or job["lease_token"] != lease:
            raise LeaseLost("Validation lease missing")
        repo.heartbeat(job_id, lease)
    with repo.transaction() as db:
        repo.assert_owner(job_id, lease, db=db)
        if content.get("type") == "did_you_know":
            require_delivery_contract(db, job_id, result)
        concurrent = db.execute(
            "SELECT 1 FROM telegram_outbox WHERE video_sha=? OR job_id=?", (digest, job_id)
        ).fetchone()
        if concurrent:
            raise RuntimeError("DELIVERY_ALREADY_RESERVED; reconciliation required")
        remember_review(db, job_id, digest, review_mode)
        db.execute(
            "INSERT INTO telegram_outbox VALUES(?,?,?,NULL,?)",
            (digest, job_id, "SENDING", time.time()),
        )
        db.execute("INSERT INTO deliveries VALUES(?,?,?)", (digest, job_id, "SENDING"))
        repo.transition(job_id, lease, "DELIVERING", db=db)
    try:
        # Network timeout is longer than lease; fence the effect for one bounded request.
        import threading

        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(config.lease_seconds / 3):
                try:
                    repo.heartbeat(job_id, lease)
                except LeaseLost:
                    return
                except sqlite3.OperationalError:
                    continue

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            body, kind = multipart(chat, video, content.get("type", "weather"), channel)
            sent = transport(token, "sendVideo", body, kind)
            if str(sent.get("chat", {}).get("id")) != chat or not isinstance(
                sent.get("message_id"), int
            ):
                raise ValueError("Unverified Telegram acknowledgement")
        finally:
            stop.set()
            thread.join(timeout=2)
        # Deposit the verified external ACK independently of state reconciliation.
        # A later local transition failure must never roll this transaction back.
        with repo.transaction() as db:
            db.execute(
                "UPDATE telegram_outbox SET state=?,message_id=?,updated_at=? WHERE video_sha=?",
                ("PUBLISHED", sent["message_id"], time.time(), digest),
            )
            db.execute("UPDATE deliveries SET state=? WHERE id=?", ("PUBLISHED", digest))
        try:
            repo.transition(job_id, lease, "COMPLETED", {"message_id": sent["message_id"]})
        except Exception:
            # Durable PUBLISHED remains available to claim() and the evidence sweep.
            with repo.transaction() as db:
                repo.reconcile_delivery(job_id, db=db)
            return confirmed_result(config, job_id, digest, review_mode, bot)
        repo.mark_artifacts_terminal(job_id, "PUBLISHED")
        return confirmed_result(config, job_id, digest, review_mode, bot)
    except Exception:
        with repo.transaction() as db:
            acknowledged = db.execute(
                "SELECT state FROM telegram_outbox WHERE video_sha=?", (digest,)
            ).fetchone()
            if acknowledged and acknowledged["state"] == "PUBLISHED":
                raise DeliveryConfirmed(job_id, digest, review_mode) from None
            db.execute(
                "UPDATE telegram_outbox SET state=?,updated_at=? WHERE video_sha=?",
                ("DELIVERY_UNKNOWN", time.time(), digest),
            )
            db.execute("UPDATE deliveries SET state=? WHERE id=?", ("DELIVERY_UNKNOWN", digest))
        try:  # noqa: SIM105  # explicit best-effort block
            repo.transition(job_id, lease, "FAILED", {"code": "DELIVERY_UNKNOWN"})
        except LeaseLost:
            pass
        raise RuntimeError("DELIVERY_UNKNOWN; manual reconciliation required") from None
