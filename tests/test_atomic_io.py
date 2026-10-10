import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d import atomic_io


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_replace_retries_while_the_target_is_busy(self):
        target = self.root / "state.json"
        real_replace = atomic_io.os.replace
        attempts = []

        def busy_twice(source, destination):
            attempts.append(destination)
            if len(attempts) < 3:
                raise PermissionError("in use by the viewer")
            real_replace(source, destination)
        with patch.object(atomic_io.os, "replace", side_effect=busy_twice), patch.object(atomic_io, "REPLACE_DELAY", 0):
            atomic_io.atomic_write_json(target, {"version": "عربي"})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"version": "عربي"})

    def test_failure_keeps_the_original_and_removes_the_temp_file(self):
        target = self.root / "manifest.json"
        target.write_text("original", encoding="utf-8")
        with patch.object(atomic_io.os, "replace", side_effect=PermissionError("locked")), \
             patch.object(atomic_io, "REPLACE_DELAY", 0), self.assertRaises(PermissionError):
            atomic_io.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual([p.name for p in self.root.iterdir()], ["manifest.json"])

    def test_atomic_copy(self):
        source = self.root / "model.glb"
        source.write_bytes(b"glb" * 1000)
        atomic_io.atomic_copy(source, self.root / "out" / "copy.glb")
        self.assertEqual((self.root / "out" / "copy.glb").read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
