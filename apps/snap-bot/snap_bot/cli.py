from collections.abc import Sequence

from snap_bot import __version__
from snap_bot.main import create_job
from studio_core.runtime import AppSpec, run_cli

SPEC = AppSpec(
    name="snap-bot",
    version=__version__,
    feature_flag="snap_enabled",
    default_content_type="news",
    create_job=create_job,
)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(SPEC, argv)
