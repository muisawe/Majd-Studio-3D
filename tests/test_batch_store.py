"""Durable queue transactions preserve raw snapshots and completed history."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from majd_studio_3d.store import SCHEMA_VERSION, V9Store


class BatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = V9Store(self.root / 'studio.sqlite3', self.root / 'projects', self.root / 'library')
        self.project = self.store.list_projects()[0]['id']
        self.assets = [self.store.create_asset({'project_id': self.project, 'name': f'Asset {n}', 'asset_type': 'Prop'}) for n in range(4)]
        self.snapshot = {'decimate_ratio': .3, 'timeout_seconds': 60}

    def items(self, count=4):
        return [{'asset_id': self.assets[n], 'asset_name': f'Asset {n}', 'engine': 'hunyuan21',
                 'raw_sha256': f'hash{n}', 'raw_source': str(self.root / f'snapshot{n}.glb'),
                 'raw_asset': str(self.root / f'raw{n}.glb'), 'source_input': str(self.root / f'input{n}.glb'),
                 'expected_candidates_json': [{'glb': 'raw'}], 'candidate_index': 0, 'candidate_number': 1}
                for n in range(count)]

    def batch(self, count=4):
        return self.store.create_processing_batch(self.project, self.snapshot, self.items(count))

    def claim(self, batch):
        self.assertTrue(self.store.claim_processing_batch(batch, 'owner', 123))
        return self.store.claim_next_processing_item(batch, 'owner')

    def test_selection_deduplicates_and_configuration_stays_immutable(self):
        items = self.items(2)
        batch = self.store.create_processing_batch(self.project, self.snapshot, items + items)
        self.snapshot['decimate_ratio'] = .8
        rows = self.store.list_processing_batch_items(batch)
        self.assertEqual([r['asset_id'] for r in rows], self.assets[:2])
        self.assertEqual([r['ordinal'] for r in rows], [0, 1])
        self.assertEqual(json.loads(self.store.get_processing_batch(batch)['config_snapshot_json'])['decimate_ratio'], .3)
        self.assertEqual(json.loads(rows[0]['expected_candidates_json']), [{'glb': 'raw'}])
        self.assertEqual(rows[0]['raw_source'], items[0]['raw_source'])

    def test_atomic_claim_blocks_duplicate_workers_and_other_batches(self):
        batch, other = self.batch(2), self.batch(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda token: self.store.claim_processing_batch(batch, token, 123), ['a', 'b']))
        self.assertEqual(sum(results), 1)
        owner = self.store.get_processing_batch(batch)['owner_token']
        self.assertFalse(self.store.claim_processing_batch(other, 'c', 123))
        self.assertIsNone(self.store.claim_next_processing_item(batch, 'wrong'))
        row = self.store.claim_next_processing_item(batch, owner)
        self.assertEqual(row['ordinal'], 0)
        self.assertIsNone(self.store.claim_next_processing_item(batch, owner))
        self.assertFalse(self.store.release_processing_batch(batch, owner))

    def test_terminal_accounting_reused_and_failure(self):
        batch = self.batch()
        self.claim(batch)
        for n, status in enumerate(['success', 'reused', 'failed', 'cancelled']):
            row = self.store.list_processing_batch_items(batch)[n]
            if n:
                row = self.store.claim_next_processing_item(batch, 'owner')
            self.assertTrue(self.store.update_processing_item_stage(row['id'], 'cleaning', 'live-run'))
            self.assertTrue(self.store.finish_processing_batch_item(row['id'], status, result={'status': status}, error='failure' if status == 'failed' else None))
            self.assertFalse(self.store.finish_processing_batch_item(row['id'], 'success'))
        parent = self.store.get_processing_batch(batch)
        self.assertEqual((parent['completed_count'], parent['reused_count'], parent['failed_count'], parent['cancelled_count']), (2, 1, 1, 1))
        self.assertEqual(parent['status'], 'partially_failed')
        self.assertTrue(self.store.release_processing_batch(batch, 'owner'))
        self.assertTrue(self.store.hide_processing_batch(batch))
        self.assertEqual(self.store.list_processing_batches(self.project), [])
        self.assertEqual(len(self.store.list_processing_batches(self.project, include_hidden=True)), 1)
        self.assertEqual(len(self.store.list_processing_batch_items(batch)), 4)

    def test_item_cancel_and_late_success_race(self):
        batch = self.batch(2)
        active = self.claim(batch)
        queued = self.store.list_processing_batch_items(batch)[1]
        self.assertTrue(self.store.cancel_processing_batch_item(queued['id']))
        self.assertEqual(self.store.get_processing_batch_item(queued['id'])['status'], 'cancelled')
        self.assertTrue(self.store.cancel_processing_batch_item(active['id']))
        self.assertEqual(self.store.get_processing_batch_item(active['id'])['status'], 'processing')
        self.store.finish_processing_batch_item(active['id'], 'success', result={'processed_asset': 'partial.glb'})
        cancelled = self.store.get_processing_batch_item(active['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(cancelled['result_json'])
        self.assertEqual(self.store.get_processing_batch(batch)['cancelled_count'], 2)
        self.assertFalse(self.store.cancel_processing_batch_item(active['id']))

    def test_whole_batch_cancellation_keeps_completed_results_and_lease(self):
        batch = self.batch(3)
        first = self.claim(batch)
        self.store.finish_processing_batch_item(first['id'], 'success', result={'processed_asset': 'good.glb'})
        active = self.store.claim_next_processing_item(batch, 'owner')
        self.assertTrue(self.store.cancel_processing_batch(batch))
        parent = self.store.get_processing_batch(batch)
        self.assertEqual(parent['status'], 'cancelled')
        self.assertEqual(parent['owner_token'], 'owner')
        self.assertIsNone(self.store.claim_next_processing_item(batch, 'owner'))
        self.store.finish_processing_batch_item(active['id'], 'success')
        self.assertTrue(self.store.release_processing_batch(batch, 'owner'))
        rows = self.store.list_processing_batch_items(batch)
        self.assertEqual([row['status'] for row in rows], ['success', 'cancelled', 'cancelled'])
        self.assertEqual(json.loads(rows[0]['result_json'])['processed_asset'], 'good.glb')

    def test_retry_only_failed_and_increment_once_on_actual_retry(self):
        batch = self.batch(3)
        first = self.claim(batch)
        self.store.finish_processing_batch_item(first['id'], 'success')
        second = self.store.claim_next_processing_item(batch, 'owner')
        self.store.finish_processing_batch_item(second['id'], 'failed', error='timeout')
        self.assertEqual(self.store.retry_processing_batch_items(batch), 0)
        third = self.store.claim_next_processing_item(batch, 'owner')
        self.store.finish_processing_batch_item(third['id'], 'cancelled')
        self.store.release_processing_batch(batch, 'owner')
        self.assertEqual(self.store.retry_processing_batch_items(batch), 1)
        self.assertEqual(self.store.retry_processing_batch_items(batch), 0)
        rows = self.store.list_processing_batch_items(batch)
        self.assertEqual([r['status'] for r in rows], ['success', 'queued', 'cancelled'])
        self.assertEqual([r['retry_count'] for r in rows], [0, 1, 0])
        self.assertEqual(rows[1]['raw_sha256'], 'hash1')
        self.assertIsNone(rows[1]['error_message'])
        self.assertEqual(self.store.get_processing_batch(batch)['status'], 'pending')

    def test_recovery_preserves_terminal_and_queued_marks_active_interrupted(self):
        batch = self.batch(3)
        first = self.claim(batch)
        self.store.finish_processing_batch_item(first['id'], 'success', result={'keep': True})
        active = self.store.claim_next_processing_item(batch, 'owner')
        self.store.update_processing_item_stage(active['id'], 'cleaning', 'run')
        self.assertFalse(self.store.recover_processing_batch(batch, 'other'))
        self.assertTrue(self.store.recover_processing_batch(batch, 'owner'))
        self.assertFalse(self.store.recover_processing_batch(batch, 'owner'))
        rows = self.store.list_processing_batch_items(batch)
        self.assertEqual([r['status'] for r in rows], ['success', 'failed', 'queued'])
        self.assertEqual(rows[1]['stage'], 'interrupted')
        self.assertEqual(rows[1]['retry_count'], 0)
        self.assertIsNone(rows[1]['active_run_id'])
        self.assertIn('preserved raw', rows[1]['error_message'])
        self.assertEqual(self.store.get_processing_batch(batch)['status'], 'interrupted')
        self.assertEqual(json.loads(rows[0]['result_json']), {'keep': True})

    def test_cancelled_recovery_does_not_resume_queued(self):
        batch = self.batch(2)
        self.claim(batch)
        self.store.cancel_processing_batch(batch)
        self.assertTrue(self.store.recover_processing_batch(batch, 'owner'))
        self.assertEqual(self.store.get_processing_batch(batch)['status'], 'cancelled')
        self.assertEqual([r['status'] for r in self.store.list_processing_batch_items(batch)], ['failed', 'cancelled'])
        self.assertFalse(self.store.claim_processing_batch(batch, 'new', 1))

    def test_schema95_backup_is_idempotent_and_preserves_existing_records(self):
        asset_before = dict(self.store.get_asset(self.assets[0]))
        source = self.root / 'raw.glb'
        source.write_bytes(b'raw')
        self.store.create_version(self.assets[0], str(source))
        version_before = dict(self.store.latest_version(self.assets[0]))
        result = {'job_id': 'old-job', 'status': 'failed', 'raw_asset': str(source), 'engine': 'hunyuan21',
                  'pipeline_version': 1, 'face_reducer_status': 'failed', 'cleanup_status': 'not_run',
                  'validation_status': 'not_run', 'duration_seconds': 1, 'processed_asset': str(source),
                  'raw_fallback': True, 'configuration_snapshot': self.snapshot}
        self.store.save_processing_run(asset_id=self.assets[0], result=result, config_snapshot=self.snapshot)
        old_run = dict(self.store.get_processing_run('old-job'))
        asset_before = dict(self.store.get_asset(self.assets[0]))
        with self.store.connect() as conn:
            conn.execute('DROP TABLE processing_batch_items')
            conn.execute('DROP TABLE processing_batches')
            conn.execute("UPDATE meta SET value='95' WHERE key='schema_version'")
        copied = self.root / 'copy.sqlite3'
        with sqlite3.connect(self.store.db_path) as source_db, sqlite3.connect(copied) as target:
            source_db.backup(target)
        for _ in range(2):
            migrated = V9Store(copied, self.root / 'projects', self.root / 'library')
            self.assertEqual(dict(migrated.get_processing_run('old-job')), old_run)
            self.assertEqual(dict(migrated.get_asset(self.assets[0])), asset_before)
            self.assertEqual(dict(migrated.latest_version(self.assets[0])), version_before)
            self.assertEqual(migrated.list_processing_batches(), [])
            with migrated.connect() as conn:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))

    def test_recorded_run_reference_and_snapshot_survive_retry_history(self):
        batch = self.batch(2)
        row = self.claim(batch)
        result = {'job_id': 'linked-run', 'status': 'failed', 'raw_asset': row['raw_asset'],
                  'engine': 'hunyuan21', 'pipeline_version': 1, 'face_reducer_status': 'failed',
                  'cleanup_status': 'not_run', 'validation_status': 'not_run', 'duration_seconds': 1,
                  'processed_asset': row['raw_asset'], 'raw_fallback': True,
                  'configuration_snapshot': self.snapshot}
        self.store.save_processing_run(asset_id=row['asset_id'], result=result, config_snapshot=self.snapshot)
        self.store.finish_processing_batch_item(row['id'], 'failed', result=result,
                                               warnings=['Preserved raw'], processing_run_id='linked-run')
        second = self.store.claim_next_processing_item(batch, 'owner')
        self.store.finish_processing_batch_item(second['id'], 'failed')
        self.store.release_processing_batch(batch, 'owner')
        self.assertEqual(self.store.get_processing_batch_item(row['id'])['processing_run_id'], 'linked-run')
        self.assertEqual(self.store.retry_processing_batch_items(batch, row['id']), 1)
        self.assertEqual(self.store.get_processing_batch_item(second['id'])['status'], 'failed')
        self.assertEqual(dict(self.store.get_processing_run('linked-run'))['config_snapshot_json'],
                         json.dumps(self.snapshot, ensure_ascii=False, sort_keys=True))
        self.assertEqual(json.loads(self.store.get_processing_batch(batch)['config_snapshot_json']), self.snapshot)

    def test_complete_success_requires_release_before_other_batch(self):
        batch, other = self.batch(1), self.batch(1)
        row = self.claim(batch)
        self.assertFalse(self.store.hide_processing_batch(batch))
        self.store.finish_processing_batch_item(row['id'], 'reused', result={'reused': True})
        parent = self.store.get_processing_batch(batch)
        self.assertEqual(parent['status'], 'success')
        self.assertIsNotNone(parent['completed_at'])
        self.assertFalse(self.store.claim_processing_batch(other, 'other', 321))
        self.assertFalse(self.store.release_processing_batch(batch, 'wrong'))
        self.assertTrue(self.store.release_processing_batch(batch, 'owner'))
        self.assertTrue(self.store.claim_processing_batch(other, 'other', 321))
        self.assertFalse(self.store.cancel_processing_batch(batch))

    def test_late_batch_cancel_keeps_verified_success_and_cancels_queued(self):
        batch = self.batch(2)
        row = self.claim(batch)
        self.store.update_processing_item_stage(row['id'], 'success', 'completed-run')
        self.assertTrue(self.store.cancel_processing_batch(batch))
        self.assertEqual(self.store.get_processing_batch_item(row['id'])['cancel_requested'], 0)
        result = {'processed_asset': 'verified.glb', 'status': 'success'}
        self.store.finish_processing_batch_item(row['id'], 'success', result=result)
        finished = self.store.get_processing_batch_item(row['id'])
        self.assertEqual(finished['status'], 'success')
        self.assertEqual(json.loads(finished['result_json']), result)
        self.assertEqual([r['status'] for r in self.store.list_processing_batch_items(batch)], ['success', 'cancelled'])
        parent = self.store.get_processing_batch(batch)
        self.assertEqual((parent['completed_count'], parent['cancelled_count']), (1, 1))
        self.assertEqual(parent['status'], 'cancelled')

    def test_late_item_cancel_preserves_observed_reuse(self):
        batch = self.batch(1)
        row = self.claim(batch)
        self.store.update_processing_item_stage(row['id'], 'reused')
        self.assertFalse(self.store.cancel_processing_batch_item(row['id']))
        self.assertTrue(self.store.finish_processing_batch_item(row['id'], 'reused', result={'reused': True}))
        self.assertEqual(self.store.get_processing_batch_item(row['id'])['status'], 'reused')
        self.assertEqual(self.store.get_processing_batch(batch)['reused_count'], 1)

    def test_recovery_after_success_only_clears_abandoned_lease(self):
        batch = self.batch(1)
        row = self.claim(batch)
        self.store.finish_processing_batch_item(row['id'], 'success', result={'verified': True})
        self.assertTrue(self.store.recover_processing_batch(batch, 'owner'))
        parent = self.store.get_processing_batch(batch)
        self.assertEqual(parent['status'], 'success')
        self.assertEqual(parent['completed_count'], 1)
        self.assertIsNone(parent['owner_token'])
        self.assertEqual(json.loads(self.store.get_processing_batch_item(row['id'])['result_json']), {'verified': True})

    def test_batch_cancel_is_noop_when_only_observed_terminal_remains(self):
        batch = self.batch(1)
        row = self.claim(batch)
        self.store.update_processing_item_stage(row['id'], 'success')
        self.assertFalse(self.store.cancel_processing_batch(batch))
        self.assertEqual(self.store.get_processing_batch(batch)['cancel_requested'], 0)
        self.store.finish_processing_batch_item(row['id'], 'success')
        self.assertEqual(self.store.get_processing_batch(batch)['status'], 'success')

    def test_invalid_processing_run_reference_rejects_terminal_transition(self):
        batch = self.batch(1)
        row = self.claim(batch)
        with self.assertRaisesRegex(ValueError, 'reference'):
            self.store.finish_processing_batch_item(row['id'], 'success', processing_run_id='absent')
        self.assertEqual(self.store.get_processing_batch_item(row['id'])['status'], 'processing')


if __name__ == '__main__':
    unittest.main()
