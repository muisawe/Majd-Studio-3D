"""Real Gradio bindings invoke the review controller without choosing rank one."""

import importlib.util
import unittest
from unittest.mock import Mock

from majd_studio_3d.review_gradio import mount_review_panel


class FakeReviewController:
    def __init__(self):
        self.model = {"asset": {"id": 1, "name": "Chair", "status": "success"},
                      "review": {"review_status": "NEEDS_REVIEW", "selected_candidate_id": None},
                      "candidates": [{"candidate_id": f"c{i}", "rank": i, "score": 1 / i,
                                      "processing_status": "success", "artifact_path": f"{i}.glb"} for i in (1, 2, 3)],
                      "history": []}
        self.select = Mock(side_effect=self._select)
        self.clear = Mock(side_effect=lambda asset_id: self.model["review"].update(selected_candidate_id=None))
        self.approve = Mock(side_effect=self._approve)
        self.reject = Mock(side_effect=lambda asset_id, reason="": self.model["review"].update(review_status="REJECTED"))
        self.request_retry = Mock(return_value="batch-retry")
        self.bulk_approve = Mock(return_value=[{"asset_id": 1, "status": "approved"},
                                               {"asset_id": 2, "status": "approved"},
                                               {"asset_id": 3, "status": "failed", "error": "Explicit candidate selection required"}])

    def _select(self, asset_id, candidate_id):
        if candidate_id is None:
            raise ValueError("Explicit candidate selection required")
        self.model["review"]["selected_candidate_id"] = candidate_id

    def _approve(self, asset_id):
        if self.model["review"]["selected_candidate_id"] is None:
            raise ValueError("Explicit candidate selection required")
        self.model["review"].update(review_status="APPROVED", approved_candidate_id=self.model["review"]["selected_candidate_id"])

    def counters(self, asset_ids):
        return {"processed": len(asset_ids), "needs_review": 1, "approved": 0, "rejected": 0, "retry_requested": 0, "failed": 0}

    def choices(self, project_id, filter_name="All"):
        if project_id != 1:
            return []
        if filter_name == "Failed Processing":
            return [("Failed table", 2)]
        return [("Chair", 1)]

    def view(self, asset_id):
        if asset_id == 2:
            return {"asset": {"id": 2, "name": "Failed table", "status": "failed"}}
        return self.model if asset_id == 1 else {}


@unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio is not installed")
class ReviewGradioTests(unittest.TestCase):
    def setUp(self):
        import gradio as gr
        self.controller = FakeReviewController()
        with gr.Blocks(analytics_enabled=False) as self.app:
            project = gr.Dropdown(choices=[("Project", 1)], value=1)
            self.panel = mount_review_panel(self.controller, project)

    def test_load_exposes_all_candidates_without_selecting_or_approving_rank_one(self):
        response = self.panel["refresh"](1)
        self.assertEqual(response[0]["value"], 1)
        self.assertEqual(len(response[2]["choices"]), 3)
        self.assertIsNone(response[2]["value"])
        self.controller.select.assert_not_called()
        self.controller.approve.assert_not_called()

    def test_dropdown_only_does_not_approve_without_persisted_selection(self):
        response = self.panel["actions"]["approve"](1, "All", 1, "c2")
        self.assertIn("Explicit candidate selection", response[-1])
        self.controller.approve.assert_called_once_with(1)
        self.controller.select.assert_not_called()
        self.assertIsNone(self.controller.model["review"]["selected_candidate_id"])

    def test_explicit_rank_two_selection_then_approval_and_reload(self):
        selected = self.panel["actions"]["select"](1, "All", 1, "c2")
        self.assertEqual(selected[2]["value"], "c2")
        approved = self.panel["actions"]["approve"](1, "All", 1, "c1")
        self.assertIn("Approved candidate", approved[1])
        self.assertEqual(self.controller.model["review"]["approved_candidate_id"], "c2")
        reloaded = self.panel["project_changed"](1)
        self.assertEqual(reloaded[3]["value"], "c2")
        self.assertIn("Approved candidate", reloaded[2])
        self.assertEqual(len(reloaded), len(self.panel["project_outputs"]))

    def test_clear_is_explicit_controller_action(self):
        self.panel["actions"]["select"](1, "All", 1, "c2")
        response = self.panel["actions"]["clear"](1, "All", 1, "c2")
        self.controller.clear.assert_called_once_with(1)
        self.assertIsNone(response[2]["value"])

    def test_reject_preserves_candidates_and_does_not_request_retry(self):
        response = self.panel["actions"]["reject"](1, "All", 1, None, "Wrong shape")
        self.controller.reject.assert_called_once_with(1, reason="Wrong shape")
        self.assertIn("Review: Rejected", response[1])
        self.assertEqual(len(response[2]["choices"]), 3)
        self.controller.request_retry.assert_not_called()

    def test_retry_delegates_to_existing_queue_and_displays_batch_reference(self):
        response = self.panel["actions"]["retry"](1, "All", 1, None, "Repair failed")
        self.controller.request_retry.assert_called_once_with(1, reason="Repair failed")
        self.assertIn("batch-retry", response[-1])
        self.assertIn("Start Batch", response[-1])
        self.assertEqual(len(response[2]["choices"]), 3)

    def test_failed_processing_filter_includes_assets_with_no_review(self):
        response = self.panel["filter_changed"](1, "Failed Processing")
        self.assertEqual(response[0]["value"], 2)
        self.assertIn("Processing: failed", response[1])
        self.assertEqual(response[2]["choices"], [])

    def test_partial_bulk_results_display_successes_and_missing_selection_error(self):
        response = self.panel["actions"]["bulk_approve"](1, "All", 1, [1, 2, 3])
        self.controller.bulk_approve.assert_called_once_with([1, 2, 3])
        self.assertEqual(response[-2], [["1", "approved", ""], ["2", "approved", ""],
                                        ["3", "failed", "Explicit candidate selection required"]])
        self.controller.select.assert_not_called()

    def test_project_switch_clears_bulk_and_asset_selection(self):
        selection = {"project_id": 1, "asset_id": 1, "filter_name": "Approved"}
        response = self.panel["project_changed"](2, selection)
        self.assertIsNone(response[1]["value"])
        self.assertEqual(response[-1]["choices"], [])
        self.assertEqual(response[-1]["value"], [])
        self.assertEqual(selection, {"project_id": 2, "asset_id": None, "filter_name": "All"})

    def test_late_action_response_does_not_overwrite_changed_project(self):
        selection = {"project_id": 1, "asset_id": 1, "filter_name": "All"}

        def select(*args):
            self.panel["project_changed"](2, selection)

        self.controller.select.side_effect = select
        response = self.panel["actions"]["select"](1, "All", 1, "c2", "", selection)
        self.assertTrue(all(value == {"__type__": "update"} for value in response))

    def test_real_gradio_bindings_are_nonqueued_and_expose_all_filters(self):
        self.assertTrue(all(dep["queue"] is False for dep in self.app.config["dependencies"]))
        filters = self.panel["widgets"]["filter"].choices
        self.assertIn(("Retry Requested", "Retry Requested"), filters)
        self.assertIn(("Failed Processing", "Failed Processing"), filters)


if __name__ == "__main__":
    unittest.main()
