"""Owner-dispatched read-only Telegram probe. No publishing or job-state access."""

import json
import os
import re
from pathlib import Path

from studio_core.network.egress import PinnedHTTPSConnection, validate_target

from . import platform_policy

REPOSITORY = "Khalidshar/status-bot-v2"
OWNER = "khalidshar"
EXPECTED_BOT = "khalid_snap_v2_test_bot"
READ_METHODS = frozenset({"getMe", "getChat"})
API_CODES = frozenset(
    {
        "CHAT_NOT_FOUND",
        "CHAT_ID_MISSING",
        "CHAT_ID_INVALID",
        "TOKEN_NOT_AUTHORIZED",
        "BOT_BLOCKED_BY_USER",
        "TELEGRAM_API_REJECTED",
    }
)
DESCRIPTIONS = {
    "Bad Request: chat not found": "CHAT_NOT_FOUND",
    "Bad Request: chat_id is empty": "CHAT_ID_MISSING",
    "Bad Request: invalid chat_id": "CHAT_ID_INVALID",
    "Bad Request: CHAT_ID_INVALID": "CHAT_ID_INVALID",
    "Forbidden: bot was blocked by the user": "BOT_BLOCKED_BY_USER",
    "Unauthorized": "TOKEN_NOT_AUTHORIZED",
}


def status_number(value):
    return value if type(value) is int and 100 <= value <= 599 else None


class ApiRejected(Exception):
    def __init__(self, status, error_code, classification):
        super().__init__("Read-only Telegram request rejected")
        self.status = status_number(status)
        self.error_code = status_number(error_code)
        self.classification = (
            classification if classification in API_CODES else "TELEGRAM_API_REJECTED"
        )


def readonly_api(token, method, body=b""):
    platform_policy.reject_status_telegram()
    if method not in READ_METHODS:
        raise ValueError("Only read-only Telegram methods are permitted")
    if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{30,80}", token):
        raise ValueError("Invalid test credential")
    host, address, path = validate_target(
        "https://api.telegram.org/bot" + token + "/" + method, ("api.telegram.org",)
    )
    connection = PinnedHTTPSConnection(host, address, 30)
    try:
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("Telegram response limit exceeded")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid Telegram response")
        if response.status != 200 or data.get("ok") is not True:
            # Exact fixed mapping only: never retain descriptions, URLs or values.
            classification = DESCRIPTIONS.get(data.get("description"), "TELEGRAM_API_REJECTED")
            raise ApiRejected(response.status, data.get("error_code"), classification)
        if not isinstance(data.get("result"), dict):
            raise ValueError("Invalid Telegram result")
        return data["result"]
    finally:
        connection.close()


def probe(env, call=readonly_api):
    platform_policy.reject_status_telegram()
    report = {
        "schema": 1,
        "mode": "READ_ONLY_CHAT_PROBE",
        "passed": False,
        "stage": "DISPATCH_VALIDATION",
        "code": "DISPATCH_NOT_ALLOWED",
        "expected_bot": EXPECTED_BOT,
        "network_started": False,
        "bot_identity_verified": False,
        "private_chat_verified": False,
        "send_message_started": False,
        "send_video_started": False,
        "send_intent_created": False,
        "http_status": None,
        "telegram_error_code": None,
    }
    if (
        env.get("GITHUB_REPOSITORY") != REPOSITORY
        or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_ACTOR", "").lower() != OWNER
        or env.get("GITHUB_TRIGGERING_ACTOR", "").lower() != OWNER
    ):
        return report
    report["stage"] = "CREDENTIAL_VALIDATION"
    token = env.get("STATUS_TG_TEST_TOKEN", "")
    chat = env.get("STATUS_TG_TEST_CHAT", "")
    if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{30,80}", token):
        report["code"] = "INVALID_TEST_TOKEN"
        return report
    if not re.fullmatch(r"[1-9][0-9]{0,15}", chat) or int(chat) > 2**52 - 1:
        report["code"] = "INVALID_PRIVATE_CHAT_ID"
        return report
    try:
        report["stage"] = "TELEGRAM_GET_ME"
        report["network_started"] = True
        identity = call(token, "getMe")
        if identity.get("is_bot") is not True or identity.get("username") != EXPECTED_BOT:
            report["code"] = "BOT_IDENTITY_MISMATCH"
            return report
        report["bot_identity_verified"] = True
        if type(identity.get("id")) is int and identity["id"] == int(chat):
            report["code"] = "BOT_ID_USED_AS_CHAT"
            return report
        report["stage"] = "TELEGRAM_GET_CHAT"
        owner = call(token, "getChat", json.dumps({"chat_id": int(chat)}).encode())
        if owner.get("username") == EXPECTED_BOT or owner.get("is_bot") is True:
            report["code"] = "BOT_ID_USED_AS_CHAT"
            return report
        if (
            owner.get("type") != "private"
            or type(owner.get("id")) is not int
            or owner["id"] != int(chat)
        ):
            report["code"] = "PRIVATE_CHAT_MISMATCH"
            return report
        report.update(
            passed=True,
            stage="COMPLETE",
            code="PRIVATE_CHAT_CONFIRMED",
            private_chat_verified=True,
            http_status=200,
        )
    except ApiRejected as error:
        report.update(
            code=error.classification,
            http_status=error.status,
            telegram_error_code=error.error_code,
        )
    except TimeoutError:
        report["code"] = "NETWORK_TIMEOUT"
    except Exception:
        # Do not print arbitrary exceptions: they may embed a credential or URL.
        report["code"] = "NETWORK_OR_RESPONSE_ERROR"
    return report


def main():
    report = probe(os.environ)
    output = Path("outputs/telegram-chat-probe.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
