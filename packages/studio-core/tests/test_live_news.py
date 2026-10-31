"""Live intake contracts: fresh Arabic, publisher attribution, preview-only output."""

import hashlib
import importlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from studio_core.network.client import NetworkError, NetworkResponse
from studio_core.sources.news import (
    MAX_AGE,
    NewsUnavailable,
    collect,
    digest,
    fingerprint,
    matches,
    published_label,
    validate,
)
from studio_core.sources.preview import prepare, render

NOW = datetime(2026, 10, 30, 8, 0, tzinfo=UTC).timestamp()
HEADLINES = {
    "urgent": "عاجل: اعتراض صواريخ قرب مدينة الرياض",
    "saudi": "الرياض تعلن مبادرة سعودية جديدة",
    "world": "فرنسا تعلن اتفاقا جديدا مع ألمانيا",
    "news": "إعلان اتفاق جديد لتطوير النقل العام",
    "football": "الأهلي يفوز في مباراة الدوري المصري",
    "markets": "ارتفاع أسعار النفط في الأسواق العالمية",
    "tech": "إطلاق معالج جديد لأجهزة الحاسوب",
    "style": "إطلاق عطر جديد ضمن مجموعة العطور",
}


class Feed:
    def __init__(self, title, *, at=NOW - 60, link=None, failure=False):
        self.title, self.at, self.link, self.failure = title, at, link, failure
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self.failure:
            raise NetworkError("token-that-must-not-escape")
        host = urlsplit(url).netloc
        if host == "feeds.bbci.co.uk":
            host = "www.bbc.com"
        link = self.link or "https://" + host + "/review-one"
        date = datetime.fromtimestamp(self.at, UTC).isoformat()
        raw = (
            f"<rss><channel><item><title>{escape(self.title)}</title>"
            f"<link>{escape(link)}</link><pubDate>{date}</pubDate>"
            "</item></channel></rss>"
        ).encode()
        return NetworkResponse(200, "text/xml", raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("command", HEADLINES)
def test_each_live_command_collects_only_its_own_fresh_arabic_headline(command):
    client = Feed(HEADLINES[command])
    result = collect(command, client, NOW)
    content = result["content"]
    validate(content, NOW)
    assert content["headline"] == HEADLINES[command]
    assert content["command"] == command and content["publication_enabled"] is False
    assert content["audio_review"] == "ORIGINAL_SOURCE_AUDIO_OR_SILENT"
    assert content["media_policy"] == "SOURCE_VIDEO_ELSE_SOURCE_TEXT"
    assert result["external_delivery"] is False
    assert len(client.calls) == len(set(client.calls))


@pytest.mark.parametrize("command", HEADLINES)
def test_stale_future_and_seen_news_are_never_filled_with_static_catalogs(command):
    title = HEADLINES[command]
    for at in (NOW - MAX_AGE[command] - 1, NOW + 1):
        with pytest.raises(NewsUnavailable):
            collect(command, Feed(title, at=at), NOW)
    seen = frozenset({fingerprint(title)})
    with pytest.raises(NewsUnavailable):
        collect(command, Feed(title), NOW, seen=seen)
    assert seen == frozenset({fingerprint(title)})


@pytest.mark.parametrize(
    "title", ["New fashion collection", "إطلاق معالج OpenAI جديد", "Election results", "x" * 181]
)
def test_english_long_latin_and_oversized_headlines_are_not_rendered(title):
    with pytest.raises(NewsUnavailable):
        collect("news", Feed(title), NOW)


@pytest.mark.parametrize("command", ["hadith", "fast", "verse", "brief", "halt"])
def test_unsupported_and_sacred_commands_never_fetch_editorial_feeds(command):
    client = Feed(HEADLINES["news"])
    with pytest.raises(ValueError):
        collect(command, client, NOW)
    assert client.calls == []


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/story",
        "https://user:secret@aawsat.com/story",
        "http://aawsat.com/story",
    ],
)
def test_foreign_or_credential_article_links_cannot_claim_a_trusted_publisher(link):
    with pytest.raises(NewsUnavailable):
        collect("news", Feed(HEADLINES["news"], link=link), NOW)


