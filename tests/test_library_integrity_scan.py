import unittest

from majd_studio_3d.library_controller import LibraryController
from majd_studio_3d.library_ui import render_counters


class FakeStore:
    def __init__(self, issues=None, error=None):
        self.issues = issues or []
        self.error = error

    def library_counters(self):
        return {"active_assets": 2}

    def library_integrity_issues(self):
        if self.error:
            raise self.error
        return self.issues


class IntegrityScanTests(unittest.TestCase):
    def test_issues_found_in_background_appear_in_counters(self):
        controller = LibraryController(FakeStore([{"kind": "missing_artifact", "version_id": "v1"}]))
        self.assertEqual(controller.counters(), {"active_assets": 2})
        with self.assertLogs("majd.library", "WARNING"):
            controller.start_integrity_scan().join()
        self.assertEqual(controller.counters()["integrity_issues"], 1)
        self.assertIn("Integrity Issues", render_counters(controller.counters()))
        self.assertNotIn("Integrity Issues", render_counters({"active_assets": 2}))

    def test_failed_scan_is_logged_and_ignored(self):
        controller = LibraryController(FakeStore(error=OSError("disk")))
        with self.assertLogs("majd.library", "ERROR"):
            controller.start_integrity_scan().join()
        self.assertIsNone(controller.integrity_issues)
        self.assertEqual(controller.counters(), {"active_assets": 2})


if __name__ == "__main__":
    unittest.main()
