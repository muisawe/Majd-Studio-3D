"""Review decisions never turn ranking into approval or alter candidate history."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.review_store import initialize_review_schema
from tests.sqlite_helpers import connect
from majd_studio_3d.store import V9Store


class ReviewFixtureStore(V9Store):
    def init_schema(self):
        super().init_schema()
        with self.connect() as conn:
            initialize_review_schema(conn)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = ReviewFixtureStore(self.root / 'studio.sqlite3', self.root / 'projects', self.root / 'library')
        self.project = self.store.list_projects()[0]['id']
        self.asset = self.store.create_asset({'project_id': self.project, 'name': 'Review asset', 'style_lock': False})
        self.items = []
        for number in range(1, 4):
            file = self.root / f'candidate{number}.glb'
            file.write_bytes(f'model-{number}'.encode())
            self.items.append({'candidate': number, 'score': 1 - number / 10, 'glb': str(file), 'faces': 300})
        self.store.update_asset(self.asset, status='review', candidates_json=json.dumps(self.items))
        self.rows = self.store.sync_review_candidates(self.asset, {'decimate_ratio': .3})
        self.rank2 = next(c for c in self.rows if c['rank'] == 2)

    def test_no_automatic_selection_or_approval(self):
        row = self.store.get_asset_review(self.asset)
        self.assertEqual(row['review_status'], 'NEEDS_REVIEW')
        self.assertIsNone(row['selected_candidate_id'])
        self.assertIsNone(row['approved_candidate_id'])
        with self.assertRaisesRegex(ValueError, 'explicitly'):
            self.store.approve_review_candidate(self.asset)
        self.assertEqual(self.store.list_versions(self.asset), [])

    def test_selection_is_independent_and_persists_after_reload(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        reopened = ReviewFixtureStore(self.store.db_path, self.root / 'projects', self.root / 'library')
        row = reopened.get_asset_review(self.asset)
        self.assertEqual(row['selected_candidate_id'], self.rank2['id'])
        self.assertEqual(row['review_status'], 'NEEDS_REVIEW')
        self.store.select_review_candidate(self.asset, None)
        self.assertIsNone(self.store.get_asset_review(self.asset)['selected_candidate_id'])
        self.assertEqual(len(self.store.review_history(self.asset)), 2)

    def test_approve_rank2_idempotent_preserves_all_candidates(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        first = self.store.approve_review_candidate(self.asset)
        second = self.store.approve_review_candidate(self.asset)
        self.assertEqual(first, second)
        self.assertEqual(first['approved_candidate_id'], self.rank2['id'])
        self.assertEqual(Path(first['approved_artifact_path']).read_bytes(), b'model-2')
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 3)
        self.assertEqual([e['action'] for e in self.store.review_history(self.asset)], ['APPROVE', 'SELECT'])
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.select_review_candidate(self.asset, None)

    def test_concurrent_approvals_create_only_one_version(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.store.approve_review_candidate(self.asset), range(2)))
        self.assertEqual(results[0]['approved_result_ref'], results[1]['approved_result_ref'])
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)

    def test_rejection_preserves_history_candidates_and_raw(self):
        self.store.reject_asset_review(self.asset, 'Wrong proportions')
        row = self.store.get_asset_review(self.asset)
        self.assertEqual(row['review_status'], 'REJECTED')
        self.assertEqual(row['rejection_reason'], 'Wrong proportions')
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 3)
        self.assertTrue(Path(self.rank2['raw_source']).is_file())
        self.assertEqual(self.store.review_history(self.asset)[0]['reason'], 'Wrong proportions')

    def test_retry_preserves_approved_version_and_old_decisions(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        approved = self.store.approve_review_candidate(self.asset)
        requested = self.store.request_asset_review_retry(self.asset, 'More detail')
        self.assertEqual(requested['review_status'], 'RETRY_REQUESTED')
        self.assertEqual(requested['approved_result_ref'], approved['approved_result_ref'])
        self.assertEqual(Path(requested['approved_artifact_path']).read_bytes(), b'model-2')
        self.store.complete_asset_review_retry(self.asset)
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'NEEDS_REVIEW')
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 3)
        self.assertEqual(len(self.store.review_history(self.asset)), 4)

    def test_snapshot_artifacts_survive_regenerated_source(self):
        Path(self.items[1]['glb']).write_bytes(b'regenerated')
        self.assertEqual(Path(json.loads(self.rank2['artifacts_json'])['glb']).read_bytes(), b'model-2')
        self.store.sync_review_candidates(self.asset, {'decimate_ratio': .3})
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 4)
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        approved = self.store.approve_review_candidate(self.asset)
        self.assertEqual(Path(approved['approved_artifact_path']).read_bytes(), b'model-2')

    def test_missing_or_tampered_snapshot_cannot_be_approved(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        path = Path(json.loads(self.rank2['artifacts_json'])['glb'])
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.store.approve_review_candidate(self.asset)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.store.approve_review_candidate(self.asset)
        self.assertEqual(self.store.list_versions(self.asset), [])

    def test_failed_processing_is_explicit_and_ineligible(self):
        items = [{**self.items[0], 'processing': {'status': 'failed', 'warnings': ['Cleanup failed'],
                  'configuration_snapshot': {'decimate_ratio': .5}, 'validation_status': 'not_run'}}]
        self.store.update_asset(self.asset, candidates_json=json.dumps(items))
        failed = next(row for row in self.store.sync_review_candidates(self.asset) if row['processing_status'] == 'failed')
        self.assertEqual(json.loads(failed['config_snapshot_json'])['decimate_ratio'], .5)
        self.assertEqual(json.loads(failed['warnings_json']), ['Cleanup failed'])
        self.store.select_review_candidate(self.asset, failed['id'])
        with self.assertRaisesRegex(ValueError, 'Failed'):
            self.store.approve_review_candidate(self.asset)

    def test_duplicate_sync_does_not_add_or_select_records(self):
        self.store.sync_review_candidates(self.asset, {'decimate_ratio': .3})
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 3)
        self.assertEqual(self.store.review_history(self.asset), [])
        self.assertIsNone(self.store.get_asset_review(self.asset)['selected_candidate_id'])

    def test_cross_asset_selection_is_rejected(self):
        other = self.store.create_asset({'project_id': self.project, 'name': 'Other'})
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.store.select_review_candidate(other, self.rank2['id'])
        self.assertIsNone(self.store.get_asset_review(other))

    def test_copy_failure_rolls_back_version_and_decision(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        with patch('majd_studio_3d.review_store.shutil.copy2', side_effect=OSError('Disk full')), self.assertRaisesRegex(OSError, 'Disk full'):
            self.store.approve_review_candidate(self.asset)
        self.assertEqual(self.store.list_versions(self.asset), [])
        self.assertEqual(self.store.get_asset_review(self.asset)['review_status'], 'NEEDS_REVIEW')
        self.assertEqual([e['action'] for e in self.store.review_history(self.asset)], ['SELECT'])
        folders = list((Path(self.store.get_asset(self.asset)['output_dir']) / 'versions').glob('approved_*'))
        self.assertEqual(folders, [])

    def test_legacy_schema_copy_backfill_is_idempotent(self):
        legacy = V9Store(self.root / 'legacy.sqlite3', self.root / 'legacy_projects', self.root / 'legacy_library')
        project = legacy.list_projects()[0]['id']
        asset = legacy.create_asset({'project_id': project, 'name': 'Legacy'})
        batch = legacy.create_processing_batch(project, {}, [{'asset_id': asset, 'engine': 'hunyuan21',
            'raw_sha256': 'hash', 'raw_source': 'raw.glb', 'raw_asset': 'raw.glb', 'source_input': 'raw.glb'}])
        with legacy.connect() as conn:
            conn.execute("UPDATE processing_batch_items SET status='success' WHERE batch_id=?", (batch,))
            conn.execute('DROP TABLE IF EXISTS review_events')
            conn.execute('DROP TABLE IF EXISTS review_candidates')
            conn.execute('DROP TABLE IF EXISTS review_assets')
            conn.execute("UPDATE meta SET value='96' WHERE key='schema_version'")
        copy = self.root / 'legacy-copy.sqlite3'
        with connect(legacy.db_path) as source, connect(copy) as destination:
            source.backup(destination)
        migrated = ReviewFixtureStore(copy, self.root / 'copy_projects', self.root / 'copy_library')
        migrated.init_schema()
        row = migrated.get_asset_review(asset)
        self.assertEqual(row['review_status'], 'NEEDS_REVIEW')
        self.assertIsNone(row['selected_candidate_id'])
        self.assertEqual(migrated.list_processing_batch_items(batch)[0]['status'], 'success')
        self.assertEqual(migrated.review_history(asset), [])

    def test_configuration_changes_do_not_replace_registered_raw_snapshot(self):
        self.store.sync_review_candidates(self.asset, {'decimate_ratio': .9})
        self.assertEqual(len(self.store.list_review_candidates(self.asset)), 3)
        self.assertEqual(json.loads(self.rank2['config_snapshot_json']), {'decimate_ratio': .3})

    def test_review_writes_refuse_active_processing(self):
        self.store.update_asset(self.asset, status='processing')
        for action in (lambda: self.store.select_review_candidate(self.asset, self.rank2['id']),
                       lambda: self.store.approve_review_candidate(self.asset),
                       lambda: self.store.reject_asset_review(self.asset),
                       lambda: self.store.request_asset_review_retry(self.asset)):
            with self.assertRaisesRegex(ValueError, 'Processing is active'):
                action()
        self.assertEqual(self.store.review_history(self.asset), [])

    def test_raw_snapshot_marker_keeps_canonical_provenance(self):
        raw = Path(self.rank2['raw_source'])
        marker = json.loads((raw.parent / 'raw_source.json').read_text())
        self.assertEqual(marker['kind'], 'majd_raw_snapshot')
        self.assertEqual(marker['raw_asset'], self.items[1]['glb'])
        self.assertEqual(marker['raw_snapshot'], str(raw))

    def test_failed_validation_is_not_eligible_for_approval(self):
        candidate = {**self.items[0], 'processing': {'status': 'success', 'validation_status': 'failed'}}
        self.store.update_asset(self.asset, candidates_json=json.dumps([candidate]))
        row = next(r for r in self.store.sync_review_candidates(self.asset) if r['processing_status'] == 'success')
        self.store.select_review_candidate(self.asset, row['id'])
        with self.assertRaisesRegex(ValueError, 'validation'):
            self.store.approve_review_candidate(self.asset)

    def test_library_publish_failure_does_not_report_committed_approval_failed(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        with patch.object(self.store, '_publish_library_copy', side_effect=OSError('Library unavailable')):
            result = self.store.approve_review_candidate(self.asset)
        self.assertEqual(result['review_status'], 'APPROVED')
        self.assertIn('Library unavailable', result['warnings'][0])
        self.assertTrue(Path(result['approved_artifact_path']).is_file())
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)
        warning = self.store.review_history(self.asset)[0]
        self.assertEqual(warning['action'], 'LIBRARY_WARNING')
        self.assertEqual((warning['previous_state'], warning['new_state']), ('APPROVED', 'APPROVED'))
        repeated = self.store.approve_review_candidate(self.asset)
        self.assertEqual(repeated['approved_result_ref'], result['approved_result_ref'])
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)

    def test_reapproval_after_retry_reuses_same_candidate_version(self):
        self.store.select_review_candidate(self.asset, self.rank2['id'])
        first = self.store.approve_review_candidate(self.asset)
        self.store.request_asset_review_retry(self.asset)
        self.store.complete_asset_review_retry(self.asset)
        second = self.store.approve_review_candidate(self.asset)
        self.assertEqual(first['approved_result_ref'], second['approved_result_ref'])
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)
        self.assertTrue(json.loads(self.store.review_history(self.asset)[0]['metadata_json'])['reused_version'])
        self.store.request_asset_review_retry(self.asset)
        self.store.complete_asset_review_retry(self.asset)
        other = next(row for row in self.rows if row['rank'] == 3)
        self.store.select_review_candidate(self.asset, other['id'])
        third = self.store.approve_review_candidate(self.asset)
        self.assertNotEqual(third['approved_result_ref'], first['approved_result_ref'])
        self.assertEqual(len(self.store.list_versions(self.asset)), 2)
        self.assertEqual(Path(first['approved_artifact_path']).read_bytes(), b'model-2')

    def test_legacy_batch_result_without_asset_candidates_is_reviewable(self):
        other = self.store.create_asset({'project_id': self.project, 'name': 'Legacy successful'})
        batch = self.store.create_processing_batch(self.project, {'decimate_ratio': .4}, [{
            'asset_id': other, 'engine': 'hunyuan21', 'raw_sha256': 'rawhash',
            'raw_source': self.items[0]['glb'], 'raw_asset': self.items[0]['glb'],
            'source_input': self.items[0]['glb'], 'candidate_index': 1, 'candidate_number': 2,
            'expected_candidates_json': self.items}])
        with self.store.connect() as conn:
            conn.execute("UPDATE processing_batch_items SET status='success',result_json=? WHERE batch_id=?",
                         (json.dumps({'status': 'success', 'processed_asset': self.items[1]['glb'], 'validation_status': 'success'}), batch))
        rows = self.store.sync_review_candidates(other)
        self.assertEqual(len(rows), 3)
        processed = next(row for row in rows if row['processing_status'] == 'success')
        self.assertEqual(processed['candidate_number'], 2)
        self.assertEqual(json.loads(processed['config_snapshot_json']), {'decimate_ratio': .4})
        self.assertTrue(Path(json.loads(processed['artifacts_json'])['glb']).is_file())
        review = self.store.get_asset_review(other)
        self.assertEqual(review['review_status'], 'NEEDS_REVIEW')
        self.assertIsNone(review['selected_candidate_id'])
        self.assertIsNone(review['approved_result_ref'])

    def test_legacy_best_glb_without_json_has_unselected_candidate(self):
        other = self.store.create_asset({'project_id': self.project, 'name': 'Best-only legacy',
                                         'best_glb': self.items[0]['glb']})
        rows = self.store.sync_review_candidates(other, {'decimate_ratio': .3})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['processing_status'], 'not_run')
        self.assertIsNone(self.store.get_asset_review(other)['selected_candidate_id'])
