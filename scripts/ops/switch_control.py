"""Source-host cutover coordinator. Dry run is pure planning, including no SSH.

Run manually as root only after the owner's explicit conversion approval. Failure
stops the sequence immediately; never automatically replays old source state.
"""

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from switch_host import RELEASE, STATE, stage_path

PLAN = (
    "١. فحص الأقفال والطوابير والإصدار والمفاتيح بالحجم وهوية الوجهة وخمولها.",
    "٢. إيقاف خدمات المصدر واللوحة، والتحقق من توقف المستهلكين والأنفاق.",
    "٣. نسخ الحالة أولًا إلى مساحة جديدة ومطابقة البصمات؛ الوسائط اختيارية.",
    "٤. إعادة فحص إصدار الوجهة وأحجام المفاتيح والأدوات، ثم اعتماد الحالة.",
    "٥. تشغيل الوجهة بالترتيب وفحص الخدمات ومعاينة خبر لكل بوت بلا إرسال.",
    "٦. الرجوع الصريح يوقف الوجهة ويعيد حالتها الحديثة للمصدر قبل تشغيله.",
)


class Transport:
    def __init__(self, target: str, key: Path) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,240}", target):
            raise ValueError("SWITCH_TARGET_INVALID")
        if not key.is_absolute() or not key.is_file() or any(ord(c) < 32 for c in str(key)):
            raise ValueError("SWITCH_KEY_PATH_INVALID")
        self.target = target
        self.ssh = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            str(key),
        ]
        self.helper = [
            str(RELEASE / ".venv/bin/python"),
            "-I",
            str(RELEASE / "scripts/ops/switch_host.py"),
        ]

    def call(self, side: str, operation: str, payload: dict[str, Any]) -> Any:
        command = [*self.helper, operation]
        if side == "destination":
            command = [*self.ssh, self.target, shlex.join(command)]
        elif side != "source":
            raise ValueError("SWITCH_SIDE_INVALID")
        result = subprocess.run(  # noqa: S603 — fixed commands, validated target, no shell
            command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            timeout=7500 if operation == "preview" else 300,
        )
        return json.loads(result.stdout)

    def copy(self, source: str, identifier: str, names: list[str]) -> None:
        stage = stage_path(STATE, identifier)
        local = str(STATE) + "/"
        remote = self.target + ":" + str(STATE) + "/"
        target = str(stage) + "/"
        origins = (local, self.target + ":" + target) if source == "source" else (remote, target)
        # No --delete, no symlink following and no in-place writes. First pass is
        # databases/state; media, when requested, is a separate subsequent pass.
        command = [
            "rsync",
            "-aH",
            "--numeric-ids",
            "--protect-args",
            "--from0",
            "--files-from=-",
            "-e",
            shlex.join(self.ssh),
            "--",
            *origins,
        ]
        subprocess.run(  # noqa: S603 — manifest paths are validated before reaching argv/stdin
            command,
            input="\0".join(names).encode() + b"\0",
            capture_output=True,
            check=True,
            timeout=7200,
        )


def save(path: Path, record: dict[str, Any]) -> None:
    # An exclusive step file preserves every previous checkpoint, including failures.
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


