"""The WhatsApp marker is independent, idempotent, audited and never replays."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from status_bot import command_control


def test_halt_start_preserve_proof_and_other_bot(tmp_path):
    runtime = SimpleNamespace(locked=lambda: nullcontext(), append=Mock())
    snap = tmp_path / "snap/HALT"
    snap.parent.mkdir()
    snap.touch()
    marker = tmp_path / "status/HALT"
    with patch.object(command_control, "HALT", marker):
        assert "موقوف" in command_control.change(True, runtime, "a" * 64)
        marker.write_text("retained-control-proof")
        command_control.change(True, runtime, "a" * 64)
        assert command_control.halted()
        assert "تم تشغيل" in command_control.change(False, runtime, "b" * 64)
        command_control.change(False, runtime, "b" * 64)
        assert not command_control.halted()
        assert "يعمل" in command_control.status_text()
    assert snap.is_file()
    archives = list((marker.parent / "control-history").iterdir())
    assert len(archives) == 1
    assert archives[0].read_text() == "retained-control-proof"
    assert runtime.append.call_count == 8
    changes = [
        c.kwargs["changed"]
        for c in runtime.append.call_args_list
        if c.args[0] == "CONTROL"
    ]
    assert changes == [True, False, True, False]


def test_symlink_marker_is_refused(tmp_path):
    target = tmp_path / "evidence"
    target.touch()
    marker = tmp_path / "HALT"
    marker.symlink_to(target)
    with (
        patch.object(command_control, "HALT", marker),
        pytest.raises(ValueError),
    ):
        command_control.change(False, Mock(), "a" * 64)
    assert target.is_file()


def test_failed_audit_does_not_enable_sending(tmp_path):
    marker = tmp_path / "HALT"
    marker.touch()
    runtime = SimpleNamespace(
        locked=lambda: nullcontext(),
        append=Mock(side_effect=OSError("fixture")),
    )
    with (
        patch.object(command_control, "HALT", marker),
        pytest.raises(OSError),
    ):
        command_control.change(False, runtime, "a" * 64)
    assert marker.is_file()


def test_verified_command_route_blocks_content_without_starting_services(tmp_path):
    import test_status_whatsapp_outbox as fixtures

    from status_bot.owner_commands import answer, process_one

    case = fixtures.WhatsAppContracts()
    case.setUp()
    try:
        runtime = SimpleNamespace(
            locked=lambda: nullcontext(),
            append=Mock(),
            events=lambda: [],
        )
        transport = SimpleNamespace(
            send_text=Mock(
                return_value=fixtures.OfflineTransport(case.identity).send_video.return_value
            )
        )
        marker = tmp_path / "HALT"
        rows = [
            {"tag": "a" * 64, "text": "أوقف"},
            {"tag": "b" * 64, "text": "حديث"},
            {"tag": "c" * 64, "text": "شغّل"},
            None,
        ]
        with (
            patch.object(command_control, "HALT", marker),
            patch("status_bot.owner_commands.claim", side_effect=rows),
        ):
            assert process_one(case.outbox, transport, Mock(), runtime)
            assert command_control.halted()
            assert "موقوف" in answer("الحالة", runtime)
            assert process_one(case.outbox, transport, Mock(), runtime)
            assert "موقوف" in transport.send_text.call_args.args[0]
            assert process_one(case.outbox, transport, Mock(), runtime)
            assert not command_control.halted()
            assert not process_one(case.outbox, transport, Mock(), runtime)
    finally:
        case.doCleanups()
