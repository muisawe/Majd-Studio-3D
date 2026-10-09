import json
import os
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.batch_controller import BatchController
from majd_studio_3d.cleanup_config import save_cleanup_config
from majd_studio_3d.model_manager import DownloadCancelled
from majd_studio_3d.parts import run_process
from majd_studio_3d.processing_controller import ProcessingController
from tests import test_processing


class BatchProcessingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_processing.ProcessingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.service.store
        self.project = self.fixture.project
        self.assets = []
        self.sources = {}
        for name in ("Reusable", "New success", "Retry candidate"):
            asset = self.store.create_asset({"project_id": self.project, "name": name, "asset_type": "Prop", "engine": "2.1"})
            source = self.fixture.root / (name + ".glb")
            source.write_text(json.dumps({"faces": 1000}))
            item = {"candidate": 1, "glb": str(source), "score": .9, "faces": 1000, "vertices": 3}
            self.store.update_asset(asset, status="review", candidates_json=json.dumps([item]))
            self.assets.append(asset)
            self.sources[asset] = source
        self.single = ProcessingController(self.fixture.service.app_dir, self.store, self.fixture.service)
        self.batch = BatchController(self.single.app_dir, self.store, self.single)

    def items(self, batch_id):
        return [dict(row) for row in self.store.list_processing_batch_items(batch_id)]

    def test_order_accounting_reuse_failure_and_successful_retry(self):
        self.single.run(self.assets[0])
        reduced_before = self.fixture.reducer.call_count
        order = []
        def cleaning(*args, **kwargs):
            result = self.fixture.clean(*args, **kwargs)
            parent = self.store.connect()
            try:
                active = parent.execute("SELECT asset_id FROM processing_batch_items WHERE status='processing'").fetchone()[0]
            finally:
                parent.close()
            order.append(active)
            if active == self.assets[2]:
                result.update(status="failed", error="Mock cleanup failed", glb_path=str(args[0]),
                              cleaned_glb_path=None, export_path=None, faces_after=None)
            return result
        self.fixture.cleaner.side_effect = cleaning
        batch_id = self.batch.create(self.project, self.assets)
        self.assertTrue(self.batch.executor.run(batch_id))
        view = self.batch.view(batch_id)
        self.assertEqual([item["status"] for item in view["items"]], ["reused", "success", "failed"])
        self.assertEqual(order, self.assets[1:])
        self.assertEqual(self.fixture.reducer.call_count - reduced_before, 2)
        self.assertEqual(view["batch"]["status"], "partially_failed")
        self.assertEqual((view["counts"]["completed"], view["counts"]["reused"], view["counts"]["failed"]), (2, 1, 1))
        self.fixture.cleaner.side_effect = self.fixture.clean
        failed = self.items(batch_id)[2]
        self.assertEqual(self.batch.retry_item(failed["id"]), 1)
        self.assertTrue(self.batch.executor.run(batch_id))
        view = self.batch.view(batch_id)
        self.assertEqual(view["batch"]["status"], "success")
        self.assertEqual(view["counts"]["completed"], 3)
        retry = view["items"][2]
        self.assertEqual(retry["retry_count"], 1)
        self.assertEqual(json.loads(retry["result_json"])["original_faces"], 1000)
        self.assertTrue(Path(retry["raw_source"]).is_file())

    def test_pinned_configuration_and_raw_survive_global_and_source_changes(self):
        batch_id = self.batch.create(self.project, [self.assets[0], self.assets[0]])
        self.assertEqual(len(self.items(batch_id)), 1)
        snapshot = json.loads(self.store.get_processing_batch(batch_id)["config_snapshot_json"])
        save_cleanup_config({"decimate_ratio": .8}, self.single.app_dir)
        self.sources[self.assets[0]].write_text(json.dumps({"faces": 2000}))
        self.batch.executor.run(batch_id)
        result = json.loads(self.items(batch_id)[0]["result_json"])
        self.assertEqual((result["original_faces"], result["requested_face_budget"], result["final_faces"]), (1000, 300, 300))
        self.assertFalse(result["review_applied"])
        self.assertEqual(json.loads(self.store.get_processing_batch(batch_id)["config_snapshot_json"]), snapshot)

    def test_cancel_queued_item_continues_other_items(self):
        batch_id = self.batch.create(self.project, self.assets)
        queued = self.items(batch_id)[1]
        self.assertTrue(self.batch.cancel_item(queued["id"]))
        self.assertFalse(self.batch.cancel_item(queued["id"]))
        self.batch.executor.run(batch_id)
        self.assertEqual([item["status"] for item in self.items(batch_id)], ["success", "cancelled", "success"])
        self.assertEqual(self.batch.view(batch_id)["counts"]["completed"], 2)

    def test_active_item_cancel_targets_it_and_batch_continues(self):
        batch_id = self.batch.create(self.project, self.assets)
        first = self.items(batch_id)[0]
        entered = threading.Event()
        def reducing(*args):
            if not entered.is_set():
                entered.set()
                if not args[5].wait(5):
                    raise RuntimeError("Cancellation timed out in test")
                raise DownloadCancelled("Cancelled one item")
            return self.fixture.reduce(*args)
        self.fixture.reducer.side_effect = reducing
        self.assertTrue(self.batch.start(batch_id))
        self.assertTrue(entered.wait(3))
        self.batch.cancel_item(first["id"])
        self.assertTrue(self.batch.executor.wait(5))
        self.assertEqual([item["status"] for item in self.items(batch_id)], ["cancelled", "success", "success"])
        self.assertTrue(Path(first["raw_source"]).is_file())

    def test_whole_batch_cancel_preserves_completed_and_pinned_raw(self):
        batch_id = self.batch.create(self.project, self.assets)
        entered = threading.Event()
        calls = []
        def reducing(*args):
            calls.append(args[0])
            if len(calls) == 2:
                entered.set()
                if not args[5].wait(5):
                    raise RuntimeError("Cancellation timed out in test")
                raise DownloadCancelled("Cancelled batch")
            return self.fixture.reduce(*args)
        self.fixture.reducer.side_effect = reducing
        self.batch.start(batch_id)
        self.assertTrue(entered.wait(3))
        self.batch.cancel(batch_id)
        self.batch.cancel(batch_id)
        self.assertTrue(self.batch.executor.wait(5))
        items = self.items(batch_id)
        self.assertEqual([item["status"] for item in items], ["success", "cancelled", "cancelled"])
        self.assertEqual(self.batch.view(batch_id)["batch"]["status"], "cancelled")
        self.assertTrue(all(Path(item["raw_source"]).exists() for item in items))
        self.assertEqual(len(calls), 2)

    def test_other_executor_can_cancel_during_long_active_stage(self):
        batch_id = self.batch.create(self.project, self.assets)
        entered = threading.Event()
        def reducing(*args):
            if not entered.is_set():
                entered.set()
                if not args[5].wait(4):
                    raise RuntimeError("Persisted cancellation was not observed")
                raise DownloadCancelled("Remote cancel")
            return self.fixture.reduce(*args)
        self.fixture.reducer.side_effect = reducing
        self.batch.start(batch_id)
        self.assertTrue(entered.wait(2))
        remote = BatchController(self.single.app_dir, self.store, self.single)
        remote.cancel_item(self.items(batch_id)[0]["id"])
        self.assertTrue(self.batch.executor.wait(4))
        self.assertEqual([item["status"] for item in self.items(batch_id)], ["cancelled", "success", "success"])

    def test_failed_lease_release_recovers_without_restarting_application(self):
        batch_id = self.batch.create(self.project, [self.assets[0]])
        with patch.object(self.store, "release_processing_batch", side_effect=sqlite3.OperationalError("Storage unavailable")):
            self.batch.executor.run(batch_id)
        self.assertIsNotNone(self.store.get_processing_batch(batch_id)["owner_token"])
        self.batch.executor.recover()
        self.assertIsNone(self.store.get_processing_batch(batch_id)["owner_token"])
        self.assertEqual(self.store.get_processing_batch(batch_id)["status"], "success")

    def test_remote_cancellation_reaps_a_real_subprocess(self):
        batch_id = self.batch.create(self.project, [self.assets[0]])
        entered = threading.Event()
        pidfile = self.fixture.root / "child.pid"
        script = self.fixture.root / "held.py"
        script.write_text("import os,time\nfrom pathlib import Path\n"
            f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
            "print('started',flush=True)\ntime.sleep(60)\n")
        def reducing(*args):
            run_process([sys.executable, str(script)], self.fixture.root, self.fixture.root / "held.log",
                lambda fraction, message: entered.set(), args[5], timeout=10)
            raise RuntimeError("Cancellation did not stop child")
        self.fixture.reducer.side_effect = reducing
        self.batch.start(batch_id)
        self.assertTrue(entered.wait(3))
        remote = BatchController(self.single.app_dir, self.store, self.single)
        remote.cancel_item(self.items(batch_id)[0]["id"])
        self.assertTrue(self.batch.executor.wait(4))
        self.assertEqual(self.items(batch_id)[0]["status"], "cancelled")
        if os.name != "nt":
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_thread_start_failure_does_not_leave_an_execution_lease(self):
        batch_id = self.batch.create(self.project, [self.assets[0]])
        with patch("majd_studio_3d.batch_processing.threading.Thread.start", side_effect=RuntimeError("No thread")), self.assertRaisesRegex(RuntimeError, "No thread"):
            self.batch.start(batch_id)
        self.assertIsNone(self.store.get_processing_batch(batch_id)["owner_token"])
        self.assertEqual(self.store.get_processing_batch(batch_id)["status"], "pending")

    def test_existing_model_operation_lock_keeps_batch_waiting_and_cancellable(self):
        resource = threading.Lock()
        resource.acquire()
        batch = BatchController(self.single.app_dir, self.store, self.single, resource)
        batch_id = batch.create(self.project, [self.assets[0]])
        batch.start(batch_id)
        for _ in range(20):
            if batch.view(batch_id)["counts"]["processing"]:
                break
            threading.Event().wait(.05)
        self.fixture.reducer.assert_not_called()
        batch.cancel(batch_id)
        self.assertTrue(batch.executor.wait(3))
        self.assertEqual(self.items(batch_id)[0]["status"], "cancelled")
        self.fixture.reducer.assert_not_called()
        resource.release()

    def test_recovery_does_not_run_workers_and_explicit_retry_reuses_completed_work(self):
        batch_id = self.batch.create(self.project, self.assets)
        self.store.claim_processing_batch(batch_id, "dead-owner", 99999999)
        active = self.store.claim_next_processing_item(batch_id, "dead-owner")
        source = self.items(batch_id)[0]["raw_source"]
        # Simulate verified pipeline completion just before the queue process died.
        self.fixture.service.process(source, project_id=self.project, asset_id=self.assets[0])
        previous_calls = self.fixture.reducer.call_count
        restored = BatchController(self.single.app_dir, self.store, self.single)
        self.assertEqual(self.store.get_processing_batch_item(active["id"])["status"], "failed")
        self.assertEqual(self.store.get_processing_batch(batch_id)["status"], "interrupted")
        self.assertEqual(self.fixture.reducer.call_count, previous_calls)
        restored.retry_failed(batch_id)
        restored.executor.run(batch_id)
        self.assertEqual(self.items(batch_id)[0]["status"], "reused")
        self.assertEqual(restored.view(batch_id)["counts"]["completed"], 3)

    def test_frozen_source_tamper_fails_only_that_item_and_history_hide_is_safe(self):
        batch_id = self.batch.create(self.project, self.assets)
        first = self.items(batch_id)[0]
        Path(first["raw_source"]).write_text(json.dumps({"faces": 50}))
        self.batch.executor.run(batch_id)
        self.assertEqual([item["status"] for item in self.items(batch_id)], ["failed", "success", "success"])
        self.assertTrue(self.batch.hide(batch_id))
        self.assertNotIn(batch_id, [value for _, value in self.batch.history(self.project)])
        self.assertIsNotNone(self.store.get_processing_batch(batch_id))
        self.assertEqual(len(self.store.list_processing_runs()), 3)


if __name__ == "__main__":
    unittest.main()
