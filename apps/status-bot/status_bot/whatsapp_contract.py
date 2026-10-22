"""Secret-free failures and bounded, signed WhatsApp delivery contracts.

Signing is a primitive for a future authenticated human-review controller.
Nothing in the render pipeline issues approvals or enables delivery.
"""

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field


class DeliveryBlocked(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DeliveryBlocked(code)


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def parse_json(raw, limit=1048576):
    require(type(raw) is bytes and 0 < len(raw) <= limit, "WA_INVALID_JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "WA_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        return json.loads(
            raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False, "WA_INVALID_JSON")
        )
    except (ValueError, UnicodeError, RecursionError):
        raise DeliveryBlocked("WA_INVALID_JSON") from None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identifier(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True, repr=False)
class DeliveryIdentity:
    # IDs, recipient and secrets deliberately excluded from repr and receipts.
    business_account: str = field(repr=False)
    phone_number: str = field(repr=False)
    recipient: str = field(repr=False)
    gate_key: bytes = field(repr=False)
    webhook_key: bytes = field(repr=False)

    def __post_init__(self):
        require(
            all(
                identifier(v, "[1-9][0-9]{4,29}")
                for v in (self.business_account, self.phone_number)
            ),
            "WA_IDENTITY_INVALID",
        )
        require(identifier(self.recipient, "[1-9][0-9]{7,14}"), "WA_RECIPIENT_INVALID")
        require(
            type(self.gate_key) is bytes
            and len(self.gate_key) >= 32
            and type(self.webhook_key) is bytes
            and len(self.webhook_key) >= 32
            and self.gate_key != self.webhook_key,
            "WA_KEYS_INVALID",
        )

    def tag(self, purpose, value):
        return hmac.new(
            self.gate_key, ("status-bot-v2/" + purpose + "/" + value).encode(), hashlib.sha256
        ).hexdigest()

    @property
    def binding(self):
        return self.tag(
            "identity", self.business_account + "/" + self.phone_number + "/" + self.recipient
        )


def sign_review(review, key):
    """Call only from an authenticated human-review controller, never a renderer."""
    require(type(key) is bytes and len(key) >= 32, "WA_KEYS_INVALID")
    return hmac.new(
        key, b"status-bot-v2/review/v1/" + json_bytes(review), hashlib.sha256
    ).hexdigest()


def validate_review(review, signature, identity, now):
    require(
        isinstance(review, dict)
        and set(review)
        == {
            "app",
            "job_id",
            "attempt",
            "content_hash",
            "video_sha256",
            "manifest_sha256",
            "identity_binding",
            "decision",
            "issued_at",
            "expires_at",
            "nonce",
        },
        "WA_REVIEW_INVALID",
    )
    require(
        review["app"] == "status-bot-v2"
        and review["decision"] == "APPROVE_WHATSAPP_VIDEO"
        and identifier(review["job_id"], "[0-9a-f]{32}")
        and type(review["attempt"]) is int
        and review["attempt"] > 0
        and identifier(review["nonce"], "[0-9a-f]{32}")
        and all(
            identifier(review[k], "[0-9a-f]{64}")
            for k in ("content_hash", "video_sha256", "manifest_sha256", "identity_binding")
        ),
        "WA_REVIEW_INVALID",
    )
    require(
        finite(now)
        and finite(review["issued_at"])
        and finite(review["expires_at"])
        and review["issued_at"] <= now < review["expires_at"]
        and 0 < review["expires_at"] - review["issued_at"] <= 600,
        "WA_REVIEW_EXPIRED",
    )
    require(review["identity_binding"] == identity.binding, "WA_REVIEW_IDENTITY_MISMATCH")
    require(
        identifier(signature, "[0-9a-f]{64}")
        and hmac.compare_digest(signature, sign_review(review, identity.gate_key)),
        "WA_REVIEW_UNAUTHENTICATED",
    )


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes = field(repr=False)


def response_id(response, phase, recipient):
    require(
        isinstance(response, ApiResponse)
        and type(response.status) is int
        and 100 <= response.status <= 599,
        "WA_RESPONSE_UNKNOWN",
    )
    value = parse_json(response.body)
    require(isinstance(value, dict), "WA_RESPONSE_UNKNOWN")
    # Only an explicit bounded Graph 4xx error is known rejection; 5xx and
    # redirects remain uncertain, and raw error text is never emitted/stored.
    if 400 <= response.status < 500 and isinstance(value.get("error"), dict):
        code = value["error"].get("code")
        if type(code) is int and 0 <= code <= 9999999:
            raise DeliveryBlocked("WA_API_REJECTED_" + str(code))
    require(200 <= response.status < 300 and "error" not in value, "WA_RESPONSE_UNKNOWN")
    if phase == "upload":
        result = value.get("id")
        require(identifier(result, "[1-9][0-9]{0,29}"), "WA_RESPONSE_UNKNOWN")
    else:
        messages = value.get("messages")
        contacts = value.get("contacts")
        require(
            value.get("messaging_product") == "whatsapp"
            and isinstance(messages, list)
            and len(messages) == 1
            and isinstance(messages[0], dict)
            and isinstance(contacts, list)
            and len(contacts) == 1
            and isinstance(contacts[0], dict)
            and contacts[0].get("wa_id") == recipient,
            "WA_RESPONSE_UNKNOWN",
        )
        result = messages[0].get("id")
        require(identifier(result, r"wamid\.[A-Za-z0-9+/=_-]{1,250}"), "WA_RESPONSE_UNKNOWN")
    return result
