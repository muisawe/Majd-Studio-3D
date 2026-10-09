"""Batch Gradio callback bindings use controller APIs, not model workers."""

import importlib.util
import unittest
from unittest.mock import Mock

from majd_studio_3d.batch_gradio import mount_batch_panel


class FakeBatchController:
    def __init__(self):
        self.created = []
        self.start = Mock()
        self.cancel = Mock()
        self.retry_failed = Mock()
        self.hidden = False
        self.hide = Mock(side_effect=lambda batch_id: setattr(self, "hidden", True))
        self.cancel_item = Mock()
        self.retry_item = Mock()
        self.model = {"batch": {"id": "batch", "status": "pending"},
                      "items": [{"id": "item", "asset_name": "Chair", "asset_id": 1,
                                 "candidate_index": 0, "status": "queued"}],
                      "counts": {"total": 3, "queued": 3, "processing": 0, "completed": 0,
                                 "reused": 0, "failed": 0, "cancelled": 0},
                      "controls": {"start": True, "cancel": True, "retry_failed": False, "hide": False},
                      "item_controls": {"item": {"cancel": True, "retry": False}}}

    def choices(self, project_id):
        return [("Chair", 1), ("Table", 2), ("Lamp", 3)] if project_id == 1 else []

    def history(self, project_id):
        return [("Batch", "batch")] if project_id == 1 and not self.hidden else []

    def create(self, project_id, asset_ids, action):
        if not asset_ids:
            raise ValueError("Choose at least one asset")
        self.created.append((project_id, asset_ids, action))
        return "batch"

    def view(self, batch_id):
        return self.model if batch_id == "batch" else {"batch": None, "items": [], "controls": {}}


@unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio is not installed")
class BatchGradioTests(unittest.TestCase):
    def setUp(self):
        import gradio as gr
        self.controller = FakeBatchController()
        with gr.Blocks(analytics_enabled=False) as self.app:
            project = gr.Dropdown(choices=[("Project", 1)], value=1)
            self.panel = mount_batch_panel(self.controller, project)

    def test_selection_one_multiple_all_and_clear(self):
        actions = self.panel["actions"]
        self.assertEqual(actions["select_all"](1)["value"], [1, 2, 3])
        self.assertEqual(actions["clear"]()["value"], [])
        actions["process"](1, [1])
        actions["process"](1, [1, 2, 3])
        self.assertEqual(self.controller.created, [(1, [1], "process"), (1, [1, 2, 3], "process")])
        self.controller.start.assert_not_called()

    def test_create_captures_batch_selection_without_starting_worker(self):
        response = self.panel["actions"]["reprocess"](1, [1, 2, 3])
        self.assertEqual(response[0]["value"], "batch")
        self.assertIn("Start Batch", response[-1])
        self.assertEqual(len(response), len(self.panel["outputs"]) + 1)
        self.assertEqual(self.controller.created, [(1, [1, 2, 3], "reprocess")])
        self.controller.start.assert_not_called()

    def test_empty_selection_error_preserves_current_widgets(self):
        response = self.panel["actions"]["process"](1, [])
        self.assertIn("Choose at least one", response[-1])
        self.assertTrue(all(value == {"__type__": "update"} for value in response[:-1]))

    def test_refresh_controls_match_backend_and_no_worker_polling(self):
        response = self.panel["refresh"](1, "batch", "item")
        self.assertIn("Ready to start", response[1])
        self.assertEqual(response[2][0][0], "Chair")
        self.assertEqual(response[2][0][2], "Queued")
        self.assertEqual(response[3]["value"], "item")
        self.assertTrue(response[4]["interactive"])
        self.assertTrue(response[8]["interactive"])
        self.assertFalse(response[9]["interactive"])
        self.controller.start.assert_not_called()
        self.assertEqual(self.panel["timer"].value, 2)
        self.assertTrue(all(dep["queue"] is False for dep in self.app.config["dependencies"]))

    def test_start_and_batch_cancel_call_controller(self):
        self.panel["actions"]["start"](1, "batch", "item")
        self.panel["actions"]["cancel"](1, "batch", "item")
        self.controller.start.assert_called_once_with("batch")
        self.controller.cancel.assert_called_once_with("batch")

    def test_start_rejection_does_not_report_running(self):
        self.controller.start.return_value = False
        response = self.panel["actions"]["start"](1, "batch", "item")
        self.assertIn("could not start", response[-1])
        self.assertNotIn("execution requested", response[-1])

    def test_reload_selects_latest_persisted_batch_without_starting_workers(self):
        response = self.panel["refresh"](1)
        self.assertEqual(response[0]["value"], "batch")
        self.assertIn("Ready to start", response[1])
        self.controller.start.assert_not_called()

    def test_project_change_during_create_does_not_overwrite_new_selection(self):
        selection = {"project_id": 1, "batch_id": None, "item_id": None}

        def create(*args, **kwargs):
            self.panel["project_changed"](2, selection)
            return "batch"

        self.controller.create = create
        response = self.panel["actions"]["process"](1, [1, 2], selection)
        self.assertTrue(all(value == {"__type__": "update"} for value in response))
        self.assertEqual(selection["project_id"], 2)

    def test_batch_selection_change_during_create_does_not_overwrite_it(self):
        selection = {"project_id": 1, "batch_id": None, "item_id": None}

        def create(*args, **kwargs):
            self.panel["selection_changed"](1, "batch", "item", selection)
            return "batch"

        self.controller.create = create
        response = self.panel["actions"]["process"](1, [1, 2], selection)
        self.assertTrue(all(value == {"__type__": "update"} for value in response))
        self.assertEqual(selection["item_id"], "item")

    def test_project_change_during_create_error_preserves_new_selection(self):
        selection = {"project_id": 1, "batch_id": None, "item_id": None}

        def create(*args, **kwargs):
            self.panel["project_changed"](2, selection)
            raise ValueError("Old project's creation failed")

        self.controller.create = create
        response = self.panel["actions"]["process"](1, [1, 2], selection)
        self.assertTrue(all(value == {"__type__": "update"} for value in response))

    def test_retry_failed_and_retry_item_require_actual_capabilities(self):
        denied = self.panel["actions"]["retry_failed"](1, "batch", "item")
        self.assertIn("unavailable", denied[-1])
        self.controller.retry_failed.assert_not_called()
        self.controller.model["controls"]["retry_failed"] = True
        self.controller.model["item_controls"]["item"]["retry"] = True
        self.panel["actions"]["retry_failed"](1, "batch", "item")
        self.panel["actions"]["retry_item"](1, "batch", "item")
        self.controller.retry_failed.assert_called_once_with("batch")
        self.controller.retry_item.assert_called_once_with("item")

    def test_cancel_item_is_separate_from_batch_cancel(self):
        self.panel["actions"]["cancel_item"](1, "batch", "item")
        self.controller.cancel_item.assert_called_once_with("item")
        self.controller.cancel.assert_not_called()

    def test_hidden_history_clears_selection_without_deleting_runs(self):
        self.controller.model["controls"]["hide"] = True
        response = self.panel["actions"]["hide"](1, "batch", "item")
        self.controller.hide.assert_called_once_with("batch")
        self.assertIsNone(response[0]["value"])
        self.assertIn("records are preserved", response[-1])

    def test_project_switch_clears_batch_and_asset_selection(self):
        response = self.panel["project_changed"](2)
        self.assertEqual(response[0]["value"], [])
        self.assertEqual(response[0]["choices"], [])
        self.assertIsNone(response[1]["value"])
        self.assertEqual(response[3], [])
        refreshed = self.panel["refresh"](2, "batch", "item")
        self.assertIsNone(refreshed[0]["value"])
        self.assertIsNone(refreshed[3]["value"])

    def test_reused_failed_and_interrupted_states_render_after_reload(self):
        self.controller.model["batch"]["status"] = "interrupted"
        self.controller.model["items"] = [{"id": "item", "asset_name": "Chair", "status": "reused"},
                                          {"id": "two", "asset_name": "Table", "status": "failed", "error": "Restart"}]
        self.controller.model["counts"].update(completed=1, reused=1, failed=1, queued=1)
        response = self.panel["refresh"](1, "batch", "two")
        self.assertIn("Interrupted", response[1])
        self.assertEqual(response[2][0][2], "Existing result reused")
        self.assertEqual(response[2][1][-1], "Restart")


if __name__ == "__main__":
    unittest.main()
