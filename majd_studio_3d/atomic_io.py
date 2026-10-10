"""Crash-safe file replacement: unique temp file, fsync, then os.replace with retries.

On Windows os.replace fails with PermissionError while another process (for
example the viewer's HTTP server) has the target open, so replacement retries
briefly before giving up. The original file is left intact on failure.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

REPLACE_ATTEMPTS = 10
REPLACE_DELAY = 0.05


def replace_with_retry(source, destination, attempts: int = REPLACE_ATTEMPTS, delay: float = REPLACE_DELAY) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        replace_with_retry(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path, data, *, indent=2, ensure_ascii=False) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=ensure_ascii))


def atomic_copy(source, destination) -> None:
    """Copy a file so readers see either the old destination or the complete new one."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as output, open(source, "rb") as data:
            while chunk := data.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        replace_with_retry(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
