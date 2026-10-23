"""Read-only GitHub evidence. Missing, expired or ambiguous evidence fails closed."""

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

Json = dict[str, Any]

REPOSITORY = "Khalidshar/snap-bot-v2"


def archive_json(artifact: Json, env: Mapping[str, str]) -> Json:
    from urllib.parse import urlsplit
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

        def http_error_302(
            self, request: Any, response: Any, code: Any, message: Any, headers: Any
        ) -> Any:
            return response

    opener = build_opener(NoRedirect())
    if artifact.get("expired") is not False:
        raise ValueError("Evidence expired")
    url = (
        "https://api.github.com/repos/"
        + REPOSITORY
        + "/actions/artifacts/"
        + str(artifact["id"])
        + "/zip"
    )
    request = Request(
        url,
        headers={
            "Authorization": "Bearer " + env["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json",
        },
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 302:
            raise ValueError("Artifact redirect required")
        location = response.headers.get("Location", "")
    target = urlsplit(location)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise ValueError("Unsafe artifact redirect")
    # Signed storage URL: a separate request with no GitHub headers. Never log it.
    download = Request(location)  # noqa: S310  # fixed https URL
    with opener.open(download, timeout=30) as response:
        if response.status != 200:
            raise ValueError("Unexpected artifact download status")
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Evidence oversized")
    if artifact.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError("Evidence digest mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        result: Json = {}
        for item in archive.infolist():
            if item.filename.endswith("/"):
                continue
            if item.file_size > 100_000:
                raise ValueError("Evidence member oversized")
            if item.filename in result:
                raise ValueError("Duplicate evidence member")
            result[item.filename] = json.loads(archive.read(item))
        return result


def bound_intent(artifact: Json, intent: Json, sha: str) -> bool:
    run = str(artifact.get("workflow_run", {}).get("id", ""))
    return (
        artifact.get("expired") is False
        and artifact.get("name") == "did-you-know-send-attempt-" + sha
        and intent.get("run_id") == run
        and intent.get("video_sha") == sha
        and intent.get("state") == "EXTERNAL_EFFECT_INTENT"
        and intent.get("actor") == "Khalidshar"
    )


def receipt_allows(receipt: Json, artifact: Json, intent: Json) -> bool:
    return (
        receipt.get("state") == "NOT_SENT_PRE_NETWORK"
        and receipt.get("run_id") == intent["run_id"]
        and receipt.get("video_sha") == intent["video_sha"]
        and receipt.get("intent_artifact_id") == str(artifact["id"])
        and receipt.get("content_hash") == intent["content_hash"]
    )


def reviewed_allows(entry: Json, artifact: Json, intent: Json, run: Json) -> bool:
    run_id = intent["run_id"]
    return (
        entry.get("run_id") == run_id
        and entry.get("video_sha") == intent["video_sha"]
        and entry.get("intent_artifact_id") == str(artifact["id"])
        and entry.get("intent_sha256")
        == hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        and entry.get("reason") == "empty TG_TEST credentials; preflight stopped before network"
        and entry.get("log_url")
        == "https://github.com/"
        + REPOSITORY
        + "/actions/runs/"
        + run_id
        + "/job/111550278182#step:10:1"
        and str(run.get("id")) == run_id
        and run.get("repository", {}).get("full_name") == REPOSITORY
        and run.get("event") == "workflow_dispatch"
        and run.get("head_sha") == artifact["workflow_run"].get("head_sha")
        and run.get("path") == ".github/workflows/did-you-know-test-send.yml"
    )


def resolve_prior(
    artifact: Json,
    env: Mapping[str, str],
    github_get: Callable[[str, Mapping[str, str]], Json],
    entries: list[Json],
    reader: Callable[[Json, Mapping[str, str]], Json] = archive_json,
) -> bool:
    sha = artifact["name"].removeprefix("did-you-know-send-attempt-")
    files = reader(artifact, env)
    if set(files) != {"intent.json"}:
        raise ValueError("Intent archive invalid")
    intent = files["intent.json"]
    if not bound_intent(artifact, intent, sha):
        raise ValueError("Intent binding invalid")
    run_id = intent["run_id"]
    results: list[Json] = []
    for page in range(1, 101):
        batch = github_get(
            "actions/runs/" + run_id + "/artifacts?per_page=100&page=" + str(page), env
        )["artifacts"]
        results.extend(x for x in batch if x.get("name") == "did-you-know-send-result-" + run_id)
        if len(batch) < 100:
            break
    else:
        raise ValueError("Receipt scan incomplete")
    if len(results) > 1:
        raise ValueError("Ambiguous receipts")
    receipt = None
    if results:
        result = results[0]
        if str(result.get("workflow_run", {}).get("id")) != run_id:
            raise ValueError("Result run mismatch")
        values = reader(result, env)
        receipt = values.get("manual-preview/receipt.json")
        # UNKNOWN/PUBLISHED/invalid receipts cannot be overridden by a reconciliation.
        if receipt is not None:
            return receipt_allows(receipt, artifact, intent)
    matches = [
        entry
        for entry in entries
        if entry.get("run_id") == run_id and entry.get("video_sha") == sha
    ]
    if len(matches) != 1:
        return False
    return reviewed_allows(matches[0], artifact, intent, github_get("actions/runs/" + run_id, env))


def load_entries(root: str | os.PathLike[str]) -> list[Json]:
    data = json.loads((Path(root) / "config/snap/send-reconciliations.json").read_text())
    if data.get("schema") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("Reconciliation schema invalid")
    entries: list[Json] = data["entries"]
    return entries
