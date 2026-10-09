"""Review presentation distinguishes recommendation, selection and approval."""

import unittest

from majd_studio_3d.review_ui import bulk_rows, history_rows, render_review_card


class ReviewPresentationTests(unittest.TestCase):
    def view(self):
        return {"asset": {"id": 1, "name": "Chair", "status": "success"},
                "review": {"review_status": "APPROVED", "selected_candidate_id": "two", "approved_candidate_id": "two"},
                "candidates": [
                    {"candidate_id": "one", "rank": 1, "score": .9, "processing_status": "success", "artifact_path": "one.glb", "artifact_available": True},
                    {"candidate_id": "two", "rank": 2, "score": .8, "processing_status": "success", "artifact_path": "two.glb", "artifact_available": True,
                     "metadata": {"configuration_snapshot": {"target_faces": 300}, "raw_asset": "raw.glb", "warnings": ["UV warning"],
                                  "validation": {"status": "success"}, "provenance": {"sha": "123"}}},
                    {"candidate_id": "three", "rank": 3, "score": .7, "processing_status": "failed"}]}

    def test_recommendation_selection_and_approval_are_separate_cards(self):
        html = render_review_card(self.view())
        cards = html.split('<article class="review-candidate">')[1:]
        self.assertEqual(len(cards), 3)
        self.assertIn("Recommended by ranking", cards[0])
        self.assertNotIn("Approved candidate", cards[0])
        self.assertNotIn("Selected by reviewer", cards[0])
        self.assertIn("Selected by reviewer", cards[1])
        self.assertIn("Approved candidate", cards[1])
        self.assertNotIn("Recommended by ranking", cards[1])
        self.assertIn("Unavailable", cards[2])

    def test_metadata_provenance_configuration_and_warnings_are_exposed(self):
        html = render_review_card(self.view())
        for value in ("target_faces", "300", "raw.glb", "UV warning", "provenance", "validation", "Processing: success"):
            self.assertIn(value, html)

    def test_rank_one_is_never_presented_as_selected_without_selection(self):
        view = self.view()
        view["review"] = {"review_status": "NEEDS_REVIEW"}
        html = render_review_card(view)
        self.assertIn("Recommended by ranking", html)
        self.assertNotIn("Selected by reviewer", html)
        self.assertNotIn("Approved candidate", html)

    def test_failed_processing_with_no_review_or_candidates_remains_visible(self):
        html = render_review_card({"asset": {"name": "Failed chair", "status": "failed"}})
        self.assertIn("Processing: failed", html)
        self.assertIn("No review record", html)
        self.assertIn("No candidates", html)

    def test_candidate_text_and_nested_metadata_escape_html(self):
        view = self.view()
        view["asset"]["name"] = "<script>bad()</script>"
        view["candidates"][0]["candidate_id"] = '<img src=x onerror="bad()">'
        view["candidates"][0]["metadata"] = {"note": "<svg onload=x>"}
        html = render_review_card(view)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<svg", html)
        self.assertIn("&lt;script&gt;", html)

    def test_history_and_partial_bulk_results_preserve_each_outcome(self):
        view = {"history": [{"action": "approve", "created_at": "now", "candidate_id": "two",
                             "previous_state": "NEEDS_REVIEW", "new_state": "APPROVED", "reason": "Preferred silhouette"}]}
        self.assertEqual(history_rows(view), [["approve", "now", "two", "NEEDS_REVIEW", "APPROVED", "Preferred silhouette"]])
        self.assertEqual(history_rows({}), [])
        self.assertEqual(bulk_rows([{"asset_id": 1, "status": "approved"},
                                    {"asset_id": 2, "status": "failed", "error": "Select a candidate"}]),
                         [["1", "approved", ""], ["2", "failed", "Select a candidate"]])

    def test_counters_are_recorded_separately_from_candidate_ranking(self):
        view = self.view()
        view["counts"] = {"processed": 7, "needs_review": 3, "approved": 2, "rejected": 1, "retry_requested": 1, "failed": 4}
        html = render_review_card(view)
        for label, value in (("Processed", 7), ("Needs Review", 3), ("Approved", 2), ("Rejected", 1), ("Retry Requested", 1), ("Failed", 4)):
            self.assertIn(f'<span>{label}</span><strong>{value}</strong>', html)

    def test_missing_artifact_is_not_reported_available_because_a_path_exists(self):
        view = self.view()
        view["candidates"] = [{"candidate_id": "missing", "artifact_path": "missing.glb", "artifact_available": False}]
        self.assertIn("Unavailable", render_review_card(view))

    def test_empty_view_prompts_asset_selection(self):
        self.assertIn("Select an asset", render_review_card({}))


if __name__ == "__main__":
    unittest.main()
