from unittest.mock import patch

import pytest

from studio_core.presentation.scene_output import plan, render, segments


def test_exact_text_and_reading_speed():
    text = "هل تعلم أن الأخطبوط يمتلك ثلاثة قلوب؟ " * 8
    assert "".join(segments(text)) == text
    value = plan({"body": text, "title": "هل تعلم"})
    assert len(value["scenes"]) > 3
    assert all(len(s["text"]) / s["seconds"] <= 17 for s in value["scenes"])


def test_at_most_one_repair_and_no_failed_evidence_removed(tmp_path):
    with (
        patch("studio_core.presentation.scene_output.proof", return_value={}),
        patch("studio_core.presentation.scene_output.run", side_effect=RuntimeError("fixture")) as runner,
    ):
        with pytest.raises(ValueError, match="ONE_REPAIR"):
            render({"body": "نص ثابت"}, tmp_path / "render")
        assert runner.call_count == 2
        assert (tmp_path / "render/attempt-1/failure.json").is_file()
        assert (tmp_path / "render/attempt-2/failure.json").is_file()
