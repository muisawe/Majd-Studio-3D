"""Resume, requeue and failure classification for GenerationEngine (torch replaced by a stub)."""

import importlib
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.store import V9Store

HAVE_RUNTIME = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"))


class Crash(BaseException):
    """Stands in for the process dying mid-generation: nothing records the outcome."""


class FakeMesh:
    def __init__(self, index):
        self.index = index
        self.vertices = [None] * 3
        self.faces = [None] * (100 + index)

    def export(self, path):
        Path(path).write_text(json.dumps({"candidate": self.index}))


@unittest.skipUnless(HAVE_RUNTIME, "Generation module needs numpy and Pillow")
class GenerationResumeTests(unittest.TestCase):
    def setUp(self):
        fake_torch = types.SimpleNamespace(OutOfMemoryError=MemoryError,
                                           cuda=types.SimpleNamespace(is_available=lambda: False))
        with patch.dict(sys.modules, {"torch": fake_torch}):
            sys.modules.pop("majd_studio_3d.generation", None)
            self.generation = importlib.import_module("majd_studio_3d.generation")
        self.addCleanup(sys.modules.pop, "majd_studio_3d.generation", None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = V9Store(self.root / "app" / "db.sqlite3", self.root / "app" / "projects", self.root / "app" / "library")
        project = self.store.list_projects()[0]
        self.front = self.root / "front.png"
        self.front.write_bytes(b"front image")
        self.asset = self.store.create_asset({"project_id": project["id"], "name": "Chair", "asset_type": "Prop",
                                              "engine": "2.1"})
        self.store.update_asset(self.asset, front_path=str(self.front), candidates=4, retry_count=0, seed_strategy="Random")
        patcher = patch.multiple(self.generation, generation_paths=lambda *args, **kwargs: {"front": str(self.front)},
                                 score_candidate=lambda mesh, refs: 0.5 + mesh.index / 10,
                                 geometry_style_score=lambda *args: None, mesh_mask=lambda *args: None,
                                 process_generated_candidates=lambda items, *args, **kwargs: items)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, crash_at=None, error=None):
        engine = self.generation.GenerationEngine(self.store, None, self.root / "app", threading.Event(), threading.Lock())
        engine.run_asset_preflight = lambda row: {"status": "PASS", "score": 1}
        engine.load_image = lambda *args: object()
        engine.calls = []

        def generate(row, refs, index, attempt, resolution, progress=None):
            engine.calls.append(index)
            if index == crash_at:
                raise Crash()
            if error is not None:
                raise error
            return FakeMesh(index), 7000 + index + len(engine.calls) * 100
        engine.generate_candidate = generate
        return engine

    def interrupt_after_two(self):
        with self.assertRaises(Crash):
            self.engine(crash_at=2).process_asset(self.store.get_asset(self.asset))
        self.assertEqual(self.store.get_asset(self.asset)["status"], "processing")
        # Next start of the studio: the dead owner's claim is released.
        self.assertEqual(self.store.recover_interrupted_generations(lambda token, pid: False), [self.asset])

    def candidates(self):
        return {item["candidate"]: item for item in json.loads(self.store.get_asset(self.asset)["candidates_json"])}

    def test_interrupted_asset_resumes_from_saved_candidates(self):
        self.interrupt_after_two()
        first_seeds = {index: json.loads((self.root / "app" / "projects").glob(f"**/candidate_0{index}/generation.json")
                                         .__next__().read_text())["item"]["seed"] for index in (1, 2)}
        engine = self.engine()
        self.assertTrue(engine.process_asset(self.store.get_asset(self.asset)))
        self.assertEqual(engine.calls, [2, 3])
        row = self.store.get_asset(self.asset)
        self.assertEqual(row["status"], "review")
        self.assertIsNone(row["generation_run_id"])
        candidates = self.candidates()
        self.assertEqual(sorted(candidates), [1, 2, 3, 4])
        self.assertEqual({index: candidates[index]["seed"] for index in (1, 2)}, first_seeds)
        metrics = json.loads(row["generation_metrics_json"])
        self.assertEqual([m["reused"] for m in metrics["candidates"]], [True, True, False, False])

    def test_requeue_regenerates_everything(self):
        self.engine().process_asset(self.store.get_asset(self.asset))
        self.store.requeue_asset_generation(self.asset, "again")
        engine = self.engine()
        engine.process_asset(self.store.get_asset(self.asset))
        self.assertEqual(engine.calls, [0, 1, 2, 3])

    def test_changed_file_or_settings_are_not_reused(self):
        self.interrupt_after_two()
        glb = next((self.root / "app" / "projects").glob("**/candidate_01/*.glb"))
        glb.write_text("tampered")
        engine = self.engine()
        engine.process_asset(self.store.get_asset(self.asset))
        self.assertEqual(engine.calls, [0, 2, 3])

        self.interrupt_after_two_again()
        self.store.update_asset(self.asset, steps=45)
        engine = self.engine()
        engine.process_asset(self.store.get_asset(self.asset))
        self.assertEqual(engine.calls, [0, 1, 2, 3])

    def interrupt_after_two_again(self):
        self.store.requeue_asset_generation(self.asset, "again")
        self.interrupt_after_two()

    def test_failures_are_classified_and_runtime_errors_are_not_retried(self):
        self.store.update_asset(self.asset, retry_count=2, candidates=2)
        engine = self.engine(error=self.generation.RuntimeUnavailable("no CUDA"))
        with self.assertRaises(self.generation.RuntimeUnavailable):
            engine.process_asset(self.store.get_asset(self.asset))
        self.assertEqual(engine.calls, [0])
        row = self.store.get_asset(self.asset)
        self.assertEqual((row["status"], row["failure_kind"]), ("failed", "runtime_unavailable"))

        engine = self.engine(error=MemoryError("CUDA out of memory"))
        with self.assertRaises(self.generation.AllCandidatesFailed):
            engine.process_asset(self.store.get_asset(self.asset))
        self.assertEqual(engine.calls, [0, 0, 0, 1, 1, 1])
        self.assertEqual(self.store.get_asset(self.asset)["failure_kind"], "oom")

    def test_batch_does_not_start_without_disk_space(self):
        engine = self.engine()
        engine.disk_free = lambda: 0
        message = engine.run_batch(self.store.get_asset(self.asset)["project_id"], False)
        self.assertIn("2GB", message)
        self.assertEqual(engine.calls, [])
        self.assertEqual(self.store.get_asset(self.asset)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
