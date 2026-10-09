"""Asset presentation preserves identity, version evidence and escaped metadata."""
import unittest

from majd_studio_3d.library_ui import (
    asset_rows,
    history_rows,
    render_counters,
    render_detail,
    render_unassigned,
)


class LibraryPresentationTests(unittest.TestCase):
    def view(self):
        return {"asset": {"id": "a1", "stable_name": "asset-a1", "display_name": "Chair", "asset_type": "Prop", "category": "Furniture", "current_version_number": 2},
                "versions": [{"id": "v1", "version_number": 1}, {"id": "v2", "version_number": 2}],
                "version": {"id": "v1", "version_number": 1, "warnings": ["UV warning"], "configuration_snapshot": {"target_faces": 300}},
                "lineage": {"raw_source": "raw.glb", "source_batch": "batch1", "candidate": "candidate2"},
                "review_history": [{"action": "approved"}], "artifact_available": True}

    def test_version_inspection_does_not_hide_older_versions_or_current_pointer(self):
        html = render_detail(self.view())
        for text in ("Chair", "asset-a1", "v001", "v002", "Current approved version: 2", "Viewing: v001"):
            self.assertIn(text, html)

    def test_lineage_config_review_and_warnings_are_visible(self):
        html = render_detail(self.view())
        for text in ("raw.glb", "batch1", "candidate2", "target_faces", "300", "UV warning", "approved"):
            self.assertIn(text, html)

    def test_missing_artifact_warning_does_not_hide_version_evidence(self):
        view = self.view()
        view["artifact_available"] = False
        html = render_detail(view)
        self.assertIn("Artifact unavailable", html)
        self.assertIn("v001", html)

    def test_names_and_nested_metadata_are_escaped(self):
        view = self.view()
        view["asset"]["display_name"] = "<script>bad()</script>"
        view["version"]["note"] = "<img onerror=x>"
        html = render_detail(view)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_asset_rows_have_separate_review_and_archive_columns(self):
        row = asset_rows([{"display_name": "Chair", "asset_type": "Prop", "current_version_number": 2,
                           "version_count": 2, "review_status": "APPROVED", "tags": ["wood"], "archived": True, "updated_at": "now"}])[0]
        self.assertEqual(row, ["Chair", "Prop", "v002", "2", "APPROVED", "wood", "Yes", "now"])

    def test_counters_are_library_counts_only(self):
        html = render_counters({"active_assets": 2, "archived_assets": 1, "total_versions": 3,
                                "unassigned_approved_results": 4, "recently_updated": 2})
        self.assertIn("Unassigned Approved Results", html)
        self.assertIn("<strong>4</strong>", html)
        self.assertNotIn("Failed", html)

    def test_unassigned_details_include_full_source_metadata(self):
        html = render_unassigned({"item_name": "Chair", "source_batch_id": "b1", "approved_candidate_id": "c2", "provenance": {"raw": "raw.glb"}})
        for text in ("Chair", "b1", "c2", "raw.glb"):
            self.assertIn(text, html)
        self.assertIn("explicitly", render_unassigned(None))

    def test_history_and_empty_detail(self):
        self.assertEqual(history_rows([{"action": "renamed", "created_at": "now", "details": {"new_name": "Chair"}}]),
                         [["renamed", "now", "", '{"new_name": "Chair"}']])
        self.assertIn("Select an asset", render_detail({}))