class Switch:
    def __init__(self, transport: Any, audit: Path, media: bool = False) -> None:
        self.transport, self.audit, self.media = transport, audit, media
        self.identifier = uuid.uuid4().hex
        self.sequence = 0
        self.record: dict[str, Any] = {"id": self.identifier, "phase": "created"}

    def checkpoint(self, phase: str) -> None:
        self.record["phase"] = phase
        save(self.audit / f"{self.identifier}-{self.sequence:02d}.json", self.record)
        self.sequence += 1
        print(phase, flush=True)  # Never log transport args, credentials or state data.

    def call(self, side: str, operation: str, **payload: Any) -> Any:
        return self.transport.call(side, operation, payload)

    def transfer(self, source: str, destination: str) -> None:
        from switch_host import MEDIA

        data = self.call(source, "manifest", media=self.media)
        if not isinstance(data, dict) or not data:
            raise ValueError("SWITCH_EMPTY_MANIFEST")
        for name in data:
            if (
                not isinstance(name, str)
                or Path(name).is_absolute()
                or ".." in Path(name).parts
                or any(ord(c) < 32 for c in name)
            ):
                raise ValueError("SWITCH_MANIFEST_UNSAFE")
        self.call(destination, "stage", id=self.identifier)
        state = [n for n in data if n != "." and Path(n).suffix.lower() not in MEDIA]
        media = [n for n in data if Path(n).suffix.lower() in MEDIA]
        self.transport.copy(source, self.identifier, state)
        if media:
            self.transport.copy(source, self.identifier, media)
        if self.call(source, "manifest", media=self.media) != data:
            raise ValueError("SWITCH_SOURCE_CHANGED_DURING_COPY")
        # Verify installed release/tools and sizes again after copying, before promotion.
        info = self.call(destination, "inspect")
        if any(info[k] != self.record[k] for k in ("commit", "key_sizes")):
            raise ValueError("SWITCH_DESTINATION_CHANGED")
        self.call(destination, "promote", id=self.identifier, manifest=data)
        self.checkpoint("state_verified_and_promoted")

    def forward(self) -> None:
        source = self.call("source", "inspect")
        destination = self.call("destination", "inspect")
        if source["identity"] == destination["identity"]:
            raise ValueError("SWITCH_SAME_HOST")
        if (
            source["commit"] != destination["commit"]
            or source["key_sizes"] != destination["key_sizes"]
        ):
            raise ValueError("SWITCH_PREFLIGHT_MISMATCH")
        if any(
            s in {"active", "activating", "reloading", "deactivating"}
            for s in destination["states"].values()
        ):
            raise ValueError("SWITCH_DESTINATION_NOT_STOPPED")
        if any(
            s in {"activating", "reloading", "deactivating", "failed"}
            for s in source["states"].values()
        ):
            raise ValueError("SWITCH_SOURCE_NOT_STABLE")
        if any(s == "active" and n not in source["units"] for n, s in source["states"].items()):
            raise ValueError("SWITCH_EXTRA_CONSUMER_OR_TIMER")
        self.record.update({k: source[k] for k in ("commit", "key_sizes", "units")})
        self.record["source_autostart"] = source.get("autostart", {"enabled": "not-found"})
        self.record["source_identity"] = source["identity"]
        self.record["destination_identity"] = destination["identity"]
        self.record["source_active"] = [
            n for n in source["units"] if source["states"].get(n) == "active"
        ]
        self.checkpoint("preflight_verified")
        self.call("source", "stop")
        self.checkpoint("source_stopped")
        self.transfer("source", "destination")
        if self.record["source_autostart"]["enabled"] != "not-found":
            # Fence reboots before the first destination consumer, not only after
            # potentially long previews. Keep the original state for rollback.
            self.checkpoint("source_autostart_disable_intent")
            self.record["source_autostart_after"] = self.call("source", "autostart-disable")
            self.checkpoint("source_autostart_disabled")
        self.checkpoint("destination_start_intent")
        self.call("destination", "start", units=self.record["units"])
        self.call("destination", "preview", id=self.identifier)
        if self.record["source_autostart"]["enabled"] != "not-found":
            self.record["source_autostart_verified"] = self.call("source", "autostart-check")
        self.checkpoint("completed_previews_without_sending")

    def rollback(self, previous: dict[str, Any]) -> None:
        if not re.fullmatch("[0-9a-f]{32}", str(previous.get("id", ""))):
            raise ValueError("SWITCH_JOURNAL_INVALID")
        self.record.update(
            {k: previous[k] for k in ("commit", "key_sizes", "units", "source_active")}
        )
        if previous.get("rollback_of"):
            raise ValueError("SWITCH_ROLLBACK_ALREADY_STARTED")
        self.record["source_autostart"] = previous.get("source_autostart", {"enabled": "not-found"})
        self.record["rollback_of"] = previous["id"]
        for side in ("source", "destination"):
            info = self.call(side, "inspect")
            if info["identity"] != previous.get(side + "_identity"):
                raise ValueError("SWITCH_ROLLBACK_HOST_MISMATCH")
            if any(info[k] != previous[k] for k in ("commit", "key_sizes")):
                raise ValueError("SWITCH_ROLLBACK_RELEASE_MISMATCH")
            self.record[side + "_identity"] = info["identity"]
        # First establish the destination is stopped. If unreachable or busy, source
        # stays stopped: the operator must resolve it locally, never risk two consumers.
        self.call("destination", "stop")
        self.call("source", "stop")
        self.checkpoint("rollback_both_stopped")
        if previous["phase"] in {"destination_start_intent", "completed_previews_without_sending"}:
            # The destination may have consumed updates. Never replay the old offsets
            # or delivery ledger: copy its latest frozen state back, retaining backup.
            self.media = True
            self.transfer("destination", "source")
        self.call("source", "start", units=self.record["source_active"])
        if self.record["source_autostart"]["enabled"] != "not-found":
            self.record["source_autostart_restored"] = self.call(
                "source", "autostart-restore", enabled=self.record["source_autostart"]["enabled"]
            )
        self.checkpoint("rollback_completed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-media", action="store_true")
    parser.add_argument("--rollback", type=Path, metavar="CHECKPOINT")
    args = parser.parse_args()
    if not args.apply:
        print("تجربة جافة: لا اتصال ولا قراءة حالة أو مفاتيح ولا تغيير خدمات.")
        print("\n".join(PLAN))
        return 0
    if os.geteuid() != 0:
        parser.error("التنفيذ يحتاج root على المصدر، وبعد موافقة خالد على التحويل.")
    try:
        target = os.environ["SWITCH_TARGET"]
        key = Path(os.environ["SWITCH_KEY"])
        audit = Path(os.environ["SWITCH_AUDIT"])
        from switch_host import path_safe

        path_safe(audit)
        if audit == STATE or STATE in audit.parents:
            raise ValueError("SWITCH_AUDIT_MUST_BE_OUTSIDE_STATE")
        audit.mkdir(mode=0o700, parents=True, exist_ok=True)
        task = Switch(Transport(target, key), audit, args.include_media)
        with Path("/run/lock/madar-switch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.rollback:
                previous = json.loads(args.rollback.read_text())
                task.rollback(previous)
            else:
                task.forward()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        print(
            "توقّف التحويل عند أول فشل. احتفظ بالسجلات وافحص آخر نقطة تحقق؛ "
            "لا تُشغّل المصدر قبل إثبات توقف الوجهة.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
