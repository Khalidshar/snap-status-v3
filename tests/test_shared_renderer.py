import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from snap_bot import command_cards as snap_cards
from snap_bot import render as snap_render
from studio_core.presentation import brand_frame
from studio_core.presentation import command_cards as core_cards
from studio_core.presentation import render as core_render

ROOT = Path(__file__).parents[1]


def test_compatibility_imports_share_one_implementation():
    assert snap_render is core_render
    assert snap_cards is core_cards
    assert snap_render.render is core_render.render


def test_status_imports_without_snap_installed():
    code = "import sys; sys.path[:0]=" + repr(
        [str(ROOT / "packages/studio-core"), str(ROOT / "apps/status-bot")]
    )
    code += "; import status_bot.content_clips"
    code += '; assert not any(n.startswith("snap_bot") for n in sys.modules)'
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True, capture_output=True)  # noqa: S603
    for path in (ROOT / "apps/status-bot/status_bot").glob("*.py"):
        tree = ast.parse(path.read_text())
        assert not any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("snap_bot")
            for n in ast.walk(tree)
        )


def test_card_output_pixels_equivalent_through_both_import_routes(tmp_path):
    profile = core_cards.profile()
    profile["ffmpeg_version"] = subprocess.run(
        ["/usr/bin/ffmpeg", "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    actual_run = brand_frame.run

    def deterministic_encode(args, timeout):
        if args[0] == "/usr/bin/ffmpeg":
            # The frame's gradients source otherwise picks a random seed for each
            # encode. Compare identical fixtures, retaining the same pixel tolerance.
            args = [arg + ":seed=1" if arg.startswith("gradients=") else arg for arg in args]
            args = [*args[:-1], "-threads", "1", "-filter_complex_threads", "1", args[-1]]
        return actual_run(args, timeout)

    with (
        patch.object(core_cards, "proof", return_value={"fixture_only": True}),
        patch.object(brand_frame, "run", side_effect=deterministic_encode),
    ):
        snap_cards.render([("نص عربي ثابت", 44, 700)], tmp_path / "snap", settings=profile)
        core_cards.render([("نص عربي ثابت", 44, 700)], tmp_path / "core", settings=profile)
    # Compare decoded pixels from the seeded fixture with the original golden tolerance.
    images = []
    for name in ("snap", "core"):
        images.append(
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(tmp_path / name / "preview.png"),
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ],
                check=True,
                capture_output=True,
            ).stdout
        )
    assert len(images[0]) == len(images[1]) == 1080 * 1920 * 3
    difference = sum(abs(a - b) for a, b in zip(*images, strict=True)) / len(images[0])
    assert difference <= 0.5  # less than half one channel level, out of 255
