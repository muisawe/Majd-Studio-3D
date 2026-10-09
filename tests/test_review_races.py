"""Regressions for historical retries and simultaneous human decisions."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests import test_review_service


class ReviewRaceTests(unittest.TestCase):
    def setUp(self):
        test_review_service.ReviewServiceTests.setUp(self)

    def current_candidate(self):
        return next(candidate for candidate in self.controller.view(self.asset)['candidates']
                    if candidate['is_current'])

    def test_historical_retry_preserves_regenerated_candidates_and_records_result(self):
        original = json.loads(self.store.get_asset(self.asset)['candidates_json'])
        original[0]['generation_tag'] = 'historical'
        self.store.update_asset(self.asset, candidates_json=json.dumps(original))
        historical = self.current_candidate()
        self.controller.select(self.asset, historical['candidate_id'])
        regenerated = self.fixture.root / 'regenerated.glb'
        regenerated.write_text(json.dumps({'faces': 700}))
        current_json = json.dumps([{'candidate': 1, 'score': .1, 'faces': 700,
            'glb': str(regenerated), 'generation_tag': 'current'}])
        self.store.update_asset(self.asset, candidates_json=current_json)
        self.controller.view(self.asset)
        before = {candidate['candidate_id'] for candidate in self.store.list_review_candidates(self.asset)}

        batch_id = self.controller.request_retry(self.asset)
        self.batch.executor.run(batch_id)

        self.assertEqual(self.store.get_asset(self.asset)['candidates_json'], current_json)
        self.assertEqual(json.loads(regenerated.read_text())['faces'], 700)
        item = self.store.list_processing_batch_items(batch_id)[0]
        self.assertEqual(item['status'], 'success')
        result = json.loads(item['result_json'])
        self.assertEqual(result['original_faces'], 1000)
        self.assertFalse(result['review_applied'])
        candidates = self.store.list_review_candidates(self.asset)
        self.assertTrue(before.issubset({candidate['candidate_id'] for candidate in candidates}))
        processed = next(candidate for candidate in candidates
                         if candidate['processing_run_id'] == result['job_id'])
        self.assertEqual(processed['processing_status'], 'success')
        self.assertEqual(json.loads(processed['metadata_json'])['generation_tag'], 'historical')
        self.assertEqual(processed['score'], .9)
        self.assertTrue(Path(json.loads(processed['artifacts_json'])['glb']).is_file())
        self.assertIn('REQUEST_RETRY', [event['action'] for event in self.store.review_history(self.asset)])

    def test_simultaneous_retry_requests_publish_one_queue_and_pinned_folder(self):
        gate = threading.Barrier(2)
        freeze_input = self.fixture.service.freeze_input

        def freeze(*args, **kwargs):
            gate.wait(timeout=5)
            return freeze_input(*args, **kwargs)

        with (patch.object(self.fixture.service, 'freeze_input', side_effect=freeze),
              ThreadPoolExecutor(max_workers=2) as pool):
            ids = list(pool.map(lambda _: self.controller.request_retry(self.asset), range(2)))

        self.assertEqual(ids[0], ids[1])
        batches = self.store.list_processing_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['status'], 'pending')
        self.assertEqual(len(self.store.list_processing_batch_items(ids[0])), 1)
        events = [event for event in self.store.review_history(self.asset)
                  if event['action'] == 'REQUEST_RETRY']
        self.assertEqual(len(events), 1)
        folders = [path for path in (self.single.app_dir / 'processing_batches').iterdir() if path.is_dir()]
        self.assertEqual([folder.name for folder in folders], [ids[0]])

    def test_selection_changed_during_preparation_cannot_approve_mixed_artifacts(self):
        first = self.current_candidate()
        second_source = self.fixture.root / 'second.glb'
        second_source.write_text(json.dumps({'faces': 900}))
        candidates = json.loads(self.store.get_asset(self.asset)['candidates_json'])
        candidates.append({'candidate': 2, 'score': .8, 'faces': 900, 'glb': str(second_source)})
        self.store.update_asset(self.asset, candidates_json=json.dumps(candidates))
        second = next(candidate for candidate in self.controller.view(self.asset)['candidates']
                      if candidate['candidate_number'] == 2)
        self.controller.select(self.asset, first['candidate_id'])
        blend = self.fixture.root / 'first.blend'
        blend.write_bytes(b'first candidate blend')

        def prepare(asset, candidate):
            self.assertEqual(candidate['candidate_id'], first['candidate_id'])
            self.store.select_review_candidate(asset['id'], second['candidate_id'])
            return str(blend), None

        self.controller.service.prepare_approval = prepare
        with self.assertRaises(ValueError):
            self.controller.approve(self.asset)
        self.assertEqual(self.store.list_versions(self.asset), [])
        review = self.store.get_asset_review(self.asset)
        self.assertEqual(review['review_status'], 'NEEDS_REVIEW')
        self.assertEqual(review['selected_candidate_id'], second['candidate_id'])
        self.assertIsNone(review['approved_candidate_id'])
        self.assertEqual(blend.read_bytes(), b'first candidate blend')

    def test_simultaneous_approval_serializes_preparation_and_publishes_one_version(self):
        selected = self.current_candidate()
        self.controller.select(self.asset, selected['candidate_id'])
        start = threading.Barrier(2)
        prepared = threading.Event()
        release = threading.Event()
        overlap = threading.Event()
        guard = threading.Lock()
        active = 0
        calls = []
        blend = self.fixture.root / 'shared.blend'

        def prepare(asset, candidate):
            nonlocal active
            with guard:
                active += 1
                calls.append(candidate['candidate_id'])
                if active > 1:
                    overlap.set()
            prepared.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError('Approval preparation was not released')
                blend.write_bytes(candidate['candidate_id'].encode())
                return str(blend), None
            finally:
                with guard:
                    active -= 1

        def approve():
            start.wait(timeout=5)
            return self.controller.approve(self.asset)

        self.controller.service.prepare_approval = prepare
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve) for _ in range(2)]
            try:
                self.assertTrue(prepared.wait(timeout=5))
                simultaneous = overlap.wait(timeout=.25)
            finally:
                release.set()
            results = [future.result(timeout=5) for future in futures]
        self.assertFalse(simultaneous, 'Shared Blender outputs were prepared concurrently')
        self.assertEqual(calls, [selected['candidate_id']])
        self.assertEqual(results[0]['approved_result_ref'], results[1]['approved_result_ref'])
        versions = self.store.list_versions(self.asset)
        self.assertEqual(len(versions), 1)
        self.assertEqual(Path(versions[0]['blend_path']).read_bytes(), selected['candidate_id'].encode())
        self.assertEqual(len([event for event in self.store.review_history(self.asset)
                              if event['action'] == 'APPROVE']), 1)
