"""Linux child-process network sandbox for offline render commands.

Usage: python -m studio_core.renderer.offline --module snap_bot news-render ...
The kernel filter is inherited across exec and by FFmpeg; no privileges are gained.
"""

import argparse
import ctypes
import ctypes.util
import os
import re
import sys


def deny_sockets() -> None:
    library = ctypes.util.find_library("seccomp")
    if sys.platform != "linux" or library is None:
        raise RuntimeError("Linux libseccomp is required for offline rendering")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    # libseccomp's default NO_NEW_PRIVS policy is retained. No root or capabilities needed.
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot create renderer network filter")
    try:
        for name in (b"socket", b"socketpair"):
            number = seccomp.seccomp_syscall_resolve_name(name)
            if number < 0 or seccomp.seccomp_rule_add(context, 0x00050001, number, 0) != 0:
                raise RuntimeError("Cannot deny renderer sockets")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Cannot activate renderer network filter")
    finally:
        seccomp.seccomp_release(context)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Python module without socket creation")
    parser.add_argument("--module", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*", args.module):
        parser.error("Invalid module name")
    deny_sockets()
    # Fixed Python executable, validated module, argv only; the network filter survives exec.
    os.execv(sys.executable, [sys.executable, "-m", args.module, *args.arguments])  # noqa: S606


if __name__ == "__main__":
    main()
