"""Explicit WhatsApp egress worker. No CLI, scheduler, server or default enabling.

Deploy separately from the no-network renderer after host/security acceptance.
Only this module opens public connections; destination/path are fixed and the
existing Network Core validates DNS and pins TLS. Never follows redirects or
retries a POST. Tokens and raw Graph replies stay inside the transport boundary.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from studio_core.jobs.component_lock import ComponentLocks
from studio_core.network.egress import PinnedHTTPSConnection, validate_target

from .whatsapp_contract import (
    ApiResponse,
    DeliveryBlocked,
    identifier,
    json_bytes,
    require,
    response_id,
)
from .whatsapp_outbox import MAX_VIDEO


@dataclass(frozen=True, repr=False)
class GraphCredentials:
    version: str
    phone_number: str = field(repr=False)
    recipient: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        require(
            identifier(self.version, "v[1-9][0-9]{0,2}\\.0")
            and identifier(self.phone_number, "[1-9][0-9]{4,29}")
            and identifier(self.recipient, "[1-9][0-9]{7,14}")
            and isinstance(self.token, str)
            and 16 <= len(self.token) <= 4096
            and all(33 <= ord(c) <= 126 for c in self.token),
            "WA_CREDENTIALS_INVALID",
        )


def graph_post(
    credentials: GraphCredentials, endpoint: str, body: bytes, content_type: str, timeout: int = 30
) -> ApiResponse:
    """Single bounded POST. Version is explicitly pinned, never inferred/latest."""
    require(
        endpoint in ("media", "messages")
        and type(body) is bytes
        and 0 < len(body) <= MAX_VIDEO + 4096
        and type(timeout) is int
        and 1 <= timeout <= 60,
        "WA_TRANSPORT_REQUEST_INVALID",
    )
    url = (
        "https://graph.facebook.com/"
        + credentials.version
        + "/"
        + credentials.phone_number
        + "/"
        + endpoint
    )
    connection: PinnedHTTPSConnection | None = None
    try:
        host, address, path = validate_target(url, ("graph.facebook.com",))
        connection = PinnedHTTPSConnection(host, address, timeout)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": "Bearer " + credentials.token,
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "User-Agent": "StatusStudioV2/whatsapp-egress",
            },
        )
        response = connection.getresponse()
        raw = response.read(1048577)
        require(len(raw) <= 1048576, "WA_RESPONSE_UNKNOWN")
        return ApiResponse(response.status, raw)
    except Exception:
        # Nothing from a URL, token, body, HTTP error or DNS exception escapes.
        raise DeliveryBlocked("WA_TRANSPORT_UNKNOWN") from None
    finally:
        if connection is not None:
            try:  # noqa: SIM105  # explicit best-effort block
                connection.close()
            except Exception:  # noqa: S110  # best effort: must not mask the primary outcome
                pass


class GraphTransport:
    def __init__(self, credentials: GraphCredentials, *, enabled: object = False) -> None:
        require(enabled is True, "WA_LIVE_TRANSPORT_DISABLED")
        require(isinstance(credentials, GraphCredentials), "WA_CREDENTIALS_INVALID")
        self.credentials = credentials

    def send_text(self, text: str) -> ApiResponse:
        require(
            isinstance(text, str) and 0 < len(text) <= 4096,
            "WA_TEXT_INVALID",
        )
        return graph_post(
            self.credentials,
            "messages",
            json_bytes(
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": self.credentials.recipient,
                    "type": "text",
                    "text": {"body": text, "preview_url": False},
                }
            ),
            "application/json",
        )

    def upload_video(self, video: bytes) -> ApiResponse:
        require(type(video) is bytes and 0 < len(video) <= MAX_VIDEO, "WA_VIDEO_SIZE_INVALID")
        boundary = "statusv2" + uuid.uuid4().hex
        while boundary.encode() in video:
            boundary = "statusv2" + uuid.uuid4().hex
        body = (
            (
                "--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\n'
                "whatsapp\r\n--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="type"\r\n\r\n'
                "video/mp4\r\n--" + boundary + '\r\nContent-Disposition: form-data; name="file"; '
                'filename="status-video.mp4"\r\nContent-Type: video/mp4\r\n\r\n'
            ).encode()
            + video
            + ("\r\n--" + boundary + "--\r\n").encode()
        )
        return graph_post(
            self.credentials, "media", body, "multipart/form-data; boundary=" + boundary
        )

    def send_video(self, media_id: Any) -> ApiResponse:
        require(identifier(media_id, "[1-9][0-9]{0,29}"), "WA_MEDIA_INVALID")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.credentials.recipient,
            "type": "video",
            "video": {"id": media_id},
        }
        return graph_post(self.credentials, "messages", json_bytes(payload), "application/json")


class DeliveryWorker:
    """One reviewed outbox item, one upload and at most one messages POST.

    The default is disabled even with configured credentials/approval. It must be
    invoked by a future authenticated controller, not from renderer/CI/webhook.
    Injected transports support offline contract tests, not live delivery evidence.
    """

    def __init__(self, outbox: Any, transport: Any, *, enabled: object = False) -> None:
        require(enabled is True, "WA_DELIVERY_WORKER_DISABLED")
        require(
            transport.credentials.phone_number == outbox.identity.phone_number
            and transport.credentials.recipient == outbox.identity.recipient,
            "WA_TRANSPORT_IDENTITY_MISMATCH",
        )
        self.outbox, self.transport = outbox, transport

    def deliver(self, outbox_id: Any, token: str, review: Any, signature: Any) -> Any:
        locks = ComponentLocks(self.outbox.repo)
        lease = locks.acquire("whatsapp-delivery", "USE", 120)
        started = False
        phase = "upload"
        try:
            video = self.outbox.begin(outbox_id, token, review, signature)
            started = True
            media_id = response_id(
                self.transport.upload_video(video), "upload", self.outbox.identity.recipient
            )
            locks.assert_holder(lease)
            # Recheck owner, review expiry and service window after upload.
            # No send if they expired. Upload alone is not a recipient message.
            self.outbox.send_intent(outbox_id, token)
            phase = "send"
            return self.outbox.accept(outbox_id, self.transport.send_video(media_id))
        except Exception as error:
            if started:
                self.outbox.fail(outbox_id, phase, error)
                return self.outbox.receipt(outbox_id)
            raise
        finally:
            # A process crash leaves the lock and intent frozen for inspection.
            locks.release(lease)
