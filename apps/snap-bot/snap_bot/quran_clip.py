"""Application-owned manual Quran adapter; delivery remains behind its existing gates."""

from pathlib import Path

from studio_core.religious import quran_commands
from studio_core.religious.quran_foundation import CATALOG

APP = "snap-bot-v2"


def prepare_clip(
    root: Path,
    job: str,
    surah: int,
    ayah: int,
    *,
    socket: Path,
    environment: str = "prelive",
    catalog: Path = CATALOG,
) -> Path:
    return quran_commands.prepare_clip(
        APP,
        root,
        job,
        surah,
        ayah,
        socket=socket,
        environment=environment,
        catalog=catalog,
    )


if __name__ == "__main__":
    quran_commands.main(APP)
