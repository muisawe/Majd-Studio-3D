"""Batch presentation reflects persisted accounting without owning execution."""

import unittest

from majd_studio_3d.batch_ui import (
    BATCH_LABELS,
    ITEM_HEADERS,
    ITEM_LABELS,
    item_rows,
    render_batch_card,
)


class BatchPresentationTests(unittest.TestCase):
    def test_empty_and_legacy_metadata(self):
        self.assertEqual(item_rows({}), [])
        self.assertIn("Select generated assets", render_batch_card({}))
        html = render_batch_card({"batch": {"id": "old"}})
        self.assertIn("Ready to start", html)
        self.assertIn("—", html)
        self.assertEqual(item_rows({"items": [{"id": "old"}]})[0],
                         ["—", "—", "—", "—", "", "0", ""])

    def test_all_batch_states_display(self):
        for state, label in BATCH_LABELS.items():
            with self.subTest(state=state):
                self.assertIn(label, render_batch_card({"batch": {"id": "b", "status": state}}))

    def test_counts_come_from_controller_including_reused_completion(self):
        counts = {"total": 7, "queued": 1, "processing": 1, "completed": 3,
                  "reused": 2, "failed": 1, "cancelled": 1}
        html = render_batch_card({"batch": {"id": "b", "status": "running"}, "counts": counts})
        for key, value in counts.items():
            self.assertIn(f'<span>{key.capitalize()}</span><strong>{value}</strong>', html)
        self.assertIn("Reused results count as completed", html)
        self.assertNotIn("%", html)

    def test_html_escapes_notice_identity_and_configuration(self):
        html = render_batch_card({"batch": {"id": '<img src=x onerror="x">'},
                                 "notice": "<script>alert(1)</script>",
                                 "configuration_snapshot": {"path": "<svg onload=x>"}})
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;svg", html)

    def test_rows_reflect_stage_warnings_errors_and_retry_count(self):
        rows = item_rows({"items": [{"id": "a", "asset_name": "Chair", "candidate_index": 0,
                                     "status": "processing", "stage": "cleaning", "warnings": ["UV", "Island"],
                                     "retry_count": 2, "error": "Cleanup timeout"}]})
        self.assertEqual(len(rows[0]), len(ITEM_HEADERS))
        self.assertEqual(rows[0], ["Chair", "1", "Processing", "Cleaning geometry", "UV; Island", "2", "Cleanup timeout"])

    def test_recorded_candidate_number_takes_precedence(self):
        row = item_rows({"items": [{"candidate_number": 9, "candidate_index": 1}]})[0]
        self.assertEqual(row[1], "9")

    def test_all_item_states_and_serialized_warnings(self):
        for state, label in ITEM_LABELS.items():
            with self.subTest(state=state):
                row = item_rows({"items": [{"status": state, "warnings": '["warning"]'}]})[0]
                self.assertEqual(row[2], label)
                self.assertEqual(row[4], "warning")
        row = item_rows({"items": [{"warning_summary": "plain warning", "error_message": "Old error"}]})[0]
        self.assertEqual(row[4], "plain warning")
        self.assertEqual(row[6], "Old error")


if __name__ == "__main__":
    unittest.main()