def test_date_label_uses_riyadh_arabic_twelve_hour_time_without_zone_label():
    assert published_label(NOW) == "الجمعة - ٣٠ - أكتوبر - ٢٠٢٦ - ١١:٠٠ ص"
    assert not matches("football", "مباراة كرة السلة في الدوري المصري")
    assert not matches("style", "صدور قرار في أسواق النفط")
    assert not matches("urgent", "هجوم الهلال يتفوق في مباراة كرة القدم")
    assert not matches("urgent", "مسيرة طلابية في وسط المدينة")


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "مصدر مختلق"),
        ("source_url", "https://evil.example/story"),
        ("published_label", "تاريخ مزيف"),
        ("publication_enabled", True),
        ("audio_review", "APPROVED"),
        ("media_policy", "GENERATED_STOCK_VIDEO"),
        ("expires_at", NOW + 86400),
        ("command", "hadith"),
        ("fingerprint", "0" * 64),
    ],
)
def test_even_rehashed_mutations_fail_offline_validation(field, value):
    content = collect("news", Feed(HEADLINES["news"]), NOW)["content"]
    content[field] = value
    content["content_hash"] = digest(content)
    with pytest.raises(ValueError):
        validate(content, NOW)


def test_source_failures_are_bounded_and_secret_free():
    client = Feed(HEADLINES["tech"], failure=True)
    with pytest.raises(NewsUnavailable) as error:
        collect("tech", client, NOW)
    assert len(client.calls) == 2
    assert "token-that" not in str(error.value)


@pytest.mark.parametrize("app", ["snap-bot", "status-bot"])
def test_prepare_freezes_app_owned_news_without_delivery_and_refuses_replay(app, tmp_path):
    from dataclasses import replace

    from studio_core.config import Config

    fixture = Path(__file__).with_name("fixtures") / "foundation.toml"
    config = replace(Config.load(fixture), app=app + "-v2")
    feed = Feed(HEADLINES["tech"])
    output = tmp_path / "collected"
    with (
        patch("studio_core.sources.preview.time.time", return_value=NOW),
        patch.object(Config, "load", return_value=config),
        patch("studio_core.network.client.NetworkClient.get", side_effect=feed.get),
    ):
        result = prepare(app, "التقنية", fixture, output)
        assert result["external_delivery"] is False
        envelope = json.loads(Path(result["input"]).read_text())
        assert envelope["app"] == app and envelope["content"]["command"] == "tech"
        assert envelope["content"]["publication_enabled"] is False
        calls = len(feed.calls)
        with pytest.raises(FileExistsError):
            prepare(app, "التقنية", fixture, output)
        assert len(feed.calls) == calls


def test_prepare_rejects_the_other_apps_network_before_any_fetch(tmp_path):
    from studio_core.config import Config

    fixture = Path(__file__).with_name("fixtures") / "foundation.toml"
    assert Config.load(fixture).app == "snap-bot-v2"
    with (
        patch(
            "studio_core.network.client.NetworkClient.get", side_effect=AssertionError("No fetch")
        ),
        pytest.raises(ValueError),
    ):
        prepare("status-bot", "tech", fixture, tmp_path / "out")


def test_cross_app_or_expired_input_is_rejected_before_render(tmp_path):
    content = collect("news", Feed(HEADLINES["news"]), NOW)["content"]
    path = tmp_path / "content.json"
    for app, when in (("status-bot", NOW), ("snap-bot", NOW + 1801)):
        path.write_text(json.dumps({"app": app, "content": content, "external_delivery": False}))
        with (
            patch("studio_core.sources.preview.time.time", return_value=when),
            pytest.raises(ValueError),
        ):
            render("snap-bot", path, tmp_path / "output", {"app": "snap-bot"}, lambda c: [])
        assert not (tmp_path / "output").exists()


