"""Article links of the BBC Arabic section feeds live on bbc.com (repair 11 Nov 2026)."""

import pytest

from studio_core.sources import for_command
from studio_core.sources.news import _link


@pytest.mark.parametrize(
    ("command", "source_id"),
    [("football", "bbc-ar-sport"), ("markets", "bbc-ar-business"), ("world", "bbc-ar-world")],
)
def test_bbc_section_feed_links_are_accepted(command, source_id):
    source = next(s for s in for_command(command) if s.id == source_id)
    assert _link("https://www.bbc.com/arabic/articles/c0abc123", source).startswith("https://")
    with pytest.raises(ValueError):
        _link("https://example.com/arabic/articles/c0abc123", source)


def test_new_publishers_are_bound_and_unusable_ones_stay_candidates():
    by_id = {s.id: s for key in ("saudi", "world", "markets", "football") for s in for_command(key)}
    for approved in (
        "okaz",
        "asharq",
        "asharq-business",
        "cnn-ar",
        "un-ar",
        "euronews-ar",
        "arriyadiyah",
    ):
        assert by_id[approved].status == "approved"
    for waiting in ("dw-ar", "independent-ar"):
        assert by_id[waiting].status == "candidate"
