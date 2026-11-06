import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio_core.production import designer
from studio_core.production.prepare import Factory

BOX = {"x": 108, "y": 210, "width": 864, "height": 1536}


def test_luma_averages_only_frames_that_were_read(monkeypatch):
    samples = iter([bytes([40, 80]), b"", bytes([100, 140])])
    argv = []

    def run(command, **kwargs):
        argv.append(command)
        assert kwargs["check"] and kwargs["capture_output"]
        return subprocess.CompletedProcess(command, 0, next(samples))

    monkeypatch.setattr(designer.subprocess, "run", run)
    assert designer.luma(Path("background.mp4")) == 90
    assert [command[command.index("-ss") + 1] for command in argv] == ["0.5", "2.0", "4.0"]


def test_luma_rejects_an_unreadable_background(monkeypatch):
    monkeypatch.setattr(
        designer.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b"")
    )
    with pytest.raises(ValueError, match="DESIGNER_FOOTAGE_UNREADABLE"):
        designer.luma(Path("background.mp4"))


def test_inspection_samples_five_frames_and_rejects_bars(monkeypatch):
    picture = bytes((180, 120, 60))
    good = picture * (1080 * 1920)
    row = picture * BOX["x"] + bytes(40 * 3) + picture * (1080 - BOX["x"] - 40)
    bad = row * 1920
    moments = []

    def frame(_video, second):
        moments.append(second)
        return good

    monkeypatch.setattr(designer, "frame", frame)
    assert designer.inspect(Path("clip.mp4"), 20, BOX) == {"moments": 5, "bars": []}
    assert moments == [2, 6, 10, 14, 18]
    monkeypatch.setattr(designer, "frame", lambda _video, _second: bad)
    with pytest.raises(ValueError, match="DESIGNER_REJECTED_BARS: black"):
        designer.inspect(Path("clip.mp4"), 20, BOX)
    monkeypatch.setattr(designer, "frame", lambda _video, _second: b"")
    with pytest.raises(ValueError, match="DESIGNER_FRAME_SIZE"):
        designer.inspect(Path("clip.mp4"), 20, BOX)


def test_news_picture_uses_only_feed_and_original_article_images():
    item = {
        "image": "https://publisher.example/broken.jpg",
        "og_image": "https://publisher.example/article.jpg",
        "source": "الناشر",
        "url": "https://publisher.example/story",
    }
    calls = []

    def picture(url, source_url, source):
        calls.append(url)
        assert source_url == item["url"]
        assert source == item["source"]
        if url == item["image"]:
            raise OSError("missing")
        return "verified-image"

    factory = SimpleNamespace(picture=picture)
    assert Factory.news_picture(factory, item) == "verified-image"
    assert calls == [item["image"], item["og_image"]]
    item["og_image"] = ""
    with pytest.raises(ValueError, match="LEGACY_EXACT_STORY_IMAGE_UNAVAILABLE"):
        Factory.news_picture(factory, item)
