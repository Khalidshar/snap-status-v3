"""Word timing and explanatory motion stay display-only and away from the Quran (8 Nov 2026)."""

from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "scripts/render_legacy_scenes.cjs").read_text(
    encoding="utf-8"
)


def test_motion_is_never_on_quran_or_sacred_text() -> None:
    assert "motion=!input.sacred&&!source.verse&&source.role!=='cta'" in TEXT
    assert "if(b&&!source.verse&&(speech||motion))" in TEXT


def test_words_keep_the_laid_out_text() -> None:
    # Wrapping re-appends every word and space it removed; counted numbers end on the original.
    assert "const text=b.textContent;b.textContent=''" in TEXT
    assert "if(q>=1)w.textContent=w.dataset.o" in TEXT


def test_years_do_not_count_up() -> None:
    assert r"year=/^\d{4}$/.test(raw)&&v>=1300&&v<=2100" in TEXT


def test_designer_decisions_reach_the_renderer() -> None:
    assert "cards:(design.type&&design.type.cards)||[]" in TEXT
    assert "plan.pages===2" in TEXT and "sp.classList.add('key')" in TEXT
