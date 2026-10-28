"""Bundled content and hostile catalog checks; no network or delivery."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from snap_bot import catalogs

BANNED = ("قال الله", "رسول الله", "آية", "حديث", "انتخابات", "حزب سياسي", "طائفية", "حرب")


@pytest.mark.parametrize("kind", catalogs.KINDS)
def test_bundled_catalog(kind: str) -> None:
    items = catalogs.load(kind)
    assert len(items) == 30
    for item in items:
        rendered = catalogs.body(item, kind)
        assert not any(word in rendered for word in BANNED)
        if kind == "riddle":
            assert rendered == item["question"] + "\n" + item["answer"]
        else:
            assert rendered == item["text"]


def write_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((catalogs.DATA / f"{kind}.ar.json").read_text())
    monkeypatch.setattr(catalogs, "DATA", tmp_path)
    return deepcopy(data)


@pytest.mark.parametrize("kind", catalogs.KINDS)
@pytest.mark.parametrize(
    "problem",
    (
        "duplicate_id",
        "duplicate_text",
        "long_text",
        "unsafe",
        "future",
        "missing",
        "sacred",
        "proofread",
        "translation",
        "wrong_kind",
        "wrong_language",
        "schema",
        "count",
        "date",
        "bad_id",
        "extra_key",
        "not_object",
        "not_items",
    ),
)
def test_reject_bad_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, problem: str
) -> None:
    data = write_catalog(tmp_path, monkeypatch, kind)
    item = data["items"][0]
    field = "question" if kind == "riddle" else "text"
    if problem == "duplicate_id":
        item["id"] = data["items"][1]["id"]
    elif problem == "duplicate_text":
        item[field] = data["items"][1][field]
    elif problem == "long_text":
        item[field] = "س" * 121
    elif problem == "unsafe":
        item[field] += "\u202e"
    elif problem == "future":
        item["verified_at"] = "9999-12-31"
    elif problem == "missing":
        del item["source"]
    elif problem == "sacred":
        data["sacred"] = True
    elif problem == "proofread":
        item[field] = "نص  عربي."
    elif problem == "translation":
        item[field] = "نص English."
    elif problem == "wrong_kind":
        data["kind"] = "other"
    elif problem == "wrong_language":
        data["language"] = "en"
    elif problem == "schema":
        data["schema"] = True
    elif problem == "count":
        data["items"].pop()
    elif problem == "date":
        item["verified_at"] = "2026-02-30"
    elif problem == "bad_id":
        item["id"] = "wrong-01"
    elif problem == "extra_key":
        item["extra"] = True
    elif problem == "not_object":
        data["items"][0] = None
    elif problem == "not_items":
        data["items"] = {}
    (tmp_path / f"{kind}.ar.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalogs.load(kind)


@pytest.mark.parametrize("kind", ("tech", "style"))
@pytest.mark.parametrize(
    "url",
    (
        "https://example.org/article",
        "http://developer.mozilla.org/article",
        "https://developer.mozilla.org.evil.test/article",
        "https://user@developer.mozilla.org/article",
        "https://developer.mozilla.org:444/article",
        "https://developer.mozilla.org/",
        "https://developer.mozilla.org:bad/article",
    ),
)
def test_reject_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, url: str
) -> None:
    data = write_catalog(tmp_path, monkeypatch, kind)
    data["items"][0]["source_url"] = url
    (tmp_path / f"{kind}.ar.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalogs.load(kind)


@pytest.mark.parametrize("value", ("س" * 61, " إجابة", "إجابة\n", "إجابة\x7f", "إجابة\u200b"))
def test_reject_riddle_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    data = write_catalog(tmp_path, monkeypatch, "riddle")
    data["items"][0]["answer"] = value
    (tmp_path / "riddle.ar.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalogs.load("riddle")


@pytest.mark.parametrize("kind", ("riddle", "funny", "night"))
def test_original_source_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    data = write_catalog(tmp_path, monkeypatch, kind)
    data["items"][0]["source"] = "external"
    (tmp_path / f"{kind}.ar.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalogs.load(kind)


@pytest.mark.parametrize("kind", ("../tech", "", "other"))
def test_unknown_kind(kind: str) -> None:
    with pytest.raises(ValueError):
        catalogs.load(kind)
    with pytest.raises(ValueError):
        catalogs.body({}, kind)


@pytest.mark.parametrize("raw", ("null", "[]", "{", "{}"))
def test_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setattr(catalogs, "DATA", tmp_path)
    (tmp_path / "tech.ar.json").write_text(raw)
    with pytest.raises(ValueError):
        catalogs.load("tech")


def test_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalogs, "DATA", tmp_path)
    with pytest.raises(ValueError):
        catalogs.load("tech")


def test_checks_receive_complete_riddle_body(monkeypatch: pytest.MonkeyPatch) -> None:
    proofread_calls: list[str] = []
    translation_calls: list[str] = []
    monkeypatch.setattr(catalogs, "proofread", proofread_calls.append)
    monkeypatch.setattr(catalogs, "translation_requirement", translation_calls.append)
    items = catalogs.load("riddle")
    expected = [catalogs.body(item, "riddle") for item in items]
    assert proofread_calls == expected
    assert translation_calls == expected


@pytest.mark.parametrize("check", ("proofread", "translation_requirement"))
def test_final_item_cannot_escape_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    data = write_catalog(tmp_path, monkeypatch, "night")
    (tmp_path / "night.ar.json").write_text(json.dumps(data))
    last = data["items"][-1]["text"]
    checked: list[str] = []

    def reject_last(text: str) -> None:
        checked.append(text)
        if text == last:
            raise ValueError("Rejected final item")

    monkeypatch.setattr(catalogs, check, reject_last)
    with pytest.raises(ValueError, match="Rejected final item"):
        catalogs.load("night")
    assert len(checked) == 30
