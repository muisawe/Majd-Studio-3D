"""Library identities preserve approved outputs without changing review decisions."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from majd_studio_3d.library_store import initialize_library_schema
from majd_studio_3d.store import V9Store


class LibraryFixtureStore(V9Store):
    def init_schema(self):
        super().init_schema()
        with self.connect() as conn:
            initialize_library_schema(conn)


class LibraryStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LibraryFixtureStore(self.root / 'studio.sqlite3', self.root / 'projects', self.root / 'copies')
        self.project = self.store.list_projects()[0]['id']
        self.source, self.candidate, self.approved = self.approve('First')

    def approve(self, name, approve=True):
        source = self.store.create_asset({'project_id': self.project, 'name': name, 'style_lock': False})
        path = self.root / (source + '.glb')
        path.write_bytes(('mesh-' + source).encode())
        self.store.update_asset(source, status='review', candidates_json=json.dumps([{'candidate': 2, 'glb': str(path), 'score': .8}]))
        candidate = self.store.sync_review_candidates(source, {'decimate_ratio': .3})[0]
        self.store.select_review_candidate(source, candidate['id'])
        review = self.store.approve_review_candidate(source) if approve else self.store.get_asset_review(source)
        return source, candidate, review

    def publish(self, **kwargs):
        return self.store.publish_library_result(self.source, display_name='Chair', **kwargs)

    def test_create_first_asset_and_version(self):
        result = self.publish(asset_type='prop', category='Furniture', tags=['wood'])
        asset, version = result['asset'], result['version']
        self.assertFalse(result['reused'])
        self.assertNotEqual(asset['id'], self.source)
        self.assertEqual(asset['display_name'], 'Chair')
        self.assertEqual(asset['current_version_id'], version['id'])
        self.assertEqual(asset['version_count'], 1)
        self.assertEqual(version['version_number'], 1)
        self.assertEqual(version['status'], 'APPROVED')
        self.assertEqual(json.loads(asset['tags_json']), ['wood'])
        self.assertEqual(self.store.library_integrity_issues(), [])

    def test_publication_does_not_copy_heavy_artifacts(self):
        result = self.publish()
        artifacts = json.loads(result['version']['artifacts_json'])
        self.assertEqual(artifacts['glb'], self.approved['approved_artifact_path'])
        self.assertTrue(artifacts['sha256']['glb'])

    def test_repeated_new_asset_and_attach_action_are_idempotent(self):
        first = self.publish()
        repeated = self.publish()
        attached = self.store.publish_library_result(self.source, target_asset_id=first['asset']['id'])
        self.assertTrue(repeated['reused'])
        self.assertTrue(attached['reused'])
        self.assertEqual(first['version'], repeated['version'])
        self.assertEqual(len(self.store.list_library_assets()), 1)
        self.assertEqual(len(self.store.library_history(first['asset']['id'])), 3)

    def test_explicit_other_asset_cannot_duplicate_published_candidate(self):
        first = self.publish()
        source, _, _ = self.approve('Other')
        other = self.store.publish_library_result(source, display_name='Other')
        with self.assertRaisesRegex(ValueError, 'another asset'):
            self.store.publish_library_result(self.source, target_asset_id=other['asset']['id'])
        self.assertEqual(first['asset']['version_count'], 1)

    def test_concurrent_publication_creates_one_identity(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.publish(), range(3)))
        self.assertEqual(len({r['asset']['id'] for r in results}), 1)
        self.assertEqual(len({r['version']['id'] for r in results}), 1)
        self.assertEqual(sum(not r['reused'] for r in results), 1)

    def test_second_version_monotonic_and_current(self):
        first = self.publish()
        source, _, _ = self.approve('Second')
        second = self.store.publish_library_result(source, target_asset_id=first['asset']['id'])
        third_source, _, _ = self.approve('Third')
        third = self.store.publish_library_result(third_source, target_asset_id=first['asset']['id'])
        self.assertEqual(second['version']['version_number'], 2)
        self.assertEqual(third['version']['version_number'], 3)
        self.assertEqual(third['asset']['current_version_id'], third['version']['id'])
        self.assertEqual(self.store.get_library_version(first['version']['id']), first['version'])

    def test_revert_does_not_create_fake_version(self):
        first = self.publish()
        source, _, _ = self.approve('Second')
        self.store.publish_library_result(source, target_asset_id=first['asset']['id'])
        self.store.set_current_library_version(first['asset']['id'], first['version']['id'])
        self.assertEqual(self.store.get_library_asset(first['asset']['id'])['current_version_number'], 1)
        self.assertEqual(len(self.store.list_library_versions(first['asset']['id'])), 2)

    def test_cross_asset_current_pointer_refused_by_api_and_database(self):
        first = self.publish()
        source, _, _ = self.approve('Second')
        second = self.store.publish_library_result(source, display_name='Second')
        with self.assertRaisesRegex(ValueError, 'belong'):
            self.store.set_current_library_version(first['asset']['id'], second['version']['id'])
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as conn:
            conn.execute('UPDATE library_assets SET current_version_id=? WHERE id=?', (second['version']['id'], first['asset']['id']))

    def test_rename_preserves_stable_identity_and_lineage(self):
        first = self.publish()
        renamed = self.store.rename_library_asset(first['asset']['id'], 'Stool')
        self.assertEqual(renamed['stable_name'], first['asset']['stable_name'])
        self.assertEqual(self.store.get_library_version(first['version']['id']), first['version'])
        self.assertEqual(self.store.library_history(renamed['id'])[0]['action'], 'ASSET_RENAMED')

    def test_archive_restore_preserves_versions_and_artifacts(self):
        first = self.publish()
        self.store.archive_library_asset(first['asset']['id'])
        self.assertEqual(self.store.list_library_assets(), [])
        self.assertEqual(len(self.store.list_library_assets(archived=True)), 1)
        self.assertEqual(len(self.store.list_library_versions(first['asset']['id'])), 1)
        self.assertTrue(Path(self.approved['approved_artifact_path']).is_file())
        self.store.archive_library_asset(first['asset']['id'], False)
        self.assertEqual(len(self.store.list_library_assets()), 1)
        self.assertEqual([e['action'] for e in self.store.library_history(first['asset']['id'])][:2], ['RESTORED', 'ARCHIVED'])

    def test_archived_identity_refuses_new_version(self):
        first = self.publish()
        self.store.archive_library_asset(first['asset']['id'])
        source, _, _ = self.approve('Second')
        with self.assertRaisesRegex(ValueError, 'Restore'):
            self.store.publish_library_result(source, target_asset_id=first['asset']['id'])

    def test_unassigned_legacy_approval_stays_accessible(self):
        rows = self.store.list_unassigned_approved_results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_asset_id'], self.source)
        self.assertEqual(rows[0]['approved_result_ref'], self.approved['approved_result_ref'])
        self.assertEqual(self.store.list_library_assets(), [])
        self.publish()
        self.assertEqual(self.store.list_unassigned_approved_results(), [])

    def test_attach_unassigned_result_to_existing_identity(self):
        first = self.publish()
        source, candidate, _ = self.approve('Second')
        self.assertEqual(self.store.list_unassigned_approved_results()[0]['source_asset_id'], source)
        result = self.store.publish_library_result(source, target_asset_id=first['asset']['id'])
        self.assertEqual(result['version']['approved_candidate_id'], candidate['id'])
        self.assertEqual(self.store.list_unassigned_approved_results(), [])

    def test_all_nonapproved_states_cannot_publish_even_with_old_approved_reference(self):
        for state in ('NEEDS_REVIEW', 'REJECTED', 'RETRY_REQUESTED'):
            with self.subTest(state=state):
                with self.store.connect() as conn:
                    conn.execute('UPDATE review_assets SET review_status=? WHERE asset_id=?', (state, self.source))
                with self.assertRaisesRegex(ValueError, 'APPROVED'):
                    self.publish()
        self.assertEqual(self.store.list_library_assets(), [])

    def test_versions_and_history_are_immutable_in_database(self):
        first = self.publish()
        for statement in ('UPDATE library_asset_versions SET config_snapshot_json=\'{}\' WHERE id=?',
                          'DELETE FROM library_asset_versions WHERE id=?'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'), self.store.connect() as conn:
                conn.execute(statement, (first['version']['id'],))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'), self.store.connect() as conn:
            conn.execute('DELETE FROM library_events WHERE asset_id=?', (first['asset']['id'],))

    def test_lineage_preserves_snapshots_after_review_changes(self):
        first = self.publish()
        version = first['version']
        self.assertEqual(version['approved_candidate_id'], self.candidate['id'])
        self.assertEqual(version['source_review_asset_id'], self.source)
        self.assertEqual(version['source_approved_result_ref'], self.approved['approved_result_ref'])
        self.assertEqual(json.loads(version['config_snapshot_json']), {'decimate_ratio': .3})
        self.store.request_asset_review_retry(self.source)
        self.assertEqual(self.store.get_library_version(version['id']), version)
        self.assertEqual(json.loads(version['review_snapshot_json'])['review_status'], 'APPROVED')

    def test_reload_persists_pointer_and_archive(self):
        first = self.publish()
        self.store.archive_library_asset(first['asset']['id'])
        reopened = LibraryFixtureStore(self.store.db_path, self.root / 'projects', self.root / 'copies')
        self.assertEqual(reopened.get_library_asset(first['asset']['id'])['current_version_id'], first['version']['id'])
        self.assertTrue(reopened.get_library_asset(first['asset']['id'])['archived'])

    def test_migration_of_existing_database_copy_is_additive_and_idempotent(self):
        with self.store.connect() as conn:
            conn.executescript('DROP TABLE library_events; DROP TABLE library_asset_versions; DROP TABLE library_assets;')
            conn.execute("UPDATE meta SET value='97' WHERE key='schema_version'")
        copy = self.root / 'copy.sqlite3'
        with sqlite3.connect(self.store.db_path) as source, sqlite3.connect(copy) as target:
            source.backup(target)
        migrated = LibraryFixtureStore(copy, self.root / 'projects', self.root / 'copies')
        migrated.init_schema()
        self.assertEqual(migrated.get_asset_review(self.source)['approved_result_ref'], self.approved['approved_result_ref'])
        self.assertEqual(len(migrated.list_versions(self.source)), 1)
        self.assertEqual(len(migrated.list_unassigned_approved_results()), 1)
        self.assertEqual(migrated.list_library_assets(), [])

    def test_search_filters_and_counters(self):
        first = self.publish(asset_type='prop', category='Furniture', tags=['wood', 'modern'])
        self.assertEqual(len(self.store.list_library_assets(search='CHA')), 1)
        self.assertEqual(len(self.store.list_library_assets(tags=['wood'], min_versions=1, max_versions=1)), 1)
        self.assertEqual(self.store.list_library_assets(asset_type='character'), [])
        self.assertEqual(self.store.list_library_assets(review_status='NEEDS_REVIEW'), [])
        self.assertEqual(self.store.list_library_assets(updated_before='2000-01-01'), [])
        self.assertEqual(len(self.store.list_library_assets(category='Furniture')), 1)
        self.assertEqual(self.store.library_counters(), {'active_assets': 1, 'archived_assets': 0,
            'total_versions': 1, 'unassigned_approved_results': 0, 'recently_updated': 1})
        self.store.archive_library_asset(first['asset']['id'])
        self.assertEqual(self.store.library_counters()['archived_assets'], 1)

    def test_missing_and_changed_approved_artifact_refuse_publication(self):
        path = Path(self.approved['approved_artifact_path'])
        saved = path.read_bytes()
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.publish()
        path.write_bytes(saved)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.publish()
        self.assertEqual(self.store.list_library_assets(), [])

    def test_integrity_reports_missing_and_changed_artifacts(self):
        first = self.publish()
        path = Path(self.approved['approved_artifact_path'])
        path.write_bytes(b'changed')
        self.assertIn('changed_artifact', {i['kind'] for i in self.store.library_integrity_issues()})
        path.unlink()
        self.assertIn('missing_artifact', {i['kind'] for i in self.store.library_integrity_issues()})
        self.assertEqual(len(self.store.list_library_versions(first['asset']['id'])), 1)

    def test_metadata_update_does_not_modify_versions(self):
        first = self.publish()
        updated = self.store.update_library_asset_metadata(first['asset']['id'], tags='wood, wood,blue', metadata={'author': 'User'}, category='Furniture')
        self.assertEqual(json.loads(updated['tags_json']), ['wood', 'blue'])
        self.assertEqual(json.loads(updated['metadata_json']), {'author': 'User'})
        self.assertEqual(self.store.get_library_version(first['version']['id']), first['version'])

    def test_current_source_review_state_filters_without_mutating_version_approval(self):
        first = self.publish()
        self.store.request_asset_review_retry(self.source)
        self.assertEqual(self.store.list_library_assets(review_status='APPROVED'), [])
        self.assertEqual(len(self.store.list_library_assets(review_status='RETRY_REQUESTED')), 1)
        self.store.complete_asset_review_retry(self.source)
        self.assertEqual(len(self.store.list_library_assets(review_status='NEEDS_REVIEW')), 1)
        self.store.reject_asset_review(self.source)
        self.assertEqual(len(self.store.list_library_assets(review_status='REJECTED')), 1)
        self.assertEqual(self.store.get_library_version(first['version']['id']), first['version'])
        with self.assertRaisesRegex(ValueError, 'APPROVED'):
            self.publish()

    def test_processing_and_batch_lineage_are_pinned(self):
        source, _, _ = self.approve('Processed', approve=False)
        raw = self.root / 'raw.glb'
        raw.write_bytes(b'raw-model')
        processed = self.root / 'processed.glb'
        processed.write_bytes(b'processed-model')
        config = {'decimate_ratio': .3, 'target_faces': 300}
        processing = {'job_id': 'test-processing', 'status': 'success', 'engine': '2.1',
            'pipeline_version': 1, 'raw_asset': str(raw), 'raw_snapshot': str(raw),
            'processed_asset': str(processed), 'raw_sha256': 'raw-hash', 'config_digest': 'config-hash',
            'face_reducer_status': 'success', 'cleanup_status': 'success', 'validation_status': 'success',
            'original_faces': 1000, 'requested_face_budget': 300, 'reduced_faces': 300, 'final_faces': 290,
            'duration_seconds': 1, 'configuration_snapshot': config}
        self.store.save_processing_run(self.project, source, result=processing, config_snapshot=config)
        self.store.update_asset(source, candidates_json=json.dumps([{'candidate': 1, 'glb': str(processed), 'processing': processing}]))
        candidate = next(r for r in self.store.sync_review_candidates(source) if r['processing_run_id'])
        batch = self.store.create_processing_batch(self.project, config, [{'asset_id': source,
            'engine': 'hunyuan21', 'raw_sha256': 'raw-hash', 'raw_source': str(raw),
            'raw_asset': str(raw), 'source_input': str(raw)}])
        with self.store.connect() as conn:
            conn.execute("UPDATE processing_batch_items SET processing_run_id='test-processing',status='success' WHERE batch_id=?", (batch,))
        self.store.select_review_candidate(source, candidate['id'])
        self.store.approve_review_candidate(source)
        result = self.store.publish_library_result(source, display_name='Processed asset')
        version = result['version']
        self.assertEqual(version['source_processing_attempt_id'], 'test-processing')
        self.assertEqual(version['source_batch_id'], batch)
        self.assertEqual(version['source_batch_item_id'], self.store.list_processing_batch_items(batch)[0]['id'])
        self.assertEqual(json.loads(version['provenance_json'])['processing_result']['requested_face_budget'], 300)
        self.assertEqual(self.store.library_integrity_issues(), [])

    def test_published_candidate_and_approved_source_cannot_be_deleted(self):
        self.publish()
        for table, identity in (('review_candidates', self.candidate['id']), ('asset_versions', self.approved['approved_result_ref'])):
            with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as conn:
                conn.execute('DELETE FROM ' + table + ' WHERE id=?', (identity,))

    def test_direct_insert_requires_current_approval(self):
        first = self.publish()
        self.store.request_asset_review_retry(self.source)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'APPROVED'), self.store.connect() as conn:
            conn.execute('''INSERT INTO library_asset_versions SELECT 'new-id',asset_id,2,created_at,
                source_review_asset_id,source_review_decision_id,approved_candidate_id,source_processing_attempt_id,
                source_batch_id,source_batch_item_id,source_approved_result_ref,provenance_json,config_snapshot_json,
                candidate_metadata_json,review_snapshot_json,warnings_json,validation_json,artifacts_json,
                approval_timestamp,status FROM library_asset_versions WHERE id=?''', (first['version']['id'],))

    def test_current_pointer_cannot_be_cleared_with_version_history(self):
        first = self.publish()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'retain'), self.store.connect() as conn:
            conn.execute('UPDATE library_assets SET current_version_id=NULL WHERE id=?', (first['asset']['id'],))

    def test_direct_duplicate_version_number_is_rejected(self):
        first = self.publish()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'monotonic'), self.store.connect() as conn:
            conn.execute('''INSERT INTO library_asset_versions SELECT 'new-id',asset_id,version_number,created_at,
                source_review_asset_id,source_review_decision_id,approved_candidate_id,source_processing_attempt_id,
                source_batch_id,source_batch_item_id,source_approved_result_ref,provenance_json,config_snapshot_json,
                candidate_metadata_json,review_snapshot_json,warnings_json,validation_json,artifacts_json,
                approval_timestamp,status FROM library_asset_versions WHERE id=?''', (first['version']['id'],))

    def test_invalid_identity_and_name_do_not_create_or_change_data(self):
        with self.assertRaisesRegex(ValueError, 'display name'):
            self.store.publish_library_result(self.source)
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.store.publish_library_result(self.source, target_asset_id='missing')
        first = self.publish()
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.store.rename_library_asset(first['asset']['id'], '')
        self.assertEqual(self.store.get_library_asset(first['asset']['id'])['display_name'], 'Chair')
