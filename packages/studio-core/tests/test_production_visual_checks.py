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


def test_news_picture_uses_exact_story_and_checks_publisher():
    item = {
        "image": "https://publisher.example/broken.jpg",
        "og_image": "",
        "title": "عنوان تجريبي",
        "source": "الناشر",
        "url": "https://publisher.example/story",
    }
    page_url = "https://publisher.example/confirmed"
    rss = """<rss><channel>
      <item><title>عنوان مختلف</title></item>
      <item><title>عنوان تجريبي</title><source url="https://other.example">مصدر آخر</source></item>
      <item><title>عنوان تجريبي - الناشر</title><source>الناشر</source>
        <link>https://publisher.example/confirmed</link></item>
    </channel></rss>""".encode()
    calls = []

    def get(url):
        calls.append(url)
        if url == page_url:
            return b'<meta property="og:image" content="https://publisher.example/confirmed.jpg">'
        assert url.startswith("https://news.google.com/rss/search?")
        return rss

    def picture(url, source_url, source):
        if url == item["image"]:
            raise OSError("missing")
        assert (url, source_url, source) == (
            "https://publisher.example/confirmed.jpg",
            page_url,
            item["source"],
        )
        return "verified-image"

    source = SimpleNamespace(get=get, picture=picture, save=lambda *_: None)
    assert Factory.news_picture(source, item) == "verified-image"
    assert len(calls) == 2
    source.get = lambda _url: b"<rss><channel /></rss>"
    with pytest.raises(ValueError, match="LEGACY_EXACT_STORY_IMAGE_UNAVAILABLE"):
        Factory.news_picture(source, item)