def test_network_isolation_failure_stops_before_any_renderer_tool(tmp_path):
    from studio_core.renderer.text_card import render as card

    with (
        patch("studio_core.renderer.text_card.proof", side_effect=RuntimeError("not isolated")),
        patch(
            "studio_core.renderer.text_card.run", side_effect=AssertionError("Must not run FFmpeg")
        ),
        pytest.raises(RuntimeError),
    ):
        card([], tmp_path, {"egress_socket": "absent.sock"})


def test_changed_font_is_rejected_before_renderer_tools(tmp_path):
    from studio_core.renderer.text_card import render as card

    font, overlay = tmp_path / "font.ttf", tmp_path / "overlay.png"
    font.write_bytes(b"changed font")
    overlay.write_bytes(b"overlay")
    settings = {
        "egress_socket": "absent.sock",
        "font": str(font),
        "overlay": str(overlay),
        "font_sha256": "0" * 64,
        "overlay_sha256": "0" * 64,
    }
    with (
        patch("studio_core.renderer.text_card.proof", return_value={"isolated": True}),
        patch(
            "studio_core.renderer.text_card.run", side_effect=AssertionError("Must not run FFmpeg")
        ),
        pytest.raises(ValueError),
    ):
        card([], tmp_path, settings)


@pytest.mark.parametrize("package", ["snap_bot", "status_bot"])
def test_malformed_preview_input_returns_a_sanitized_failure(package, tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("[]")
    main = importlib.import_module(package + ".cli").main
    assert main(["news-render", "--input", str(path), "--output", str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("package", ["snap_bot", "status_bot"])
def test_both_cli_collection_commands_resolve_existing_arabic_names(package, capsys):
    main = importlib.import_module(package + ".cli").main
    with patch(
        "studio_core.sources.preview.prepare", return_value={"external_delivery": False}
    ) as prepare:
        assert (
            main(
                [
                    "news-collect",
                    "--command",
                    "الأناقة",
                    "--network-config",
                    "network.toml",
                    "--output",
                    "review",
                ]
            )
            == 0
        )
    assert prepare.call_args.args[:2] == (package.replace("_", "-"), "الأناقة")
    assert json.loads(capsys.readouterr().out)["external_delivery"] is False


@pytest.mark.parametrize("package", ["snap_bot", "status_bot"])
def test_real_news_review_is_arabic_branded_9_by_16_and_cannot_be_sent(package, tmp_path):
    if not Path("/usr/bin/ffmpeg").exists():
        pytest.skip("Needs the pinned FFmpeg stack")
    now = time.time()
    content = collect("news", Feed(HEADLINES["news"], at=now - 30), now)["content"]
    app = package.replace("_", "-")
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"app": app, "content": content, "external_delivery": False}))
    argv = [
        sys.executable,
        "-m",
        "studio_core.renderer.offline",
        "--module",
        package,
        "news-render",
        "--input",
        str(path),
        "--output",
        str(tmp_path / "render"),
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=150, check=False)  # noqa: S603
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert (result["width"], result["height"]) == (1080, 1920)
    assert result["audio"] == "SILENT_REVIEW_ONLY"
    assert result["eligible_for_delivery"] is result["external_delivery"] is False
    assert (tmp_path / "render/preview.png").is_file()
    repeated = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    assert repeated.returncode == 2


def test_google_news_style_names_the_publisher_and_drops_sports():
    from studio_core.sources.news import google_headline, matches

    assert google_headline("عطر جديد من ديور للشتاء - مجلة هي") == (
        "عطر جديد من ديور للشتاء",
        "مجلة هي",
    )
    assert google_headline("أزياء نانسي عجرم في حفل دبي - www.hiamag.com")[1] == "hiamag.com"
    for bad in ("بلا ناشر", "موضة رونالدو في دوري روشن - Kooora", " - مجلة هي"):
        try:
            google_headline(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert matches("style", "اتجاهات جديدة للمكياج هذا الموسم")
    assert not matches("style", "موضة رونالدو في الدوري")
    assert not matches("style", "اللحوم الحمراء موضة غذائية جديدة")
