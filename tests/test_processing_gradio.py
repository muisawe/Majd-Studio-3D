"""Real Gradio bindings with a fake controller; optional on lightweight installs."""

import importlib.util
import unittest
from unittest.mock import Mock

from majd_studio_3d.processing_gradio import mount_processing_panel
from majd_studio_3d.processing_ui import view_model


class FakeController:
    def __init__(self):
        self.active = False
        self.run_calls = []
        self.cancel_calls = 0
        self.result = None

    def view(self, asset_id, index):
        model = view_model({"id": asset_id, "name": "Fixture", "status": "review"}, {}, self.result,
                           active=self.active, raw_available=bool(asset_id),
                           processed_available=bool(self.result and self.result.get("status") == "success"),
                           folder_available=bool(self.result))
        return model

    def is_active(self, asset_id):
        return self.active

    def run(self, asset_id, index, action, progress):
        self.run_calls.append((asset_id, index, action))
        self.active = True
        progress("reducing")
        self.result = {"status": "success", "state": "success", "original_faces": 1000,
                       "requested_face_budget": 300, "reduced_faces": 300, "final_faces": 290}
        self.active = False
        return self.result

    def cancel(self, asset_id, index):
        self.cancel_calls += 1
        if not self.active:
            return False
        self.active = False
        self.result = {"status": "cancelled", "state": "cancelled"}
        return True


@unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio is not installed")
class ProcessingGradioTests(unittest.TestCase):
    def setUp(self):
        import gradio as gr
        self.controller = FakeController()
        self.complete = Mock(return_value=("Existing QA", gr.update(choices=[("Candidate 1", 0)], value=0)))
        self.preview = Mock(return_value="Preview loaded")
        self.folder = Mock(return_value="Folder opened")
        with gr.Blocks(analytics_enabled=False) as self.app:
            asset = gr.Dropdown(choices=[("Fixture", 1)], value=1)
            candidate = gr.Dropdown(choices=[("Candidate 1", 0)], value=0)
            review = gr.Textbox()
            approve = gr.Button("Approve")
            requeue = gr.Button("Requeue")
            self.panel = mount_processing_panel(self.controller, asset, candidate, review, approve, requeue,
                on_complete=self.complete, on_preview=self.preview, on_folder=self.folder)
        self.app.queue(default_concurrency_limit=3, max_size=20)

    def test_binding_events_use_existing_queue_and_nonblocking_poll_cancel(self):
        deps = self.app.config["dependencies"]
        self.assertEqual(len(deps), 10)
        self.assertEqual(sum(dep["queue"] is False for dep in deps), 7)
        actions = self.panel["actions"]
        self.assertEqual(set(actions), {"start", "retry", "cancel", "reprocess", "view_raw", "view_processed", "open_folder"})
        self.assertEqual(len(self.panel["outputs"]), 10)
        self.assertEqual(self.panel["timer"].value, 2)

    def test_run_updates_counts_and_preserves_existing_review_selection(self):
        progress = Mock()
        response = self.panel["actions"]["start"](1, 0, progress=progress)
        self.assertEqual(len(response), 13)
        self.assertIn("Processing complete", response[0])
        self.assertIn("290", response[0])
        self.assertEqual(response[-2], "Existing QA")
        self.complete.assert_called_once_with(1, 0)
        self.assertEqual(self.controller.run_calls, [(1, 0, "start")])
        progress.assert_called_once_with((0, None), desc="Reducing faces")

    def test_server_control_check_prevents_unavailable_run_and_preview(self):
        response = self.panel["actions"]["start"](None, 0, progress=Mock())
        self.assertEqual(self.controller.run_calls, [])
        self.complete.assert_not_called()
        self.assertIn("unavailable", response[10])
        self.assertIn("unavailable", self.panel["actions"]["view_processed"](1, 0))
        self.preview.assert_not_called()

    def test_changed_selection_during_run_never_overwrites_current_review(self):
        selection = {"asset_id": 1, "candidate_index": "0"}

        def run(asset_id, index, action, progress):
            self.panel["refresh"](2, 1, selection)
            return {"status": "success", "state": "success"}

        self.controller.run = run
        response = self.panel["actions"]["start"](1, 0, selection, progress=Mock())
        self.complete.assert_not_called()
        self.assertEqual(len(response), 13)
        self.assertTrue(all(value == {"__type__": "update"} for value in response))
        self.assertEqual(selection, {"asset_id": 2, "candidate_index": "1"})

    def test_changed_selection_during_error_never_overwrites_current_review(self):
        selection = {"asset_id": 1, "candidate_index": "0"}

        def run(asset_id, index, action, progress):
            selection.update(asset_id=2, candidate_index="0")
            raise ValueError("Old selection failure")

        self.controller.run = run
        response = self.panel["actions"]["start"](1, 0, selection, progress=Mock())
        self.complete.assert_not_called()
        self.assertTrue(all(value == {"__type__": "update"} for value in response))

    def test_nonterminal_return_does_not_refresh_review(self):
        self.controller.run = Mock(return_value={"state": "cleaning"})
        response = self.panel["actions"]["start"](1, 0, progress=Mock())
        self.complete.assert_not_called()
        self.assertEqual(response[10], "Cleaning geometry")

    def test_cancel_is_repeatable_and_refreshes_terminal_state(self):
        self.controller.active = True
        first = self.panel["actions"]["cancel"](1, 0)
        second = self.panel["actions"]["cancel"](1, 0)
        self.assertIn("Processing cancelled", first[0])
        self.assertIn("Cancellation requested", first[-1])
        self.assertIn("No active processing", second[-1])
        self.assertEqual(self.controller.cancel_calls, 2)

    def test_active_selection_disables_approval_and_requeue(self):
        self.controller.active = True
        self.controller.result = {"state": "cleaning"}
        updates = self.panel["refresh"](1, 0)
        self.assertFalse(updates[-1]["interactive"])
        self.assertFalse(updates[-2]["interactive"])
        self.assertTrue(updates[3]["interactive"])

    def test_raw_processed_and_folder_callbacks_use_current_selection(self):
        self.panel["actions"]["view_raw"](1, 0)
        self.preview.assert_called_with(1, 0, "raw")
        self.controller.result = {"status": "success"}
        self.panel["actions"]["view_processed"](1, 0)
        self.preview.assert_called_with(1, 0, "processed")
        self.panel["actions"]["open_folder"](1, 0)
        self.folder.assert_called_once_with(1, 0)


if __name__ == "__main__":
    unittest.main()
