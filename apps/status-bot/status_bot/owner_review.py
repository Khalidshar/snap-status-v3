"""Status-only human controller. No transport, scheduler or public send route.

Only a configured owner can attest to viewing the exact bound video. Serving
bytes is recorded, but is deliberately not represented as proof of watching.
"""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .whatsapp_contract import (
    identifier,
    json_bytes,
    parse_json,
    require,
    sha,
    sign_review,
    validate_review,
)


@dataclass(frozen=True, repr=False)
class OwnerConfig:
    origin: str
    owner: str
    login_secret: bytes = field(repr=False)
    session_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path == ""
            and self.origin == "https://" + parsed.netloc
            and parsed.hostname.isascii()
            and not any(c.isspace() for c in self.origin),
            "REVIEW_ORIGIN_INVALID",
        )
        require(identifier(self.owner, "[A-Za-z0-9_-]{1,64}"), "REVIEW_OWNER_INVALID")
        require(
            all(type(k) is bytes and len(k) >= 32 for k in (self.login_secret, self.session_key))
            and len(self.login_secret) <= 512
            and all(33 <= c <= 126 for c in self.login_secret)
            and self.login_secret != self.session_key,
            "REVIEW_KEYS_INVALID",
        )


class OwnerReview:
    def __init__(self, outbox: Any, config: OwnerConfig) -> None:
        self.outbox, self.repo, self.config = outbox, outbox.repo, config
        require(
            len(
                {
                    config.login_secret,
                    config.session_key,
                    outbox.identity.gate_key,
                    outbox.identity.webhook_key,
                }
            )
            == 4,
            "REVIEW_KEYS_INVALID",
        )
        with self.repo.transaction() as db:
            outbox.scope(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owner_sessions(
                  tag TEXT PRIMARY KEY, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS owner_login_budget(
                  id INTEGER PRIMARY KEY CHECK(id=1), began_at REAL NOT NULL, failures INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS owner_reviews(
                  id TEXT PRIMARY KEY, session_tag TEXT NOT NULL, job_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL, manifest_sha TEXT NOT NULL, video_sha TEXT NOT NULL,
                  content_sha TEXT NOT NULL, identity_binding TEXT NOT NULL, expires_at REAL NOT NULL,
                  state TEXT NOT NULL, media_served INTEGER NOT NULL DEFAULT 0,
                  outbox_id TEXT, approved_review TEXT,
                  UNIQUE(job_id,attempt));
            """)

    def tag(self, purpose: str, value: str) -> str:
        return hmac.new(
            self.config.session_key,
            (
                "status-bot-v2/owner/"
                + self.config.origin
                + "/"
                + self.config.owner
                + "/"
                + purpose
                + "/"
                + value
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

    def login(self, owner: object, secret: object) -> str:
        # Fixed single-owner bucket is durable and cannot be bypassed by changing
        # usernames, spoofing proxy/IP headers, or restarting the HTTP process.
        supplied = secret if type(secret) is bytes and len(secret) <= 4096 else b""
        valid = (
            type(owner) is str
            and hmac.compare_digest(owner.encode(), self.config.owner.encode())
            and hmac.compare_digest(
                hashlib.sha256(supplied).digest(), hashlib.sha256(self.config.login_secret).digest()
            )
        )
        cookie = secrets.token_hex(32)
        accepted = False
        with self.repo.transaction() as db:
            self.outbox.scope(db)
            now = self.repo.clock()
            row = db.execute("SELECT * FROM owner_login_budget WHERE id=1").fetchone()
            if row is None or now - row["began_at"] >= 300:
                db.execute("INSERT OR REPLACE INTO owner_login_budget VALUES(1,?,0)", (now,))
                failures = 0
            else:
                failures = row["failures"]
            if failures < 5 and valid:
                db.execute("DELETE FROM owner_sessions WHERE expires_at<=?", (now,))
                require(
                    db.execute("SELECT COUNT(*) FROM owner_sessions").fetchone()[0] < 32,
                    "REVIEW_SESSION_LIMIT",
                )
                db.execute(
                    "INSERT INTO owner_sessions VALUES(?,?)",
                    (self.tag("session", cookie), now + 900),
                )
                accepted = True
            elif failures < 5:
                db.execute("UPDATE owner_login_budget SET failures=failures+1 WHERE id=1")
        require(accepted, "REVIEW_LOGIN_DENIED")
        return cookie

    def session(self, cookie: Any, db: sqlite3.Connection | None = None) -> str:
        require(identifier(cookie, "[0-9a-f]{64}"), "REVIEW_SESSION_DENIED")
        if db is None:
            with self.repo.connect() as connection:
                return self.session(cookie, connection)
        self.outbox.scope(db)
        tag = self.tag("session", cookie)
        row = db.execute("SELECT expires_at FROM owner_sessions WHERE tag=?", (tag,)).fetchone()
        require(row is not None and row[0] > self.repo.clock(), "REVIEW_SESSION_DENIED")
        return tag

    def csrf(self, cookie: Any) -> str:
        self.session(cookie)
        return self.tag("csrf", cookie)

    def authorize(self, cookie: Any, csrf: Any, db: sqlite3.Connection | None = None) -> str:
        tag = self.session(cookie, db)
        require(
            identifier(csrf, "[0-9a-f]{64}")
            and hmac.compare_digest(csrf, self.tag("csrf", cookie)),
            "REVIEW_CSRF_DENIED",
        )
        return tag

    def logout(self, cookie: Any, csrf: Any) -> None:
        with self.repo.transaction() as db:
            tag = self.authorize(cookie, csrf, db)
            db.execute("DELETE FROM owner_sessions WHERE tag=?", (tag,))
        # Logout expires the session, not an already committed delivery approval.

    def pending(self, cookie: Any) -> list[dict[str, Any]]:
        with self.repo.connect() as db:
            self.session(cookie, db)
            rows = db.execute(
                "SELECT j.id,j.created_at FROM jobs j WHERE j.app='status-bot-v2' "
                "AND j.command='weather' AND j.state='VALIDATING' AND j.lease_until>? AND j.expires_at>? "
                "AND NOT EXISTS(SELECT 1 FROM whatsapp_outbox w WHERE w.job_id=j.id) "
                "ORDER BY j.created_at DESC LIMIT 100",
                (self.repo.clock(), self.repo.clock()),
            ).fetchall()
        return [dict(r) for r in rows]

    def _owned(self, job_id: Any) -> Any:
        require(identifier(job_id, "[0-9a-f]{32}"), "REVIEW_JOB_INVALID")
        job = self.repo.get(job_id)
        require(
            job is not None and job["app"] == "status-bot-v2" and job["command"] == "weather",
            "REVIEW_JOB_INVALID",
        )
        owned = self.repo.assert_owner(job_id, job["lease_token"])
        require(owned["state"] == "VALIDATING", "REVIEW_JOB_UNAVAILABLE")
        return owned

    def open(self, cookie: Any, csrf: Any, job_id: Any) -> Any:
        tag = self.authorize(cookie, csrf)
        owned = self._owned(job_id)
        manifest, _ = self.outbox.bundle(job_id, owned["attempt"])
        ticket = secrets.token_hex(16)
        expires = min(
            self.repo.clock() + 300,
            owned["lease_until"],
            owned["expires_at"],
            manifest["content"]["expires_at"],
        )
        with self.repo.owned_transaction(
            job_id, owned["lease_token"], attempt=owned["attempt"]
        ) as db:
            self.authorize(cookie, csrf, db)
            current = self.repo.assert_owner(
                job_id, owned["lease_token"], attempt=owned["attempt"], db=db
            )
            require(current["state"] == "VALIDATING", "REVIEW_JOB_UNAVAILABLE")
            require(
                not db.execute(
                    "SELECT 1 FROM whatsapp_outbox WHERE job_id=?", (job_id,)
                ).fetchone(),
                "REVIEW_JOB_UNAVAILABLE",
            )
            old = db.execute(
                "SELECT * FROM owner_reviews WHERE job_id=? AND attempt=?",
                (job_id, owned["attempt"]),
            ).fetchone()
            if old is not None:
                require(old["state"] == "OPEN", "REVIEW_TICKET_EXISTS")
                if (
                    old["expires_at"] > self.repo.clock()
                    and old["session_tag"] == tag
                    and old["manifest_sha"] == sha(manifest)
                    and old["identity_binding"] == self.outbox.identity.binding
                ):
                    return old["id"]
                # Explicit authenticated open starts a fresh review, never
                # renews an approval or preserves the viewing attestation.
                db.execute("DELETE FROM owner_reviews WHERE id=?", (old["id"],))
            require(
                db.execute("SELECT COUNT(*) FROM owner_reviews").fetchone()[0] < 10000,
                "REVIEW_TICKET_LIMIT",
            )
            db.execute(
                "INSERT INTO owner_reviews(id,session_tag,job_id,attempt,manifest_sha,video_sha,"
                "content_sha,identity_binding,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket,
                    tag,
                    job_id,
                    owned["attempt"],
                    sha(manifest),
                    manifest["render"]["video_sha256"],
                    manifest["content"]["content_hash"],
                    self.outbox.identity.binding,
                    expires,
                    "OPEN",
                ),
            )
        return ticket

    def _ticket(self, cookie: Any, ticket: Any, db: sqlite3.Connection) -> dict[str, Any]:
        tag = self.session(cookie, db)
        require(identifier(ticket, "[0-9a-f]{32}"), "REVIEW_TICKET_INVALID")
        row = db.execute(
            "SELECT * FROM owner_reviews WHERE id=? AND session_tag=?", (ticket, tag)
        ).fetchone()
        require(
            row is not None and row["state"] == "OPEN" and row["expires_at"] > self.repo.clock(),
            "REVIEW_TICKET_UNAVAILABLE",
        )
        require(row["identity_binding"] == self.outbox.identity.binding, "REVIEW_IDENTITY_CHANGED")
        return dict(row)

    def view(self, cookie: Any, ticket: Any) -> tuple[dict[str, Any], Any, Any, Any]:
        with self.repo.connect() as db:
            row = self._ticket(cookie, ticket, db)
        owned = self._owned(row["job_id"])
        require(owned["attempt"] == row["attempt"], "REVIEW_ATTEMPT_CHANGED")
        manifest, video = self.outbox.bundle(row["job_id"], row["attempt"])
        require(
            sha(manifest) == row["manifest_sha"]
            and manifest["render"]["video_sha256"] == row["video_sha"]
            and manifest["content"]["content_hash"] == row["content_sha"],
            "REVIEW_ARTIFACT_CHANGED",
        )
        return row, owned, manifest, video

    def media(self, cookie: Any, ticket: Any) -> Any:
        row, owned, _, video = self.view(cookie, ticket)
        with self.repo.owned_transaction(
            row["job_id"], owned["lease_token"], attempt=row["attempt"]
        ) as db:
            self._ticket(cookie, ticket, db)
            current = self.repo.assert_owner(
                row["job_id"], owned["lease_token"], attempt=row["attempt"], db=db
            )
            require(current["state"] == "VALIDATING", "REVIEW_JOB_UNAVAILABLE")
            db.execute("UPDATE owner_reviews SET media_served=1 WHERE id=?", (ticket,))
        return video

    def decide(
        self, cookie: Any, csrf: Any, ticket: Any, decision: Any, watched: object = False
    ) -> Any:
        self.authorize(cookie, csrf)
        require(decision in ("approve", "reject"), "REVIEW_DECISION_INVALID")
        row, owned, manifest, _ = self.view(cookie, ticket)
        if decision == "reject":
            with self.repo.owned_transaction(
                row["job_id"], owned["lease_token"], attempt=row["attempt"]
            ) as db:
                self.authorize(cookie, csrf, db)
                self._ticket(cookie, ticket, db)
                current = self.repo.assert_owner(
                    row["job_id"], owned["lease_token"], attempt=row["attempt"], db=db
                )
                require(current["state"] == "VALIDATING", "REVIEW_JOB_UNAVAILABLE")
                require(
                    not db.execute(
                        "SELECT 1 FROM whatsapp_outbox WHERE job_id=?", (row["job_id"],)
                    ).fetchone(),
                    "REVIEW_DECISION_CONFLICT",
                )
                db.execute("UPDATE owner_reviews SET state='REJECTED' WHERE id=?", (ticket,))
                self.repo.transition(row["job_id"], owned["lease_token"], "CANCELLED", db=db)
            return {"state": "REJECTED"}
        require(watched is True, "REVIEW_ATTESTATION_REQUIRED")
        now = self.repo.clock()
        review = {
            "app": "status-bot-v2",
            "job_id": row["job_id"],
            "attempt": row["attempt"],
            "content_hash": row["content_sha"],
            "video_sha256": row["video_sha"],
            "manifest_sha256": row["manifest_sha"],
            "identity_binding": self.outbox.identity.binding,
            "decision": "APPROVE_WHATSAPP_VIDEO",
            "issued_at": now,
            "expires_at": min(row["expires_at"], now + 300),
            "nonce": secrets.token_hex(16),
        }

        def commit(db: sqlite3.Connection, current: Any, item: Any, material: Any) -> None:
            self.authorize(cookie, csrf, db)
            latest = self._ticket(cookie, ticket, db)
            require(
                latest["media_served"] == 1
                and current["state"] == "VALIDATING"
                and current["attempt"] == row["attempt"],
                "REVIEW_MEDIA_REQUIRED",
            )
            db.execute(
                "UPDATE owner_reviews SET state='APPROVED',outbox_id=?,approved_review=? WHERE id=?",
                (item, json_bytes(material).decode(), ticket),
            )
            self.repo.event(
                db,
                row["job_id"],
                "owner.review_approved",
                {"ticket": ticket, "video_sha256": row["video_sha"], "human_attestation": True},
            )

        return self.outbox.prepare(
            row["job_id"],
            owned["lease_token"],
            row["attempt"],
            review,
            sign_review(review, self.outbox.identity.gate_key),
            approval_commit=commit,
        )

    def approved_material(self, outbox_id: Any) -> tuple[Any, str]:
        """Trusted local assembly only. No HTTP export or implicit dispatch."""
        receipt = self.outbox.receipt(outbox_id)
        require(receipt["state"] == "PREPARED", "REVIEW_GRANT_UNAVAILABLE")
        with self.repo.connect() as db:
            self.outbox.scope(db)
            row = db.execute(
                "SELECT r.approved_review,w.review_sha256 FROM owner_reviews r "
                "JOIN whatsapp_outbox w ON w.id=r.outbox_id WHERE r.outbox_id=? AND r.state='APPROVED'",
                (outbox_id,),
            ).fetchone()
        require(row is not None, "REVIEW_GRANT_UNAVAILABLE")
        review = parse_json(row["approved_review"].encode())
        require(sha(review) == row["review_sha256"], "REVIEW_GRANT_CHANGED")
        signature = sign_review(review, self.outbox.identity.gate_key)
        validate_review(review, signature, self.outbox.identity, self.repo.clock())
        return review, signature
