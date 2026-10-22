"""Child-process targets for the N1 crash tests.

They live in a plain, importable module: the v3 test run imports test files in
importlib mode, so a spawned child cannot unpickle a function defined in one.
"""

import os
import signal

from snap_bot.n1_generations import RegisteredWriter
from snap_bot.n1_writer import PhysicalWriter


def killed_partial(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        if os.write(fd, raw) != len(raw):
            raise RuntimeError("short write")
        os.fsync(fd)
        os.kill(os.getpid(), signal.SIGKILL)  # no completed line or ACK
    finally:
        os.close(fd)


def crash_after_durable(root, key, desc, env):
    writer = RegisteredWriter(PhysicalWriter(root, key, "snap-bot-v2"))
    writer.append(desc, env)
    os.kill(os.getpid(), signal.SIGKILL)  # deliberately no receipt crosses process boundary
