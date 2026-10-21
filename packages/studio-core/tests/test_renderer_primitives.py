"""Shared renderer primitives. Ported from the lab's atomic-attempt and glyph guards."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from studio_core.renderer import (
    attempt_workspace,
    cleanup_attempt_temps,
    ensure_glyphs,
    file_sha256,
    proof,
    publish_once,
    run,
    write_once,
)

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Attempt files require Linux")


@pytest.fixture
def attempt(tmp_path: Path) -> Path:
    root = tmp_path / "attempts" / "1"
    root.mkdir(parents=True)
    return root


def old(parent: Path, name: str) -> Path:
    path = parent / name
    path.write_bytes(b"orphan")
    os.utime(path, (time.time() - 7200,) * 2)
    return path


def test_missing_glyph_rejected() -> None:
    ensure_glyphs("abc", "61-63")
    ensure_glyphs("روق", "621-64a")
    with pytest.raises(ValueError, match="glyph"):
        ensure_glyphs("abz", "61-63")


def test_run_raises_on_failure_and_returns_text() -> None:
    assert run([sys.executable, "-c", "print('ok')"], 30).stdout.strip() == "ok"
    with pytest.raises(Exception, match="returned non-zero"):
        run([sys.executable, "-c", "raise SystemExit(3)"], 30)


@linux_only
def test_proof_refuses_a_renderer_that_still_has_network(tmp_path: Path) -> None:
    # A normal test process can open IP sockets, so isolation must be reported missing.
    with pytest.raises(RuntimeError, match="isolation missing"):
        proof(tmp_path / "no-egress.sock")


@linux_only
def test_fsync_precedes_link_and_link_never_replaces_inode(attempt: Path) -> None:
    path = attempt / "content.json"
    events: list[str] = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd: int) -> None:
        events.append("sync")
        real_fsync(fd)

    def link(a: object, b: object) -> None:
        assert not path.exists()
        assert "sync" in events, "guard:fsync-before-link"
        events.append("link")
        real_link(a, b)  # type: ignore[arg-type]

    module = "studio_core.renderer.primitives.os"
    with patch(module + ".fsync", side_effect=fsync), patch(module + ".link", side_effect=link):
        write_once(path, b"complete")
    assert events[-1] == "sync"
    assert list(attempt.glob(".tmp-*")) == []
    inode = path.stat().st_ino
    write_once(path, b"complete")
    with pytest.raises(ValueError, match="immutable"):
        write_once(path, b"different")
    assert (path.stat().st_ino, path.read_bytes()) == (inode, b"complete"), "guard:create-once"


@linux_only
def test_cleanup_keeps_final_fresh_other_attempt_and_symlink(attempt: Path) -> None:
    orphan = old(attempt, ".tmp-" + "a" * 32)
    final = old(attempt, "content.json")
    unknown = old(attempt, ".tmp-not-ours")
    fresh = attempt / (".tmp-" + "b" * 32)
    fresh.write_bytes(b"active-age")
    other = attempt.parent / "2"
    other.mkdir()
    outside = old(other, ".tmp-" + "c" * 32)
    linked = attempt / (".tmp-" + "d" * 32)
    linked.symlink_to(outside)
    directory = attempt / (".tmp-" + "e" * 32)
    directory.mkdir()
    fifo = attempt / (".tmp-" + "f" * 32)
    os.mkfifo(fifo)
    shortcut = attempt / "elsewhere"
    shortcut.symlink_to(other, target_is_directory=True)
    cleanup_attempt_temps(attempt)
    assert not orphan.exists(), "guard:orphan-cleanup"
    for path in (final, unknown, fresh, outside, linked, directory, fifo):
        assert path.exists() or path.is_symlink(), f"guard:cleanup-scope {path.name}"


@linux_only
def test_cleanup_preserves_old_locked_writer(attempt: Path) -> None:
    import fcntl

    orphan = old(attempt, ".tmp-" + "a" * 32)
    with orphan.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        cleanup_attempt_temps(attempt)
        assert orphan.exists(), "guard:active-temp-lock"
    cleanup_attempt_temps(attempt)
    assert not orphan.exists()


@linux_only
def test_cleanup_refuses_paths_outside_an_attempt(tmp_path: Path, attempt: Path) -> None:
    for bad in (tmp_path, attempt.parent / "0", attempt.parent / "x", tmp_path / "other" / "1"):
        with pytest.raises(ValueError, match="Invalid cleanup attempt"):
            cleanup_attempt_temps(bad)
    alias = attempt.parent / "7"
    alias.symlink_to(attempt, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe cleanup attempt"):
        cleanup_attempt_temps(alias)


@linux_only
def test_attempt_start_cleans_own_old_temporary_only(attempt: Path) -> None:
    old(attempt, ".tmp-" + "a" * 32)
    root = attempt.parent.parent
    made = attempt_workspace(root, 1)
    assert made == attempt
    assert list(attempt.glob(".tmp-*")) == [], "guard:startup-cleanup"
    assert {p.name for p in attempt.iterdir()} == {"design", "temp", "output"}


@linux_only
def test_attempt_workspace_rejects_bad_numbers_and_symlinks(tmp_path: Path) -> None:
    for bad in (0, -1, True, "1"):
        with pytest.raises(ValueError, match="Invalid render attempt"):
            attempt_workspace(tmp_path, bad)  # type: ignore[arg-type]
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "attempts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe attempt directory"):
        attempt_workspace(tmp_path, 1)


@linux_only
def test_publish_is_exclusive_and_identical_republish_is_allowed(attempt: Path) -> None:
    final = attempt / "video.mp4"
    first = attempt / "a.partial"
    first.write_bytes(b"video-one")
    publish_once(first, final)
    assert final.read_bytes() == b"video-one" and not first.exists()
    same = attempt / "b.partial"
    same.write_bytes(b"video-one")
    publish_once(same, final)
    other = attempt / "c.partial"
    other.write_bytes(b"video-two")
    with pytest.raises(ValueError, match="immutable"):
        publish_once(other, final)
    assert final.read_bytes() == b"video-one", "guard:exclusive-publication"
    assert not other.exists()


@linux_only
def test_file_sha256_refuses_empty_and_non_regular_files(attempt: Path) -> None:
    empty = attempt / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="Unsafe published artifact"):
        file_sha256(empty)
    target = attempt / "real.mp4"
    target.write_bytes(b"x")
    link = attempt / "link.mp4"
    link.symlink_to(target)
    with pytest.raises(OSError):
        file_sha256(link)
    assert file_sha256(target) == (
        "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    )
