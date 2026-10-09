"""Presentation coverage without importing Gradio or model runtimes."""

import unittest

from majd_studio_3d.processing_ui import (
    ACTIVE_STATES,
    STATE_LABELS,
    format_count,
    render_processing_card,
    view_model,
)


class ProcessingPresentationTests(unittest.TestCase):
    def model(self, run=None, **kwargs):
        return view_model({"id": 7, "name": "Chair", "status": "review"}, {"faces": 999}, run, **kwargs)

    def test_count_formatting_and_unknown_settings(self):
        self.assertEqual(format_count(1000), "1,000")
        for value in (None, -1, True, "1000", 1.2):
            self.assertEqual(format_count(value), "—")
        html = render_processing_card(self.model())
        self.assertIn("After generation: —", html)
        self.assertIn("FaceReducer: —", html)
        self.assertEqual(self.model(active=True)["label"], "Pending processing")

    def test_canonical_failed_and_cancelled_stages_have_human_labels(self):
        for stage, label in (("inspecting", "Raw inspection"), ("reducing", "FaceReducer"),
                             ("validating_reduced", "Reduced validation"), ("cleaning", "Cleanup"),
                             ("validating_final", "Final validation")):
            with self.subTest(stage=stage):
                model = self.model({"status": "failed", "failed_stage": stage, "error": "Failure"})
                self.assertEqual(model["errors"][0]["stage"], label)
                cancelled = self.model({"status": "cancelled", "cancelled_stage": stage, "error": "Cancelled"})
                self.assertEqual(cancelled["errors"][0]["stage"], label)

    def test_all_backend_states_have_readable_labels(self):
        for state, label in STATE_LABELS.items():
            with self.subTest(state=state):
                self.assertEqual(self.model({"state": state})["label"], label)
        for state in ACTIVE_STATES:
            self.assertIn(state, STATE_LABELS)

    def test_legacy_missing_metadata_is_not_fabricated_from_candidate(self):
        model = self.model()
        self.assertEqual(model["state"], "pending")
        self.assertEqual(model["generation_status"], "Ready for review")
        for key in ("original_faces", "requested_face_budget", "reduced_faces", "final_faces"):
            self.assertIsNone(model[key])
        self.assertIn("—", render_processing_card(model))
        self.assertNotIn("999", render_processing_card(model))

    def test_success_count_display(self):
        model = self.model({"status": "success", "original_faces": 1000,
                            "requested_face_budget": 300, "reduced_faces": 300, "final_faces": 290},
                           raw_available=True, processed_available=True)
        self.assertTrue(model["controls"]["view_processed"])
        self.assertEqual(model["requested_face_budget"], 300)
        self.assertIn("290", render_processing_card(model))

    def test_stage_errors_and_raw_fallback(self):
        for stage in ("inspection", "FaceReducer", "cleanup", "validation"):
            with self.subTest(stage=stage):
                model = self.model({"status": "failed", "failed_stage": stage,
                                    "error": "test failure", "raw_fallback": True},
                                   raw_available=True, processed_available=True)
                self.assertEqual(model["errors"], [{"stage": stage, "message": "test failure"}])
                self.assertTrue(model["controls"]["retry"])
                self.assertFalse(model["controls"]["view_processed"])
                self.assertIn("Raw fallback · not cleaned", render_processing_card(model))

    def test_distinct_reducer_and_cleanup_errors(self):
        model = self.model({"status": "failed", "face_reducer_error": "reducer unavailable",
                            "cleanup_result": {"error": "Blender missing"}, "error": "fallback retained"})
        self.assertEqual([item["stage"] for item in model["errors"]], ["FaceReducer", "Cleanup", "Processing"])

    def test_cancelled_and_reused_are_distinct(self):
        cancelled = self.model({"status": "cancelled", "cancelled_stage": "cleanup", "error": "Cancelled"})
        self.assertEqual(cancelled["label"], "Processing cancelled")
        self.assertFalse(cancelled["controls"]["retry"])
        reused = self.model({"status": "success", "reused": True})
        self.assertEqual(reused["state"], "reused")
        self.assertIn("Existing result reused", render_processing_card(reused))
        self.assertEqual(render_processing_card(reused).count("Existing result reused"), 1)

    def test_busy_and_compatible_controls(self):
        self.assertTrue(self.model(raw_available=True)["controls"]["start"])
        for options in ({"busy": True}, {"active": True}, {"compatible": True}, {"processable": False}):
            with self.subTest(options=options):
                self.assertFalse(self.model(raw_available=True, **options)["controls"]["start"])
        active = self.model({"state": "cleaning"}, active=True, raw_available=True)
        self.assertTrue(active["controls"]["cancel"])
        self.assertFalse(active["controls"]["reprocess"])
        busy = self.model({"status": "success"}, busy=True, raw_available=True)
        self.assertFalse(busy["controls"]["cancel"])
        self.assertFalse(busy["controls"]["reprocess"])
        ready = self.model({"status": "success"}, raw_available=True, folder_available=True)
        self.assertTrue(ready["controls"]["reprocess"])
        self.assertTrue(ready["controls"]["open_folder"])

    def test_historical_config_display_wins_over_current_settings(self):
        model = self.model({"configuration_snapshot": {"target_faces": 300, "auto_cleanup": True,
                            "resolved_blender_path": "/old/Blender", "hunyuan_face_reducer": True}},
                           config={"target_faces": 90, "blender_path": "/new/Blender"})
        html = render_processing_card(model)
        self.assertIn("Target: 300 faces", html)
        self.assertIn("/old/Blender", html)
        self.assertIn("/new/Blender", html)
        self.assertIn("Current settings:", html)
        self.assertIn("Target: 90 faces", html)
        self.assertIn("After generation: On", html)
        self.assertIn("cleanup_config.json", html)

    def test_cancelled_or_incompatible_previous_result_can_start(self):
        for status in ("cancelled", "success"):
            with self.subTest(status=status):
                controls = self.model({"status": status}, raw_available=True)["controls"]
                self.assertTrue(controls["start"])
        self.assertFalse(self.model({"status": "failed"}, raw_available=True)["controls"]["start"])

    def test_warning_counts_and_blender_availability(self):
        empty = render_processing_card(self.model(config={}))
        self.assertIn("Warnings: 0", empty)
        self.assertIn("Blender: <code>—</code>", empty)
        unavailable = render_processing_card(self.model(config={"resolved_blender_path": None, "blender_path": None}))
        self.assertIn("Unavailable", unavailable)
        self.assertIn('class="proc-stats"', empty)
        configured = render_processing_card(self.model({"warnings": ["First", "Second"]}, config={"blender_path": "/Blender"}))
        self.assertIn("Warnings: 2", configured)
        self.assertIn("Configured / not found: /Blender", configured)
        found = render_processing_card(self.model(config={"resolved_blender_path": "/Blender"}))
        self.assertIn("Found: /Blender", found)

    def test_html_escapes_every_user_controlled_field(self):
        attack = '<script>alert("x")</script>'
        model = view_model({"id": attack, "name": attack, "status": attack}, {},
                           {"state": attack, "warnings": [attack], "error": attack,
                            "failed_stage": attack, "raw_asset": attack, "processed_asset": attack,
                            "original_faces": attack}, {"blender_path": attack, "decimate_ratio": attack}, notice=attack)
        html = render_processing_card(model)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_fabricated_progress_or_verification_claim(self):
        html = render_processing_card(self.model({"state": "cleaning"}, active=True))
        self.assertNotIn("Blender verified", html)
        self.assertNotIn("progressbar", html)
        self.assertIn("Cleaning geometry", html)


if __name__ == "__main__":
    unittest.main()
