import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.model_manager import (
    MODEL_MANIFESTS,
    DownloadCancelled,
    ModelManager,
    ModelSpec,
)


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}


class FakeHub:
    def __init__(self):
        self.files = {"model/config.json": b'{"model":"test"}', "model/weights.bin": b"a" * (2 * 1024 * 1024 + 123)}
        self.calls = []
        self.corrupt = False
        self.ignore_range = False

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, request.get_header("Range")))
        if "/api/" in request.full_url:
            siblings = [{"rfilename": name, "size": len(data), "lfs": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
                        for name, data in self.files.items()]
            return Response(json.dumps({"sha": "a" * 40, "siblings": siblings}).encode())
        name = request.full_url.split("/resolve/" + "a" * 40 + "/")[1]
        data = self.files[name]
        if self.corrupt:
            data = b"x" * len(data)
        range_header = request.get_header("Range")
        if range_header and not self.ignore_range:
            offset = int(range_header.split("=")[1].split("-")[0])
            return Response(data[offset:], 206, {"Content-Range": f"bytes {offset}-{len(data)-1}/{len(data)}"})
        return Response(data)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = FakeHub()
        self.spec = ModelSpec("Test model", "tests/majd-download-fixture", ("model/*",), tuple(self.hub.files))
        self.manager = ModelManager(self.root / "models", self.hub, {"test": self.spec})

    def test_verified_download_reports_bytes_and_reuses_receipt(self):
        events = []
        directory = self.manager.ensure("test", lambda p, text: events.append((p, text)))
        self.assertEqual((directory / "model/weights.bin").read_bytes(), self.hub.files["model/weights.bin"])
        self.assertTrue(self.manager.status("test")["ready"])
        self.assertEqual(events[-1][0], 1)
        self.assertTrue(any("MB" in text for _, text in events))
        count = len(self.hub.calls)
        self.manager.ensure("test")
        self.assertEqual(len(self.hub.calls), count)
        (directory / "model/weights.bin").write_bytes(b"changed")
        self.assertFalse(self.manager.status("test")["ready"])

    def test_cancelled_file_resumes_from_saved_range(self):
        cancel = threading.Event()
        def progress(fraction, text):
            if 0.1 < fraction < 1 and "MB" in text:
                cancel.set()
        with self.assertRaises(DownloadCancelled):
            self.manager.ensure("test", progress, cancel)
        directory = self.manager.directory("test")
        partial = directory / "model/weights.bin.part"
        self.assertTrue(partial.exists())
        self.assertFalse(self.manager.status("test")["ready"])
        offset = partial.stat().st_size
        cancel.clear()
        self.manager.ensure("test", cancel=cancel)
        self.assertTrue(any(header == f"bytes={offset}-" for _, header in self.hub.calls))
        self.assertFalse(partial.exists())
        self.assertTrue(self.manager.status("test")["ready"])

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        cancel = threading.Event()
        with self.assertRaises(DownloadCancelled):
            self.manager.ensure("test", lambda p, text: cancel.set() if 0.1 < p < 1 and "MB" in text else None, cancel)
        self.hub.ignore_range = True
        cancel.clear()
        self.manager.ensure("test", cancel=cancel)
        self.assertTrue(self.manager.status("test")["ready"])

    def test_checksum_failure_never_publishes_a_ready_model(self):
        self.hub.corrupt = True
        with self.assertRaisesRegex(ValueError, "Checksum"):
            self.manager.ensure("test")
        self.assertFalse(self.manager.status("test")["ready"])
        self.assertFalse((self.manager.directory("test") / "model/config.json").exists())

    def test_existing_hunyuan_cache_is_used_without_copying_weights(self):
        cache = self.root / "legacy" / self.spec.repo
        for name, data in self.hub.files.items():
            target = cache / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with patch.dict(os.environ, {"HY3DGEN_MODELS": str(self.root / "legacy"), "HF_HUB_CACHE": str(self.root / "empty-hub")}):
            directory = self.manager.ensure("test")
            self.assertEqual(directory, cache)
            self.assertEqual(len(self.hub.calls), 1)
            self.assertTrue(self.manager.status("test")["ready"])

    def test_unsafe_metadata_is_rejected(self):
        self.hub.files["../outside.bin"] = b"bad"
        with self.assertRaisesRegex(ValueError, "Unsafe model path"):
            self.manager.ensure("test")
        self.assertFalse((self.root / "outside.bin").exists())

    def test_verified_legacy_weights_remain_usable_offline(self):
        cache = self.root / "legacy" / self.spec.repo
        for name, data in self.hub.files.items():
            target = cache / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        entries=[{"path":name,"size":len(data),"sha256":hashlib.sha256(data).hexdigest(),"git_sha":None} for name,data in self.hub.files.items()]
        fallback={"repo":self.spec.repo,"revision":"a"*40,"files":entries}
        manager=ModelManager(self.root/"models",lambda request,timeout: (_ for _ in ()).throw(urllib.error.URLError("offline")),{"test":self.spec})
        with patch.dict(MODEL_MANIFESTS,{"test":fallback}), patch.dict(os.environ,{"HY3DGEN_MODELS":str(self.root/"legacy"),"HF_HUB_CACHE":str(self.root/"empty-hub")}):
            self.assertEqual(manager.ensure("test"),cache)
            self.assertTrue(manager.status("test")["ready"])


if __name__ == "__main__":
    unittest.main()
