from collections.abc import Sequence

from status_bot import __version__
from status_bot.main import create_job
from status_bot.news_preview import render as render_news
from studio_core.runtime import AppSpec, run_cli

SPEC = AppSpec(
    name="status-bot",
    version=__version__,
    feature_flag="status_enabled",
    default_content_type="status",
    create_job=create_job,
    news_renderer=render_news,
)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(SPEC, argv)
