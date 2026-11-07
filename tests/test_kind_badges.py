"""Every production kind has a type badge in the renderer (owner decision 7 Nov 2026)."""

import re
from pathlib import Path

from studio_core.production.model import TITLES

ROOT = Path(__file__).resolve().parents[1]


def test_every_kind_has_a_badge() -> None:
    text = (ROOT / "scripts/render_legacy_scenes.cjs").read_text(encoding="utf-8")
    block = re.search(r"const kinds=\{(.*?)\};", text)
    assert block
    badges = dict(re.findall(r"(\w+):'([^']+)'", block.group(1)))
    assert set(TITLES) <= set(badges), sorted(set(TITLES) - set(badges))
    assert all(label.strip() for label in badges.values())
