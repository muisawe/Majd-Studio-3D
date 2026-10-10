"""Single-instance guard: an OS file lock held for the life of the studio process.

The operating system releases the lock when the process exits or crashes, so a
lock that can be acquired proves no other studio is running on this data folder.
"""

from __future__ import annotations

import os
from pathlib import Path


def acquire_instance_lock(path):
    """Return an open handle that holds the lock (keep a reference), or None if another process holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
