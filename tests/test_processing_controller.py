import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.model_manager import DownloadCancelled
from majd_studio_3d.processing_controller import ProcessingController
from majd_studio_3d.processing_ui import render_processing_card
from tests import test_processing


class ProcessingControllerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established external-worker fixture; the real service,
        # controller, configuration and SQLite store run normally.
        self.fixture = test_processing.ProcessingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.service.store
        self.asset = self.fixture.asset
        self.items = [{"candidate": 1, "glb": str(self.fixture.source), "score": .9,
                       "faces": 1000, "vertices": 3, "seed": 1234, "resolution": 128}]
        self.store.update_asset(self.asset, status="review", candidates_json=json.dumps(self.items))
        self.controller = ProcessingController(self.fixture.service.app_dir, self.store, self.fixture.service)

    def test_missing_metadata_and_legacy_asset_are_safe(self):
        model = self.controller.view(self.asset, "0")
        self.assertEqual(model["state"], "pending")
        self.assertIsNone(model["original_faces"])
        self.assertTrue(model["controls"]["start"])
        self.assertFalse(model["controls"]["cancel"])
        self.assertIn("—", render_processing_card(model))
        self.store.update_asset(self.asset, candidates_json=None, best_glb=str(self.fixture.source))
        legacy = self.controller.view(self.asset)
        self.assertFalse(legacy["controls"]["start"])
        self.assertTrue(legacy["controls"]["view_raw"])
        self.assertIsNone(legacy["final_faces"])
        self.assertEqual(self.controller.view(None)["state"], "pending")
        self.store.update_asset(self.asset, candidates_json='[null]')
        self.assertFalse(self.controller.view(self.asset)["controls"]["start"])

    def test_regenerated_candidate_is_not_overwritten_by_stale_processing(self):
        def changed(*args, **kwargs):
            result = self.fixture.clean(*args, **kwargs)
            candidate = {**self.items[0], "seed": 999, "faces": 2000}
            self.fixture.source.write_text(json.dumps({"faces": 2000}))
            self.store.update_asset(self.asset, candidates_json=json.dumps([candidate]))
            return result
        self.fixture.cleaner.side_effect = changed
        with self.assertRaisesRegex(ValueError, "changed"):
            self.controller.run(self.asset)
        candidate = json.loads(self.store.get_asset(self.asset)["candidates_json"])[0]
        self.assertEqual(candidate["seed"], 999)
        self.assertEqual(candidate["faces"], 2000)
        self.assertNotIn("processing", candidate)
        self.assertEqual(len(self.store.list_processing_runs(asset_id=self.asset)), 1)

    def test_mixed_malformed_and_valid_candidates_keep_selected_identity(self):
        self.store.update_asset(self.asset, candidates_json=json.dumps([None, self.items[0]]))
        result = self.controller.run(self.asset, "0")
        candidates = json.loads(self.store.get_asset(self.asset)["candidates_json"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate"], 1)
        self.assertEqual(candidates[0]["glb"], result["processed_asset"])

    def test_replaced_raw_with_same_metadata_is_rejected_by_backend_signature(self):
        def changed(*args, **kwargs):
            result = self.fixture.clean(*args, **kwargs)
            self.fixture.source.write_text(json.dumps({"faces": 2000}))
            return result
        self.fixture.cleaner.side_effect = changed
        with self.assertRaisesRegex(ValueError, "changed"):
            self.controller.run(self.asset)
        self.assertEqual(json.loads(self.store.get_asset(self.asset)["candidates_json"]), self.items)

    def test_backend_stage_transitions_drive_live_card_and_controls(self):
        observed = []
        original = self.controller._observe
        def observe(context, event):
            original(context, event)
            observed.append(self.controller.view(self.asset))
        with patch.object(self.controller, "_observe", side_effect=observe):
            result = self.controller.run(self.asset)
        self.assertEqual(result["status"], "success")
        self.assertEqual([model["state"] for model in observed[:7]],
                         ["pending", "inspecting", "reducing", "validating_reduced", "cleaning", "validating_final", "success"])
        for model in observed[:6]:
            self.assertTrue(model["controls"]["cancel"])
            self.assertFalse(model["controls"]["start"])
            self.assertFalse(model["raw_fallback"])
        ready = self.controller.view(self.asset)
        self.assertEqual(ready["final_faces"], 300)
        self.assertFalse(ready["controls"]["start"])
        self.assertTrue(ready["controls"]["reprocess"])
        self.assertTrue(ready["controls"]["view_raw"])
        self.assertTrue(ready["controls"]["view_processed"])
        self.assertEqual(Path(ready["paths"]["raw"]).read_bytes(), self.fixture.source.read_bytes())
        self.assertNotEqual(ready["paths"]["raw"], ready["paths"]["processed"])
        asset = self.store.get_asset(self.asset)
        self.assertEqual(asset["status"], "review")
        self.assertEqual(asset["current_version"], 0)
        self.assertEqual(json.loads(asset["candidates_json"])[0]["glb"], result["processed_asset"])

    def test_reprocess_reuses_service_result_and_restores_reused_after_reload(self):
        first = self.controller.run(self.asset)
        reused = self.controller.run(self.asset, action="reprocess")
        self.assertTrue(reused["reused"])
        self.assertEqual(first["job_id"], reused["job_id"])
        self.assertEqual(self.fixture.reducer.call_count, 1)
        self.assertEqual(self.fixture.cleaner.call_count, 1)
        restored = ProcessingController(self.controller.app_dir, self.store, self.fixture.service).view(self.asset)
        self.assertEqual(restored["state"], "reused")
        self.assertTrue(restored["reused"])
        self.assertIn("Existing result reused", render_processing_card(restored))

    def test_processing_does_not_modify_an_approved_version(self):
        self.store.create_version(self.asset, str(self.fixture.source))
        before_asset = dict(self.store.get_asset(self.asset))
        before_version = dict(self.store.latest_version(self.asset))
        self.controller.run(self.asset, action="reprocess")
        after = self.store.get_asset(self.asset)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["current_version"], before_asset["current_version"])
        self.assertEqual(after["best_glb"], before_asset["best_glb"])
        self.assertEqual(dict(self.store.latest_version(self.asset)), before_version)
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)

    def test_cleanup_failure_fallback_and_retry_use_raw(self):
        def failed(*args, **kwargs):
            result = self.fixture.clean(*args, **kwargs)
            return {**result, "status": "failed", "glb_path": str(args[0]), "cleaned_glb_path": None,
                    "export_path": None, "error": "Blender timeout", "faces_after": None}
        self.fixture.cleaner.side_effect = failed
        result = self.controller.run(self.asset)
        model = self.controller.view(self.asset)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(model["raw_fallback"])
        self.assertTrue(model["controls"]["retry"])
        self.assertFalse(model["controls"]["view_processed"])
        self.assertIn("Blender timeout", render_processing_card(model))
        self.assertIn("Raw fallback", render_processing_card(model))
        self.fixture.cleaner.side_effect = self.fixture.clean
        retried = self.controller.run(self.asset, action="retry")
        self.assertEqual(retried["status"], "success")
        self.assertEqual(retried["original_faces"], 1000)
        self.assertEqual(retried["final_faces"], 300)

    def test_reducer_failure_and_validation_failure_are_stage_specific(self):
        self.fixture.reducer.side_effect = RuntimeError("Reducer missing")
        self.controller.run(self.asset)
        model = self.controller.view(self.asset)
        self.assertIn("FaceReducer", render_processing_card(model))
        self.assertIn("Reducer missing", render_processing_card(model))
        self.assertTrue(model["raw_fallback"])
        self.fixture.reducer.side_effect = self.fixture.reduce
        def inspect(*args, **kwargs):
            if args[2] == "final_metadata":
                raise ValueError("Broken normals")
            return self.fixture.inspect(*args, **kwargs)
        self.fixture.inspector.side_effect = inspect
        self.controller.run(self.asset, action="retry")
        model = self.controller.view(self.asset)
        self.assertIn("Final validation", render_processing_card(model))
        self.assertIn("Broken normals", render_processing_card(model))

    def test_cancel_is_safe_and_does_not_offer_partial_output(self):
        entered = threading.Event()
        errors = []
        def slow(*args):
            entered.set()
            if not args[5].wait(3):
                raise RuntimeError("Test cancellation did not arrive")
            raise DownloadCancelled("Reducer cancelled")
        self.fixture.reducer.side_effect = slow
        def run():
            try:
                self.controller.run(self.asset)
            except Exception as exc:  # noqa: BLE001 -- propagate any thread failure into the test assertion
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(2))
        active = self.controller.view(self.asset)
        self.assertEqual(active["state"], "reducing")
        self.assertTrue(active["controls"]["cancel"])
        self.assertTrue(self.controller.cancel(self.asset))
        self.controller.cancel(self.asset)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        stopped = self.controller.view(self.asset)
        self.assertEqual(stopped["state"], "cancelled")
        self.assertFalse(stopped["controls"]["cancel"])
        self.assertTrue(stopped["controls"]["start"])
        self.assertTrue(stopped["controls"]["view_raw"])
        self.assertFalse(stopped["controls"]["view_processed"])
        self.assertIsNone(stopped["final_faces"])

    def test_history_restores_without_embedded_candidate_metadata(self):
        result = self.controller.run(self.asset)
        self.store.update_asset(self.asset, candidates_json=json.dumps(self.items))
        self.controller._journal_path((self.asset, "1")).unlink()
        restored = ProcessingController(self.controller.app_dir, self.store, self.fixture.service).view(self.asset)
        self.assertEqual(restored["state"], "success")
        self.assertEqual(restored["run"]["job_id"], result["job_id"])
        self.assertEqual(restored["requested_face_budget"], 300)

    def test_interrupted_ui_session_restores_cancellation_not_fake_success(self):
        key = (self.asset, "1")
        context = {"key": key, "cancel": threading.Event(), "source": str(self.fixture.source)}
        self.controller._save_observation(context, {"state": "inspecting", "status": "failed", "job_id": "interrupted",
                                                   "raw_asset": str(self.fixture.source), "raw_fallback": False})
        restored = ProcessingController(self.controller.app_dir, self.store, self.fixture.service).view(self.asset)
        self.assertEqual(restored["state"], "cancelled")
        self.assertIn("interrupted", render_processing_card(restored))
        self.assertFalse(restored["controls"]["view_processed"])


if __name__ == "__main__":
    unittest.main()
