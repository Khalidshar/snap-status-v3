"""Test-only Linux seccomp harness; no production isolation bypass."""

import ctypes
import json
import sys
from pathlib import Path


class Compare(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_uint),
        ("a", ctypes.c_uint64),
        ("b", ctypes.c_uint64),
    ]


def deny_ip_sockets() -> None:
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(Compare),
    ]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # ALLOW
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        comparison = Compare(0, 1, 1, 0)  # socket arg0 != AF_UNIX
        socket_call = library.seccomp_syscall_resolve_name(b"socket")
        if (
            library.seccomp_rule_add_array(
                context, 0x50001, socket_call, 1, ctypes.byref(comparison)
            )
            != 0
        ):
            raise RuntimeError("seccomp_rule_add failed")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("seccomp_load failed")
    finally:
        library.seccomp_release(context)


if __name__ == "__main__":
    deny_ip_sockets()
    if sys.argv[1] == "proof":
        from studio_core.renderer import proof

        print(json.dumps(proof("/run/madar-quran-test-no-egress.sock")))
    else:
        from studio_core.presentation.quran_clip import render

        result = render(Path(sys.argv[1]), sys.argv[2])
        print(json.dumps({k: result[k] for k in ("network_isolation", "audio_policy", "duration")}))
