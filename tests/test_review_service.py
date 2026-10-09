"""Review rules use persisted candidates and the existing serial executor."""
import json
import unittest

from majd_studio_3d.batch_controller import BatchController
from majd_studio_3d.processing_controller import ProcessingController
from majd_studio_3d.review_controller import ReviewController
from majd_studio_3d.store import V9Store
from tests import test_processing


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_processing.ProcessingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.service.store
        self.asset = self.fixture.asset
        self.store.update_asset(self.asset, status='review', engine='2.1', style_lock=0,
            candidates_json=json.dumps([{'candidate': 1, 'score': .9, 'faces': 1000,
                                        'glb': str(self.fixture.source)}]))
        self.single = ProcessingController(self.fixture.service.app_dir, self.store, self.fixture.service)
        self.batch = BatchController(self.single.app_dir, self.store, self.single)
        self.controller = ReviewController(self.store, self.batch)
        self.controller.initialize()

    def candidate(self):
        return self.controller.view(self.asset)['candidates'][0]['candidate_id']

    def test_no_automatic_rank_one_approval(self):
        review = self.store.get_asset_review(self.asset)
        self.assertEqual(review['review_status'], 'NEEDS_REVIEW')
        self.assertIsNone(review['selected_candidate_id'])
        with self.assertRaisesRegex(ValueError, 'Select a candidate'):
            self.controller.approve(self.asset)
        self.assertEqual(self.store.list_versions(self.asset), [])

    def test_selected_rank_two_approved_and_reload(self):
        raw2 = self.fixture.root / 'second.glb'
        raw2.write_text(json.dumps({'faces': 900}))
        candidates = json.loads(self.store.get_asset(self.asset)['candidates_json'])
        candidates.append({'candidate': 2, 'score': .8, 'faces': 900, 'glb': str(raw2)})
        self.store.update_asset(self.asset, candidates_json=json.dumps(candidates))
        view = self.controller.view(self.asset)
        selected = next(c for c in view['candidates'] if c['rank'] == 2)
        self.controller.select(self.asset, selected['candidate_id'])
        approved = self.controller.approve(self.asset)
        self.assertEqual(approved['approved_candidate_id'], selected['candidate_id'])
        self.assertEqual(self.controller.approve(self.asset)['approved_result_ref'], approved['approved_result_ref'])
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)
        reopened = V9Store(self.store.db_path, self.store.projects_root, self.store.library_root)
        self.assertEqual(reopened.get_asset_review(self.asset)['approved_candidate_id'], selected['candidate_id'])
        self.assertEqual(len(reopened.list_review_candidates(self.asset)), 2)

    def test_bulk_partial_failure(self):
        ids = [self.asset]
        for index in range(2):
            aid = self.store.create_asset({'project_id': self.fixture.project, 'name': f'Bulk {index}', 'engine': '2.1', 'style_lock': 0})
            self.store.update_asset(aid, status='review', candidates_json=self.store.get_asset(self.asset)['candidates_json'])
            self.controller.view(aid)
            ids.append(aid)
        for aid in ids[:2]:
            self.controller.select(aid, self.controller.view(aid)['candidates'][0]['candidate_id'])
        results = self.controller.bulk_approve(ids)
        self.assertEqual([r['status'] for r in results], ['APPROVED', 'APPROVED', 'failed'])
        self.assertIn('explicitly', results[2]['error'])
        self.assertIsNone(self.store.get_asset_review(ids[2])['approved_candidate_id'])

    def test_retry_pins_settings_preserves_candidates_and_uses_executor(self):
        self.single.run(self.asset)
        selected = self.candidate()
        self.controller.select(self.asset, selected)
        before = self.store.list_review_candidates(self.asset)
        batch_id = self.controller.request_retry(self.asset, 'Inspect again')
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'RETRY_REQUESTED')
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), len(before))
        parent = self.store.get_processing_batch(batch_id)
        expected = json.loads(next(c for c in before if c['candidate_id'] == selected)['config_snapshot_json'])
        self.assertEqual(json.loads(parent['config_snapshot_json']), expected)
        self.assertEqual(self.batch.view(batch_id)['counts']['queued'], 1)
        self.batch.executor.run(batch_id)
        self.assertEqual(self.batch.view(batch_id)['counts']['completed'], 1)
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'NEEDS_REVIEW')
        self.assertEqual(self.batch.view(batch_id)['counts']['reused'], 1)

    def test_rejection_and_failed_filter_preserve_artifacts(self):
        candidate = self.candidate()
        self.controller.reject(self.asset, 'Wrong shape')
        self.assertEqual(self.store.get_asset_review(self.asset)['rejection_reason'], 'Wrong shape')
        self.assertEqual(self.candidate(), candidate)
        failed = self.store.create_asset({'project_id': self.fixture.project, 'name': 'No mesh'})
        self.store.update_asset(failed, status='failed')
        self.assertIn(failed, [aid for _, aid in self.controller.choices(self.fixture.project, 'Failed Processing')])
        self.assertEqual(self.controller.counters([self.asset])['rejected'], 1)

    def test_clear_and_selection_persist_before_approval(self):
        self.controller.select(self.asset, self.candidate())
        self.controller.clear(self.asset)
        self.assertIsNone(self.store.get_asset_review(self.asset)['selected_candidate_id'])
        self.assertEqual([r['action'] for r in self.store.review_history(self.asset)], ['CLEAR_SELECTION', 'SELECT'])

    def test_retry_request_is_idempotent_while_queued(self):
        first = self.controller.request_retry(self.asset)
        self.assertEqual(self.controller.request_retry(self.asset), first)
        self.assertEqual(len(self.store.list_processing_batches()), 1)
        self.assertEqual([e['action'] for e in self.store.review_history(self.asset)], ['REQUEST_RETRY'])

    def test_batch_processing_complete_does_not_approve(self):
        batch_id = self.batch.create(self.fixture.project, [self.asset])
        self.batch.executor.run(batch_id)
        view = self.batch.view(batch_id)
        self.assertEqual(view['batch']['status'], 'success')
        self.assertEqual(view['review_counts']['needs_review'], 1)
        self.assertEqual(view['review_counts']['approved'], 0)
        self.assertEqual(self.store.list_versions(self.asset), [])

    def test_retry_approval_preserves_original_approved_version(self):
        self.controller.select(self.asset, self.candidate())
        original = self.controller.approve(self.asset)
        from pathlib import Path
        contents = Path(original['approved_artifact_path']).read_bytes()
        batch_id = self.controller.request_retry(self.asset)
        self.batch.executor.run(batch_id)
        review = self.store.get_asset_review(self.asset)
        self.assertEqual(review['approved_result_ref'], original['approved_result_ref'])
        self.assertEqual(Path(original['approved_artifact_path']).read_bytes(), contents)
        self.assertEqual(review['review_status'], 'NEEDS_REVIEW')
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)
        self.controller.approve(self.asset)
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)

    def test_failed_retry_remains_requested_and_raw_fallback_is_not_approvable(self):
        def fail(*args, **kwargs):
            result = self.fixture.clean(*args, **kwargs)
            result.update(status='failed', error='Mock failure', glb_path=str(args[0]),
                          export_path=None, faces_after=None)
            return result
        self.fixture.cleaner.side_effect = fail
        batch_id = self.controller.request_retry(self.asset)
        self.batch.executor.run(batch_id)
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'RETRY_REQUESTED')
        current = [c for c in self.controller.view(self.asset)['candidates'] if c['is_current']]
        self.assertEqual(current[0]['processing_status'], 'failed')
        self.assertIn(self.asset, [aid for _, aid in self.controller.choices(self.fixture.project, 'Failed Processing')])

    def test_retry_queue_and_review_write_roll_back_together(self):
        from unittest.mock import patch
        with patch.object(self.store, '_review_event', side_effect=RuntimeError('Interrupted decision')), self.assertRaisesRegex(RuntimeError, 'Interrupted decision'):
            self.controller.request_retry(self.asset)
        self.assertEqual(self.store.list_processing_batches(), [])
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'NEEDS_REVIEW')

    def test_library_follows_reapproved_historical_version(self):
        original = self.candidate()
        self.controller.select(self.asset, original)
        first = self.controller.approve(self.asset)
        self.store.request_asset_review_retry(self.asset)
        self.store.complete_asset_review_retry(self.asset)
        self.fixture.source.write_text(json.dumps({'faces': 800}))
        updated = next(c for c in self.controller.view(self.asset)['candidates'] if c['candidate_id'] != original)
        self.controller.select(self.asset, updated['candidate_id'])
        self.controller.approve(self.asset)
        self.store.request_asset_review_retry(self.asset)
        self.store.complete_asset_review_retry(self.asset)
        self.controller.select(self.asset, original)
        self.controller.approve(self.asset)
        self.assertEqual(len(self.store.list_versions(self.asset)), 2)
        self.assertEqual(self.store.latest_version(self.asset)['id'], first['approved_result_ref'])
