"""Persisted serial execution of pinned processing requests; no mesh algorithms."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import ClassVar

from .model_manager import DownloadCancelled
from .store import utcnow


class ItemCancellation:
    """Poll persisted cancellation from the existing subprocess watchdog."""
    def __init__(self, store, batch_id, item_id):
        self.store, self.batch_id, self.item_id = store, batch_id, item_id
        self.event = threading.Event()
        self.checked = 0.0

    def set(self):
        self.event.set()

    def is_set(self):
        if self.event.is_set():
            return True
        now = time.monotonic()
        if now - self.checked >= .15:
            item = self.store.get_processing_batch_item(self.item_id)
            batch = self.store.get_processing_batch(self.batch_id)
            if not item or not batch or item["cancel_requested"] or batch["cancel_requested"]:
                self.event.set()
            self.checked = now
        return self.event.is_set()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else .1
            if remaining <= 0:
                return False
            self.event.wait(min(.1, remaining))
        return True


class BatchExecutor:
    concurrency = 1
    _sessions: ClassVar[set[str]] = set()
    _session_guard: ClassVar[object] = threading.RLock()

    def __init__(self, app_dir, store, single_controller, resource_lock=None):
        self.app_dir = Path(app_dir)
        self.store = store
        self.single = single_controller
        self.resource_lock = resource_lock
        self.owner = uuid.uuid4().hex
        self._guard = threading.RLock()
        self._thread = None
        self._active = None
        self.recover()

    def log(self, batch_id, event, item_id=None, **details):
        path = self.app_dir / "logs" / "batches" / (batch_id + ".jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": utcnow(), "event": event, "item_id": item_id, **details}) + "\n")
        except OSError:
            pass  # Logging failures cannot discard an otherwise persisted batch.

    @classmethod
    def owner_alive(cls, token, pid):
        if not token or not pid:
            return False
        if pid == os.getpid():
            return token in cls._sessions
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # access denied is not evidence of a dead owner
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def recover(self):
        recovered = []
        with self._session_guard:
            for batch in self.store.list_processing_batches(include_hidden=True):
                if (batch["owner_token"] and not self.owner_alive(batch["owner_token"], batch["owner_pid"])
                        and self.store.recover_processing_batch(batch["id"], batch["owner_token"])):
                    recovered.append(batch["id"])
                    self.log(batch["id"], "recovered", reason="Interrupted work is retryable; no workers relaunched")
        return recovered

    def start(self, batch_id):
        with self._guard, self._session_guard:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.recover()
            if not self.store.claim_processing_batch(batch_id, self.owner, os.getpid()):
                return False
            self._sessions.add(self.owner)
            self._thread = threading.Thread(target=self._execute, args=(batch_id,), daemon=True,
                                            name="majd-processing-batch")
            try:
                self._thread.start()
            except RuntimeError:
                self._sessions.discard(self.owner)
                self.store.release_processing_batch(batch_id, self.owner)
                raise
            return True

    def run(self, batch_id):
        """Synchronous service/testing entry, using the identical execution path."""
        with self._session_guard:
            self.recover()
            if not self.store.claim_processing_batch(batch_id, self.owner, os.getpid()):
                return False
            self._sessions.add(self.owner)
        self._execute(batch_id)
        return True

    def _observe(self, batch_id, item_id, cancel, event):
        self.store.update_processing_item_stage(item_id, event["state"], event.get("job_id"))
        item = self.store.get_processing_batch_item(item_id)
        parent = self.store.get_processing_batch(batch_id)
        if item["cancel_requested"] or parent["cancel_requested"]:
            cancel.set()

    def _execute(self, batch_id):
        self.log(batch_id, "batch_started")
        try:
            while not self.single.reserve_batch(self.owner):
                if self.store.get_processing_batch(batch_id)["cancel_requested"]:
                    return
                threading.Event().wait(.1)
            snapshot = json.loads(self.store.get_processing_batch(batch_id)["config_snapshot_json"])
            while True:
                claimed = self.store.claim_next_processing_item(batch_id, self.owner)
                if claimed is None:
                    break
                item = dict(claimed)
                cancel = ItemCancellation(self.store, batch_id, item["id"])
                with self._guard:
                    self._active = (batch_id, item["id"], cancel)
                self.log(batch_id, "item_started", item["id"])
                resource_acquired = False
                try:
                    current = self.store.get_processing_batch_item(item["id"])
                    parent = self.store.get_processing_batch(batch_id)
                    if current["cancel_requested"] or parent["cancel_requested"]:
                        cancel.set()
                    if self.resource_lock is not None:
                        while not self.resource_lock.acquire(timeout=.1):
                            if cancel.is_set():
                                raise DownloadCancelled("Cancelled while waiting for the existing model operation")
                        resource_acquired = True
                    root = self.app_dir / "processing_batches" / batch_id / "work" / item["id"] / uuid.uuid4().hex
                    result = self.single.run_frozen(item, snapshot, cancel=cancel, output_root=root,
                        reservation_owner=self.owner,
                        state_callback=lambda event, item_id=item["id"], token=cancel: self._observe(batch_id, item_id, token, event))
                    state = "reused" if result["status"] == "success" and result.get("reused") else result["status"]
                    run_id = result.get("job_id")
                    stored = self.store.get_processing_run(run_id) if run_id else None
                    self.store.finish_processing_batch_item(item["id"], state, result=result,
                        error=result.get("error"), warnings=result.get("warnings", []),
                        processing_run_id=run_id if stored else None)
                except DownloadCancelled as exc:
                    self.store.finish_processing_batch_item(item["id"], "cancelled", error=str(exc))
                except Exception as exc:  # noqa: BLE001 -- one item must not stop remaining jobs
                    self.store.finish_processing_batch_item(item["id"], "failed", error=str(exc))
                finally:
                    if resource_acquired:
                        self.resource_lock.release()
                    with self._guard:
                        self._active = None
                completed = self.store.get_processing_batch_item(item["id"])
                self.log(batch_id, "item_" + completed["status"], item["id"], error=completed["error_message"])
        except Exception as exc:  # noqa: BLE001 -- database failure leaves work recoverable
            self.log(batch_id, "executor_interrupted", error=str(exc))
            self.store.recover_processing_batch(batch_id, self.owner)
        finally:
            self.single.release_batch(self.owner)
            try:
                self.store.release_processing_batch(batch_id, self.owner)
            except Exception as exc:  # noqa: BLE001 -- drop live ownership so restored storage can recover
                self.log(batch_id, "lease_release_failed", error=str(exc))
            with self._guard:
                self._active = None
            with self._session_guard:
                self._sessions.discard(self.owner)
            self.log(batch_id, "batch_stopped")

    def cancel_item(self, item_id):
        changed = self.store.cancel_processing_batch_item(item_id)
        with self._guard:
            if changed and self._active and self._active[1] == item_id:
                self._active[2].set()
        if changed:
            item = self.store.get_processing_batch_item(item_id)
            self.log(item["batch_id"], "item_cancellation_requested", item_id)
        return changed

    def cancel(self, batch_id):
        changed = self.store.cancel_processing_batch(batch_id)
        with self._guard:
            if self._active and self._active[0] == batch_id:
                item = self.store.get_processing_batch_item(self._active[1])
                if item["cancel_requested"]:
                    self._active[2].set()
        if changed:
            self.log(batch_id, "batch_cancellation_requested")
        return changed

    def wait(self, timeout=10):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True
