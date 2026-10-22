"""Durable Status-only, weather-only outbox; no network or automatic replay.

API acceptance, recipient delivery and manual Status publication are distinct.
Only signed account/recipient-bound webhooks establish recipient delivery.
"""

import hashlib
import hmac
import os
import stat
import uuid
from pathlib import Path

from studio_core.observability.agent_runs import digest

from .agents import PLAN_SHA
from .weather import content_hash
from .whatsapp_contract import (
    DeliveryBlocked,
    finite,
    identifier,
    parse_json,
    require,
    response_id,
    sha,
    validate_review,
)

MAX_VIDEO = 16000000


def read_bound(root, relative, limit):
    """Open every directory via dir_fd/O_NOFOLLOW; never create on a read."""
    parts = Path(relative).parts
    require(parts and not Path(relative).is_absolute() and ".." not in parts, "WA_ARTIFACT_INVALID")
    descriptor = None
    try:
        # Absolute data root itself must also contain no symlink components.
        absolute = Path(root).absolute()
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in absolute.parts[1:] + parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit, "WA_ARTIFACT_INVALID"
            )
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            require(
                len(raw) <= limit
                and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                "WA_ARTIFACT_CHANGED",
            )
            return raw
    except OSError:
        raise DeliveryBlocked("WA_ARTIFACT_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class WhatsAppOutbox:
    def __init__(self, repository, identity):
        require(repository.config.app == "status-bot-v2", "WA_APP_SCOPE_INVALID")
        self.repo, self.identity = repository, identity
        with self.repo.transaction() as db:
            self.scope(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS whatsapp_outbox(
                  id TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id),
                  app TEXT NOT NULL, attempt INTEGER NOT NULL, identity_binding TEXT NOT NULL,
                  content_hash TEXT NOT NULL, video_sha256 TEXT NOT NULL,
                  manifest_sha256 TEXT NOT NULL, review_sha256 TEXT UNIQUE NOT NULL,
                  state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                  send_started_at REAL, message_tag TEXT UNIQUE, error_code TEXT,
                  UNIQUE(identity_binding,content_hash));
                CREATE TABLE IF NOT EXISTS whatsapp_windows(
                  identity_binding TEXT PRIMARY KEY, last_inbound_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS whatsapp_receipts(
                  message_tag TEXT NOT NULL, state TEXT NOT NULL, at REAL NOT NULL,
                  PRIMARY KEY(message_tag,state));
            """)

    def scope(self, db):
        require(
            not db.execute(
                "SELECT 1 FROM jobs WHERE app != ? LIMIT 1", ("status-bot-v2",)
            ).fetchone(),
            "WA_DATABASE_SCOPE_INVALID",
        )

    def bundle(self, job_id, attempt):
        require(
            identifier(job_id, "[0-9a-f]{32}") and type(attempt) is int and attempt > 0,
            "WA_JOB_INVALID",
        )
        root = self.repo.config.data_root
        base = "renders/" + job_id + "/"
        manifest = parse_json(read_bound(root, base + "manifest.json", 1048576))
        require(
            isinstance(manifest, dict)
            and manifest.get("app") == "status-bot-v2"
            and manifest.get("job_id") == job_id
            and manifest.get("state") == "VALIDATING"
            and manifest.get("destination") == "whatsapp"
            and manifest.get("external_delivery") is False
            and manifest.get("synthetic_source") is False,
            "WA_MANIFEST_INVALID",
        )
        source = parse_json(read_bound(root, base + "input/content.json", 1048576))
        source_payload = manifest.get("source_payload")
        render = parse_json(read_bound(root, base + "output/render-result.json", 1048576))
        require(
            isinstance(source, dict)
            and isinstance(render, dict)
            and manifest.get("content") == source
            and manifest.get("render") == render
            and isinstance(source_payload, dict)
            and source_payload.get("content") == source
            and source.get("type") == "weather"
            and source.get("sacred") is False
            and source.get("source") != "synthetic-fixture"
            and source.get("content_hash") == content_hash(source)
            and finite(source.get("expires_at"))
            and source["expires_at"] > self.repo.clock()
            and render.get("job_id") == job_id
            and render.get("content_hash") == source["content_hash"],
            "WA_CONTENT_INVALID",
        )
        qa = render.get("qa")
        isolation = render.get("network_isolation")
        require(
            isinstance(qa, dict)
            and qa.get("result") == "PASS"
            and qa.get("codec") == "h264"
            and qa.get("width") == 1080
            and qa.get("height") == 1920
            and qa.get("audio") == "not_required_for_weather_card"
            and render.get("glyph_coverage") == "PASS"
            and isinstance(isolation, dict)
            and all(
                isolation.get(k) is True for k in ("ipv4", "ipv6", "udp", "egress_unreachable")
            ),
            "WA_QA_INVALID",
        )
        video = read_bound(root, base + "output/weather.mp4", MAX_VIDEO)
        require(
            hashlib.sha256(video).hexdigest() == render.get("video_sha256")
            and qa.get("bytes") == len(video),
            "WA_VIDEO_MISMATCH",
        )
        quality = {
            "job_id": job_id,
            "attempt": attempt,
            "content_hash": source["content_hash"],
            "video_sha256": render["video_sha256"],
            "qa": "PASS",
            "visual_review": "PENDING",
        }
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT * FROM agent_runs WHERE app=? AND job_id=? AND attempt=? AND role=?",
                ("status-bot-v2", job_id, attempt, "quality"),
            ).fetchone()
        require(
            row is not None
            and row["status"] == "SUCCEEDED"
            and row["contract_sha"] == PLAN_SHA
            and row["input_sha"] == digest({"source_payload": source_payload, "render": render})
            and row["output_sha"] == digest(quality),
            "WA_QUALITY_LINEAGE_INVALID",
        )
        return manifest, video

    def prepare(self, job_id, token, attempt, review, signature, *, approval_commit=None):
        validate_review(review, signature, self.identity, self.repo.clock())
        manifest, _ = self.bundle(job_id, attempt)
        require(
            review["job_id"] == job_id
            and review["attempt"] == attempt
            and review["manifest_sha256"] == sha(manifest)
            and review["content_hash"] == manifest["content"]["content_hash"]
            and review["video_sha256"] == manifest["render"]["video_sha256"],
            "WA_REVIEW_ARTIFACT_MISMATCH",
        )
        with self.repo.owned_transaction(job_id, token, attempt=attempt) as db:
            self.scope(db)
            owned = self.repo.assert_owner(job_id, token, attempt=attempt, db=db)
            require(
                owned["state"] == "VALIDATING" and owned["command"] == "weather",
                "WA_JOB_NOT_VALIDATED",
            )
            require(
                not db.execute(
                    "SELECT 1 FROM whatsapp_outbox WHERE job_id=? OR "
                    "(identity_binding=? AND content_hash=?) OR review_sha256=?",
                    (job_id, self.identity.binding, review["content_hash"], sha(review)),
                ).fetchone(),
                "WA_DUPLICATE_BLOCKED",
            )
            outbox_id = uuid.uuid4().hex
            expires = min(
                review["expires_at"], manifest["content"]["expires_at"], owned["expires_at"]
            )
            # Trusted controller hook: DB-only authorization and approval receipt
            # share this commit. No IO/network and no partially approved ticket.
            if approval_commit is not None:
                approval_commit(db, owned, outbox_id, review)
            db.execute(
                "INSERT INTO whatsapp_outbox(id,job_id,app,attempt,identity_binding,content_hash,"
                "video_sha256,manifest_sha256,review_sha256,state,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outbox_id,
                    job_id,
                    "status-bot-v2",
                    attempt,
                    self.identity.binding,
                    review["content_hash"],
                    review["video_sha256"],
                    sha(manifest),
                    sha(review),
                    "PREPARED",
                    self.repo.clock(),
                    expires,
                ),
            )
            db.execute("INSERT INTO deliveries VALUES(?,?,?)", (outbox_id, job_id, "WA_PREPARED"))
            self.repo.event(db, job_id, "whatsapp.prepared", {"outbox_id": outbox_id})
        return self.receipt(outbox_id)

    def receipt(self, outbox_id):
        require(identifier(outbox_id, "[0-9a-f]{32}"), "WA_OUTBOX_INVALID")
        with self.repo.connect() as db:
            row = db.execute(
                "SELECT * FROM whatsapp_outbox WHERE id=? AND identity_binding=?",
                (outbox_id, self.identity.binding),
            ).fetchone()
        require(row is not None, "WA_OUTBOX_UNAVAILABLE")
        return {
            k: row[k]
            for k in (
                "id",
                "job_id",
                "app",
                "attempt",
                "state",
                "content_hash",
                "video_sha256",
                "created_at",
                "expires_at",
                "error_code",
            )
        } | {
            "destination": "whatsapp",
            "publication": "MANUAL_PUBLICATION_PENDING",
            "recipient_delivery_confirmed": row["state"] in ("DELIVERED", "READ"),
            "automatic_retry": False,
        }

    def _window(self, db):
        row = db.execute(
            "SELECT last_inbound_at FROM whatsapp_windows WHERE identity_binding=?",
            (self.identity.binding,),
        ).fetchone()
        require(
            row is not None and 0 <= self.repo.clock() - row[0] < 86400, "WA_SERVICE_WINDOW_CLOSED"
        )

    def begin(self, outbox_id, token, review, signature):
        validate_review(review, signature, self.identity, self.repo.clock())
        receipt = self.receipt(outbox_id)
        manifest, video = self.bundle(receipt["job_id"], receipt["attempt"])
        require(sha(manifest) == review["manifest_sha256"], "WA_REVIEW_ARTIFACT_MISMATCH")
        with self.repo.owned_transaction(
            receipt["job_id"], token, attempt=receipt["attempt"]
        ) as db:
            self.scope(db)
            self._window(db)
            row = db.execute("SELECT * FROM whatsapp_outbox WHERE id=?", (outbox_id,)).fetchone()
            require(
                row["state"] == "PREPARED"
                and row["expires_at"] > self.repo.clock()
                and row["review_sha256"] == sha(review)
                and row["manifest_sha256"] == sha(manifest),
                "WA_DISPATCH_BLOCKED",
            )
            owned = self.repo.assert_owner(row["job_id"], token, attempt=row["attempt"], db=db)
            require(owned["state"] == "VALIDATING", "WA_JOB_NOT_VALIDATED")
            self.repo.transition(row["job_id"], token, "DELIVERING", db=db)
            db.execute("UPDATE whatsapp_outbox SET state='UPLOADING' WHERE id=?", (outbox_id,))
            db.execute("UPDATE deliveries SET state='WA_UPLOADING' WHERE id=?", (outbox_id,))
            self.repo.event(db, row["job_id"], "whatsapp.upload_intent", {"outbox_id": outbox_id})
        # Immutable verified bytes, never pass a path to the transport.
        return video

    def send_intent(self, outbox_id, token):
        receipt = self.receipt(outbox_id)
        with self.repo.owned_transaction(
            receipt["job_id"], token, attempt=receipt["attempt"]
        ) as db:
            self.scope(db)
            self._window(db)
            row = db.execute("SELECT * FROM whatsapp_outbox WHERE id=?", (outbox_id,)).fetchone()
            require(
                row["state"] == "UPLOADING" and row["expires_at"] > self.repo.clock(),
                "WA_DISPATCH_EXPIRED",
            )
            db.execute(
                "UPDATE whatsapp_outbox SET state='SEND_INTENT',send_started_at=? WHERE id=?",
                (self.repo.clock(), outbox_id),
            )
            db.execute("UPDATE deliveries SET state='WA_SEND_INTENT' WHERE id=?", (outbox_id,))
            self.repo.event(db, row["job_id"], "whatsapp.send_intent", {"outbox_id": outbox_id})

    def accept(self, outbox_id, response):
        message = response_id(response, "send", self.identity.recipient)
        with self.repo.transaction() as db:
            self.scope(db)
            row = db.execute(
                "SELECT * FROM whatsapp_outbox WHERE id=? AND identity_binding=?",
                (outbox_id, self.identity.binding),
            ).fetchone()
            require(row is not None and row["state"] == "SEND_INTENT", "WA_ACK_CONFLICT")
            tag = self.identity.tag("message", message)
            require(
                not db.execute(
                    "SELECT 1 FROM whatsapp_outbox WHERE message_tag=?", (tag,)
                ).fetchone(),
                "WA_ACK_CONFLICT",
            )
            # A verified ACK may arrive after lease expiry; it still belongs to
            # this durable single send intent, never authorizes another send.
            db.execute(
                "UPDATE whatsapp_outbox SET state='API_ACCEPTED',message_tag=? WHERE id=?",
                (tag, outbox_id),
            )
            db.execute("UPDATE deliveries SET state='WA_API_ACCEPTED' WHERE id=?", (outbox_id,))
            self.repo.event(db, row["job_id"], "whatsapp.api_accepted", {"outbox_id": outbox_id})
            self._settle(db, outbox_id)
        return self.receipt(outbox_id)

    def fail(self, outbox_id, phase, error):
        # No raw exception/reply, and no retry state, even after known rejection.
        known = isinstance(error, DeliveryBlocked) and identifier(
            str(error), "WA_API_REJECTED_[0-9]{1,7}"
        )
        state = (
            "REJECTED" if known else ("UPLOAD_UNKNOWN" if phase == "upload" else "DELIVERY_UNKNOWN")
        )
        code = str(error) if known else "WA_" + state
        with self.repo.transaction() as db:
            self.scope(db)
            row = db.execute(
                "SELECT * FROM whatsapp_outbox WHERE id=? AND identity_binding=?",
                (outbox_id, self.identity.binding),
            ).fetchone()
            expected = "UPLOADING" if phase == "upload" else "SEND_INTENT"
            if row is None or row["state"] != expected:
                return
            db.execute(
                "UPDATE whatsapp_outbox SET state=?,error_code=? WHERE id=?",
                (state, code, outbox_id),
            )
            db.execute("UPDATE deliveries SET state=? WHERE id=?", ("WA_" + state, outbox_id))
            self.repo.event(
                db,
                row["job_id"],
                "whatsapp.blocked_or_unknown",
                {"outbox_id": outbox_id, "code": code},
            )

    def _settle(self, db, outbox_id):
        row = db.execute("SELECT * FROM whatsapp_outbox WHERE id=?", (outbox_id,)).fetchone()
        if row["state"] not in ("API_ACCEPTED", "SENT", "DELIVERED", "READ", "FAILED_REMOTE"):
            return
        events = {
            x["state"]
            for x in db.execute(
                "SELECT state FROM whatsapp_receipts WHERE message_tag=? AND at>=?",
                (row["message_tag"], row["send_started_at"] - 5),
            )
        }
        events.add(row["state"])
        state = next(
            (s for s in ("READ", "DELIVERED", "FAILED_REMOTE", "SENT") if s in events), row["state"]
        )
        if row["state"] == "READ" or (row["state"] == "DELIVERED" and state != "READ"):
            return
        db.execute("UPDATE whatsapp_outbox SET state=? WHERE id=?", (state, outbox_id))
        db.execute("UPDATE deliveries SET state=? WHERE id=?", ("WA_" + state, outbox_id))
        if state != row["state"]:
            self.repo.event(
                db,
                row["job_id"],
                "whatsapp.receipt_verified",
                {"outbox_id": outbox_id, "state": state},
            )
        # Verified delivery finishes this exact computation/delivery attempt.
        # It does not publish a Status or grant permission to release artifacts.
        if state in ("DELIVERED", "READ"):
            job = db.execute(
                "SELECT app,state,error_code FROM jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
            epoch = db.execute(
                "SELECT count(*) FROM events WHERE job_id=? AND event='job.claimed'",
                (row["job_id"],),
            ).fetchone()[0]
            if (
                job
                and job["app"] == "status-bot-v2"
                and epoch == row["attempt"]
                and (
                    job["state"] == "DELIVERING"
                    or (job["state"] == "FAILED" and job["error_code"] == "DELIVERY_UNKNOWN")
                )
            ):
                db.execute(
                    "UPDATE jobs SET state='COMPLETED',error_code=NULL,updated_at=?,lease_token=NULL,"
                    "lease_until=NULL WHERE id=?",
                    (self.repo.clock(), row["job_id"]),
                )
                self.repo.event(
                    db, row["job_id"], "job.whatsapp_delivery_confirmed", {"outbox_id": outbox_id}
                )
        # Never use PUBLISHED: recipient ACK is not publishing a WhatsApp Status.

    def webhook(self, raw, signature):
        require(
            type(raw) is bytes
            and 0 < len(raw) <= 1048576
            and identifier(signature, "sha256=[0-9a-f]{64}"),
            "WA_WEBHOOK_UNAUTHENTICATED",
        )
        expected = "sha256=" + hmac.new(self.identity.webhook_key, raw, hashlib.sha256).hexdigest()
        require(hmac.compare_digest(signature, expected), "WA_WEBHOOK_UNAUTHENTICATED")
        value = parse_json(raw)
        require(
            isinstance(value, dict)
            and value.get("object") == "whatsapp_business_account"
            and isinstance(value.get("entry"), list)
            and len(value["entry"]) <= 32,
            "WA_WEBHOOK_INVALID",
        )
        now = self.repo.clock()
        inbound = []
        receipts = []
        for entry in value["entry"]:
            require(
                isinstance(entry, dict)
                and entry.get("id") == self.identity.business_account
                and isinstance(entry.get("changes"), list)
                and len(entry["changes"]) <= 32,
                "WA_WEBHOOK_SCOPE_INVALID",
            )
            for change in entry["changes"]:
                require(
                    isinstance(change, dict) and change.get("field") == "messages",
                    "WA_WEBHOOK_SCOPE_INVALID",
                )
                payload = change.get("value")
                require(
                    isinstance(payload, dict)
                    and payload.get("messaging_product") == "whatsapp"
                    and isinstance(payload.get("metadata"), dict)
                    and payload["metadata"].get("phone_number_id") == self.identity.phone_number,
                    "WA_WEBHOOK_SCOPE_INVALID",
                )
                for key in ("messages", "statuses"):
                    items = payload.get(key, [])
                    require(isinstance(items, list) and len(items) <= 128, "WA_WEBHOOK_INVALID")
                    for item in items:
                        require(
                            isinstance(item, dict)
                            and identifier(item.get("timestamp"), "[0-9]{1,12}"),
                            "WA_WEBHOOK_INVALID",
                        )
                        at = int(item["timestamp"])
                        require(0 < at <= now + 30, "WA_WEBHOOK_TIME_INVALID")
                        if key == "messages":
                            if (
                                item.get("from") == self.identity.recipient
                                and item.get("type")
                                in (
                                    "text",
                                    "image",
                                    "video",
                                    "audio",
                                    "document",
                                    "sticker",
                                    "location",
                                    "contacts",
                                    "interactive",
                                    "button",
                                    "reaction",
                                    "order",
                                )
                                and identifier(item.get("id"), r"wamid\.[A-Za-z0-9+/=_-]{1,250}")
                            ):
                                inbound.append(at)
                        elif item.get("recipient_id") == self.identity.recipient:
                            require(
                                identifier(item.get("id"), r"wamid\.[A-Za-z0-9+/=_-]{1,250}")
                                and item.get("status") in ("sent", "delivered", "read", "failed"),
                                "WA_WEBHOOK_INVALID",
                            )
                            states = {
                                "sent": "SENT",
                                "delivered": "DELIVERED",
                                "read": "READ",
                                "failed": "FAILED_REMOTE",
                            }
                            receipts.append(
                                (
                                    self.identity.tag("message", item["id"]),
                                    states[item["status"]],
                                    at,
                                )
                            )
        require(len(inbound) + len(receipts) <= 512, "WA_WEBHOOK_INVALID")
        with self.repo.transaction() as db:
            self.scope(db)
            if inbound:
                db.execute(
                    "INSERT INTO whatsapp_windows VALUES(?,?) ON CONFLICT(identity_binding) "
                    "DO UPDATE SET last_inbound_at=MAX(last_inbound_at,excluded.last_inbound_at)",
                    (self.identity.binding, max(inbound)),
                )
            db.execute("DELETE FROM whatsapp_receipts WHERE at<?", (now - 7 * 86400,))
            require(
                db.execute("SELECT count(*) FROM whatsapp_receipts").fetchone()[0] < 10000,
                "WA_WEBHOOK_STORAGE_LIMIT",
            )
            for tag, state, at in receipts:
                db.execute(
                    "INSERT INTO whatsapp_receipts VALUES(?,?,?) ON CONFLICT(message_tag,state) "
                    "DO UPDATE SET at=MAX(at,excluded.at)",
                    (tag, state, at),
                )
                row = db.execute(
                    "SELECT id FROM whatsapp_outbox WHERE message_tag=? AND identity_binding=?",
                    (tag, self.identity.binding),
                ).fetchone()
                if row:
                    self._settle(db, row["id"])
        return {
            "verified": True,
            "accepted_events": len(inbound) + len(receipts),
            "external_delivery": False,
        }
