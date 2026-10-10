"""Process-wide logging: rotating v9.log, captured stdout/stderr and crash hooks.

The studio runs under pythonw.exe, which has no console, so anything not routed
here is lost. Call configure_logging() before importing torch or gradio.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
_fault_file = None


class _SafeRotatingFileHandler(RotatingFileHandler):
    """On Windows a rollover fails while another process holds the log; keep appending instead."""

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


class StreamToLogger:
    """File-like replacement for stdout/stderr that forwards complete lines to a logger."""

    encoding = "utf-8"

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""
        self._lock = threading.Lock()
        self._local = threading.local()

    def write(self, text: str) -> int:
        if getattr(self._local, "busy", False):
            return len(text)  # A failing handler printed an error; avoid recursion.
        self._local.busy = True
        try:
            with self._lock:
                self._buffer += text.replace("\r", "\n")
                *lines, self._buffer = self._buffer.split("\n")
            for line in lines:
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        finally:
            self._local.busy = False
        return len(text)

    def flush(self) -> None:
        with self._lock:
            line, self._buffer = self._buffer, ""
        if line.strip():
            self.write(line + "\n")

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("Captured stream has no file descriptor")


def configure_logging(log_dir, *, capture_std: bool = True) -> logging.Logger:
    """Idempotently route logging, stdout/stderr and uncaught errors into log_dir."""
    global _fault_file
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(handler, "majd_handler", False) for handler in root.handlers):
        handler = _SafeRotatingFileHandler(log_dir / "v9.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                                           encoding="utf-8")
        handler.majd_handler = True
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    log = logging.getLogger("majd")
    if _fault_file is None:
        # Native crashes (CUDA, drivers) bypass Python's exception hooks.
        _fault_file = open(log_dir / "faults.log", "a", encoding="utf-8")
        faulthandler.enable(_fault_file)

    def log_uncaught(exc_type, value, traceback):
        log.critical("Uncaught exception", exc_info=(exc_type, value, traceback))

    def log_thread_exception(args):
        name = args.thread.name if args.thread else "unknown"
        log.error("Uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_exception
    if capture_std and not isinstance(sys.stdout, StreamToLogger):
        sys.stdout = StreamToLogger(logging.getLogger("stdout"), logging.INFO)
        sys.stderr = StreamToLogger(logging.getLogger("stderr"), logging.WARNING)
    return log
