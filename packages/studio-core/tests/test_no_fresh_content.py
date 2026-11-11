"""An empty urgent desk must never turn into fabricated or stale content."""

import pytest

from studio_core.observability.failure_code import NoFreshContent, failure_code
from studio_core.production.prepare import Factory


@pytest.mark.parametrize("app", ["snap-bot-v2", "status-bot-v2"])
@pytest.mark.parametrize("key", ["urgent", "world"])
def test_empty_desk_preserves_typed_outcome(tmp_path, monkeypatch, app, key):
    monkeypatch.setattr("studio_core.sources.news.news_sources", lambda *a, **kw: [])
    monkeypatch.setattr("studio_core.sources.panel.approved", lambda *a: [])
    factory = Factory(app, key, "a" * 32, tmp_path / "job", state=tmp_path / "state")
    monkeypatch.setattr(factory, "get", lambda url: b"<rss><channel/></rss>")
    expected = NoFreshContent if key == "urgent" else ValueError
    with pytest.raises(expected) as caught:
        factory.news()
    assert failure_code(caught.value) == (
        "NO_FRESH_CONTENT" if key == "urgent" else "VALIDATION_FAILED"
    )
    assert not factory.sources
    assert (tmp_path / "job/source-failures.json").is_file()
