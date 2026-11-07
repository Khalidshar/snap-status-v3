"""scripts/ops/madar.sh (master plan item 1): valid bash, full unit names, dry run by default."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/madar.sh"


def services() -> list[str]:
    block = SCRIPT.read_text().split("SERVICES=(", 1)[1].split("\n)", 1)[0]
    return re.findall(r'"([^"|]+)\|', block)


def test_every_unit_is_named_in_full_never_a_pattern() -> None:
    names = services()
    assert len(names) == 13
    for name in names:
        assert re.fullmatch(r"madar-v3-[a-z0-9-]+(@[a-z]+)?\.service", name), name


def test_changes_need_apply_and_secrets_are_never_read() -> None:
    text = SCRIPT.read_text()
    assert "echo yes || echo no" in text and "dry run: nothing changed" in text
    assert not re.search(r"\b(cat|less|head|tail|grep|source)\b[^\n]*\$k\b", text)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_script_is_valid_bash() -> None:
    bash = shutil.which("bash")
    assert bash
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)  # noqa: S603  # fixed argv, repo file
