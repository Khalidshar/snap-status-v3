"""Sa'di beside Muyassar (owner decision 6 Nov 2026): whole text, in scope, only if it fits."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio_core.production.model import SAADI, saadi_text
from studio_core.production.prepare import Factory

TEXT = "{ فَاذْكُرُونِي أَذْكُرْكُمْ } فأمر تعالى بذكره، ووعد عليه أفضل جزاء."


def raw(text: str = TEXT, verses: tuple[str, ...] = ("2:152",), resource: int = 91) -> bytes:
    body = {"resource_id": resource, "text": text, "verses": {k: {} for k in verses}}
    return json.dumps({"tafsir": body}, ensure_ascii=False).encode()


def test_saadi_text_keeps_every_word_and_drops_markup_only() -> None:
    assert saadi_text(raw("<p>" + TEXT + "</p>"), 2, [152]) == TEXT


@pytest.mark.parametrize(
    "value",
    [raw(resource=16), raw(verses=("2:151", "2:152")), raw(verses=())],
)
def test_saadi_text_refuses_other_tafsir_or_verses_outside_the_clip(value: bytes) -> None:
    with pytest.raises(ValueError, match="LEGACY_SAADI_SCOPE"):
        saadi_text(value, 2, [152])


def factory(tmp_path: Path, app: str, text: str) -> Factory:
    client = SimpleNamespace(get=lambda url: SimpleNamespace(status=200, body=raw(text)))
    return Factory(app, "verse", "a" * 32, tmp_path / "root", client=client, state=tmp_path / "s")


def test_saadi_scene_is_whole_named_and_sourced(tmp_path: Path) -> None:
    f = factory(tmp_path, "snap-bot-v2", TEXT)
    [s] = f.saadi(2, [152], [{"seconds": 20.0}])
    assert s["body"] == TEXT and s["head"] == "تفسير السعدي" and s["role"] == "saadi"
    assert s["source_url"] == SAADI.format(2, 152)
    assert s["seconds"] >= (len(s["body"]) + len(s["head"])) / 17
    assert any(a["source_url"] == s["source_url"] for a in f.assets)


def test_saadi_is_left_out_whole_when_the_clip_has_no_room(tmp_path: Path) -> None:
    long = TEXT + " " + "والشكر يكون بالقلب واللسان والجوارح. " * 30
    assert factory(tmp_path, "status-bot-v2", long).saadi(2, [152], [{"seconds": 20.0}]) == []


def test_snap_verse_clips_get_120_seconds_and_other_kinds_keep_90() -> None:
    from studio_core.production.model import budget

    assert budget("snap-bot-v2", "verse")[1] == 120.0
    assert budget("snap-bot-v2", "facts")[1] == 90.0
    assert budget("status-bot-v2", "verse")[1] == 58.0


def test_whole_passage_room_follows_the_platform_budget(tmp_path: Path) -> None:
    snap = factory(tmp_path / "a", "snap-bot-v2", TEXT)
    status = factory(tmp_path / "b", "status-bot-v2", TEXT)
    assert round(snap.passage_room(), 1) == 111.5
    assert round(status.passage_room(), 1) == 49.5
