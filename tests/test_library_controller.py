"""Publication eligibility, durable navigation, filtering and reload behavior."""
import json
import unittest
from pathlib import Path

from majd_studio_3d.library_controller import LibraryController
from majd_studio_3d.store import V9Store
from tests import test_review_service


class LibraryControllerTests(unittest.TestCase):
    def setUp(self):
        test_review_service.ReviewServiceTests.setUp(self)
        self.source = self.asset
        self.review = self.controller
        selected = self.review.view(self.source)['candidates'][0]['candidate_id']
        self.review.select(self.source, selected)
        self.review.approve(self.source)
        self.library = LibraryController(self.store)

    def create(self):
        return self.library.create(self.source, 'Chair', 'Prop', 'Furniture', 'wood, chair,wood')

    def test_unassigned_flow_and_no_automatic_asset_creation(self):
        self.assertEqual(self.library.list(), [])
        self.assertEqual(self.library.unassigned()[0]['source_asset_id'], self.source)
        result = self.create()
        self.assertEqual(result['version']['version_number'], 1)
        self.assertEqual(self.library.unassigned(), [])
        self.assertEqual(self.library.list()[0]['tags'], ['wood', 'chair'])
        self.assertEqual(self.library.counters()['total_versions'], 1)

    def test_immutable_artifact_reference_and_source_review(self):
        result = self.create()
        aid = result['asset']['id']
        view = self.library.view(aid)
        approved = self.store.get_asset_review(self.source)
        self.assertEqual(view['version']['artifacts']['glb_path'], approved['approved_artifact_path'])
        self.assertTrue(view['artifact_available'])
        self.assertEqual(self.library.source_review(aid), self.source)
        self.assertEqual(view['lineage']['approved_candidate_id'], approved['approved_candidate_id'])
        with self.assertRaisesRegex(ValueError, 'without a persisted batch'):
            self.library.source_batch(aid)

    def test_archive_restore_rename_and_reload_preserve_identity(self):
        result = self.create()
        aid = result['asset']['id']
        before = self.library.view(aid)['version']
        self.library.rename(aid, 'Oak Chair')
        self.library.archive(aid)
        self.assertEqual(self.library.list(), [])
        self.assertEqual(self.library.list({'archived': 'archived'})[0]['id'], aid)
        self.library.restore(aid)
        reopened = V9Store(self.store.db_path, self.store.projects_root, self.store.library_root)
        view = LibraryController(reopened).view(aid)
        self.assertEqual(view['asset']['display_name'], 'Oak Chair')
        self.assertEqual(view['version']['id'], before['id'])
        self.assertEqual(view['version']['artifacts'], before['artifacts'])

    def test_filters_and_metadata_update_are_separate_from_versions(self):
        aid = self.create()['asset']['id']
        self.library.metadata(aid, 'Furniture', 'Seating', 'oak,interior')
        matching = {'query': 'CHAIR', 'asset_type': 'Furniture', 'tags': 'oak', 'min_versions': 1, 'max_versions': 1}
        self.assertEqual(len(self.library.list(matching)), 1)
        self.assertEqual(self.library.list({'query': 'table'}), [])
        self.assertEqual(self.library.list({'tags': 'wood'}), [])
        self.assertEqual(self.library.view(aid)['version']['candidate_snapshot'], self.library.view(aid)['versions'][0]['candidate_snapshot'])
        with self.assertRaisesRegex(ValueError, 'YYYY-MM-DD'):
            self.library.list({'updated_after': 'yesterday'})

    def test_nonapproved_results_cannot_publish(self):
        for state in ('NEEDS_REVIEW', 'REJECTED', 'RETRY_REQUESTED'):
            with self.store.connect() as conn:
                conn.execute('UPDATE review_assets SET review_status=? WHERE asset_id=?', (state, self.source))
            with self.assertRaises(ValueError):
                self.create()
        self.assertEqual(self.library.counters()['total_versions'], 0)

    def test_version_switch_is_read_only_and_cross_asset_version_is_rejected(self):
        result = self.create()
        aid, vid = result['asset']['id'], result['version']['id']
        self.assertEqual(self.library.view(aid, vid)['version']['id'], vid)
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.library.view(aid, 'another-version')
        self.assertEqual(self.library.view(aid)['asset']['current_version_id'], vid)

    def test_batch_lineage_survives_archiving_and_reload(self):
        # Produce another explicitly approved candidate through the existing batch.
        self.store.request_asset_review_retry(self.source)
        self.store.complete_asset_review_retry(self.source)
        candidate = json.loads(self.store.get_asset(self.source)['candidates_json'])[0]
        raw = Path(candidate['glb'])
        raw.write_text(json.dumps({'faces': 1500}))
        batch_id = self.batch.create(self.fixture.project, [self.source])
        self.batch.executor.run(batch_id)
        current = next(c for c in self.review.view(self.source)['candidates'] if c['is_current'])
        self.review.select(self.source, current['candidate_id'])
        self.review.approve(self.source)
        result = self.create()
        aid = result['asset']['id']
        self.assertEqual(self.library.source_batch(aid), batch_id)
        self.assertEqual(self.library.view(aid)['version']['source_batch_item_id'], self.store.list_processing_batch_items(batch_id)[0]['id'])
        self.library.archive(aid)
        self.assertEqual(self.library.source_batch(aid), batch_id)
