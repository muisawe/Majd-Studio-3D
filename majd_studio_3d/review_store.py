"""Transactional human decisions and immutable candidate history (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

REVIEW_STATES = ('NEEDS_REVIEW', 'APPROVED', 'REJECTED', 'RETRY_REQUESTED')


def _now():
    from .store import utcnow
    return utcnow()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def initialize_review_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS review_assets (
            asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
            review_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'
                CHECK(review_status IN ('NEEDS_REVIEW','APPROVED','REJECTED','RETRY_REQUESTED')),
            selected_candidate_id TEXT, approved_candidate_id TEXT,
            approved_result_ref TEXT, approved_artifact_path TEXT,
            approval_timestamp TEXT, rejection_timestamp TEXT, rejection_reason TEXT,
            retry_request_timestamp TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_candidates (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            candidate_number INTEGER, rank INTEGER NOT NULL, score REAL,
            processing_run_id TEXT, processing_status TEXT NOT NULL, raw_source TEXT,
            metadata_json TEXT NOT NULL, config_snapshot_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
            validation_json TEXT NOT NULL, artifacts_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_review_candidates_asset ON review_candidates(asset_id,created_at);
        CREATE TABLE IF NOT EXISTS review_events (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            action TEXT NOT NULL, candidate_id TEXT, previous_state TEXT, new_state TEXT NOT NULL,
            reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_review_events_asset ON review_events(asset_id,created_at);
    """)
    # Historical successful processing is eligible for review, never human approval.
    conn.execute("""INSERT OR IGNORE INTO review_assets(asset_id,created_at,updated_at)
        SELECT DISTINCT asset_id,?,? FROM processing_batch_items
        WHERE asset_id IS NOT NULL AND status IN ('success','reused')""", (_now(), _now()))


class ReviewStoreMixin:
    def _ensure_review(self, conn, asset_id):
        if not conn.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone():
            raise ValueError('Asset not found')
        conn.execute('INSERT OR IGNORE INTO review_assets(asset_id,created_at,updated_at) VALUES(?,?,?)',
                     (asset_id, _now(), _now()))
        return conn.execute('SELECT * FROM review_assets WHERE asset_id=?', (asset_id,)).fetchone()

    @staticmethod
    def _review_event(conn, asset_id, action, previous, current, candidate=None, reason=None, metadata=None):
        conn.execute('''INSERT INTO review_events(id,asset_id,action,candidate_id,previous_state,new_state,
            reason,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (uuid.uuid4().hex, asset_id, action, candidate, previous, current, reason, _json(metadata or {}), _now()))

    def sync_review_candidates(self, asset_id, config_snapshot=None, *, candidate_snapshots=None):
        """Append changed candidate snapshots; neither select nor approve ranked results."""
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            asset = conn.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if asset is None:
                raise ValueError('Asset not found')
            if candidate_snapshots is None:
                if asset['status'] == 'processing':
                    return self.list_review_candidates(asset_id)
                try:
                    candidates = json.loads(asset['candidates_json'] or '[]')
                except (ValueError, TypeError):
                    candidates = []
            else:
                candidates = candidate_snapshots
            candidates = [c for c in candidates if isinstance(c, dict)] if isinstance(candidates, list) else []
            if not candidates:
                for item in conn.execute('''SELECT i.*,b.config_snapshot_json FROM processing_batch_items i
                        JOIN processing_batches b ON b.id=i.batch_id WHERE i.asset_id=?
                        AND i.status IN ('success','reused') ORDER BY i.created_at,i.ordinal''', (asset_id,)):
                    try:
                        result = json.loads(item['result_json'] or '{}')
                    except (ValueError, TypeError):
                        result = {}
                    if not isinstance(result, dict):
                        result = {}
                    run = conn.execute('SELECT * FROM processing_runs WHERE id=?', (item['processing_run_id'],)).fetchone()
                    if run:
                        result = json.loads(run['result_json'])
                    result = {**result, 'status': result.get('status') or 'success', 'configuration_snapshot': result.get('configuration_snapshot') or json.loads(item['config_snapshot_json']),
                              'raw_asset': result.get('raw_asset') or item['raw_asset'],
                              'raw_snapshot': result.get('raw_snapshot') or item['raw_source'],
                              'raw_sha256': result.get('raw_sha256') or item['raw_sha256']}
                    try:
                        old_set = json.loads(item['expected_candidates_json'] or '[]')
                    except (ValueError, TypeError):
                        old_set = []
                    old_set = [c for c in old_set if isinstance(c, dict)] if isinstance(old_set, list) else []
                    if old_set:
                        for index, old in enumerate(old_set):
                            if index == (item['candidate_index'] or 0):
                                old = {**old, 'glb': result.get('processed_asset') or old.get('glb') or asset['best_glb'], 'processing': result}
                            if old not in candidates:
                                candidates.append(old)
                    else:
                        candidates.append({'candidate': item['candidate_number'] or 1,
                            'glb': result.get('processed_asset') or asset['best_glb'], 'processing': result})
                if not candidates and asset['best_glb']:
                    candidates = [{'candidate': 1, 'glb': asset['best_glb'], 'score': asset['best_score']}]
                if not candidates:
                    return self.list_review_candidates(asset_id)
            self._ensure_review(conn, asset_id)
            for rank, candidate in enumerate(candidates, 1):
                processing = candidate.get('processing') or {}
                if not isinstance(processing, dict):
                    processing = {}
                run_id = processing.get('job_id')
                run = conn.execute('SELECT * FROM processing_runs WHERE id=?', (run_id,)).fetchone() if run_id else None
                config = processing.get('configuration_snapshot') or config_snapshot
                if config is None:
                    from .cleanup_config import load_cleanup_config
                    config = load_cleanup_config(self.db_path.parent)
                if run:
                    config = json.loads(run['config_snapshot_json'])
                artifact = processing.get('processed_asset') or candidate.get('glb')
                artifacts = {'glb': artifact, 'blend': candidate.get('blend'), 'thumbnail': candidate.get('thumbnail')}
                provenance = {'raw_sha256': processing.get('raw_sha256'), 'config_digest': processing.get('config_digest'),
                              'pipeline_version': processing.get('pipeline_version'), 'engine': processing.get('engine') or asset['engine']}
                # A reused filename with regenerated bytes is a different immutable candidate.
                if artifact and Path(artifact).is_file():
                    digest = hashlib.sha256()
                    with Path(artifact).open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                    provenance['artifact_sha256'] = digest.hexdigest()
                identity_metadata = {k: v for k, v in candidate.items() if k not in {'rank', 'candidate_index'}}
                payload = {'candidate': identity_metadata, 'provenance': provenance, 'config': config if processing else None}
                identity = hashlib.sha256((asset_id + _json(payload)).encode()).hexdigest()
                if conn.execute('SELECT 1 FROM review_candidates WHERE id=?', (identity,)).fetchone():
                    continue
                raw = processing.get('raw_snapshot') or candidate.get('raw_glb') or processing.get('raw_asset') or candidate.get('glb')
                freeze_dir = Path(asset['output_dir']) / 'review_candidates' / identity
                freeze_dir.mkdir(parents=True, exist_ok=True)
                for name, value in list(artifacts.items()) + [('raw', raw)]:
                    if value and Path(value).is_file():
                        frozen = freeze_dir / (name + Path(value).suffix)
                        shutil.copy2(value, frozen)
                        if name == 'raw':
                            raw = str(frozen)
                        else:
                            artifacts[name] = str(frozen)
                if raw and Path(raw).is_file():
                    (freeze_dir / 'raw_source.json').write_text(_json({'kind': 'majd_raw_snapshot', 'version': 1,
                        'raw_asset': processing.get('raw_asset') or candidate.get('raw_glb') or candidate.get('glb'),
                        'raw_snapshot': raw, 'raw_sha256': hashlib.sha256(Path(raw).read_bytes()).hexdigest()}), encoding='utf-8')
                stored_metadata = {**candidate, 'candidate_index': candidate.get('candidate_index', rank - 1)}
                conn.execute('''INSERT OR IGNORE INTO review_candidates(id,asset_id,candidate_number,rank,score,
                    processing_run_id,processing_status,raw_source,metadata_json,config_snapshot_json,
                    provenance_json,warnings_json,validation_json,artifacts_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identity, asset_id, candidate.get('candidate', rank), rank, candidate.get('score'), run_id,
                     processing.get('status') or 'not_run', raw,
                     _json(stored_metadata), _json(config), _json(provenance), _json(processing.get('warnings') or candidate.get('warnings') or []),
                     _json({'status': processing.get('validation_status'), 'details': processing.get('validation')}), _json(artifacts), _now()))
        return self.list_review_candidates(asset_id)

    def get_asset_review(self, asset_id):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM review_assets WHERE asset_id=?', (asset_id,)).fetchone()
            return dict(row) if row else None

    def list_review_candidates(self, asset_id):
        with self.connect() as conn:
            return [{**dict(row), 'candidate_id': row['id']} for row in conn.execute('SELECT * FROM review_candidates WHERE asset_id=? ORDER BY created_at DESC,rowid DESC', (asset_id,))]

    def review_history(self, asset_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM review_events WHERE asset_id=? ORDER BY rowid DESC', (asset_id,))]

    @staticmethod
    def _assert_review_idle(conn, asset_id):
        asset = conn.execute('SELECT status FROM assets WHERE id=?', (asset_id,)).fetchone()
        if (asset and asset['status'] == 'processing') or conn.execute(
                "SELECT 1 FROM processing_batch_items WHERE asset_id=? AND status='processing' LIMIT 1", (asset_id,)).fetchone():
            raise ValueError('Processing is active; wait before changing review decisions')

    def select_review_candidate(self, asset_id, candidate_id):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._ensure_review(conn, asset_id)
            self._assert_review_idle(conn, asset_id)
            if row['review_status'] == 'APPROVED':
                raise ValueError('Approved selection is immutable; request retry before selecting another candidate')
            if candidate_id and not conn.execute('SELECT 1 FROM review_candidates WHERE asset_id=? AND id=?', (asset_id, candidate_id)).fetchone():
                raise ValueError('Candidate does not belong to this asset')
            if row['selected_candidate_id'] != candidate_id:
                conn.execute('UPDATE review_assets SET selected_candidate_id=?,updated_at=? WHERE asset_id=?', (candidate_id, _now(), asset_id))
                self._review_event(conn, asset_id, 'SELECT' if candidate_id else 'CLEAR_SELECTION', row['review_status'], row['review_status'], candidate_id)
        return self.get_asset_review(asset_id)

    def approve_review_candidate(self, asset_id, override_style_lock=False, blend_path=None, thumbnail_path=None, expected_candidate_id=None):
        folder = None
        try:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = self._ensure_review(conn, asset_id)
                self._assert_review_idle(conn, asset_id)
                if expected_candidate_id is not None and row['selected_candidate_id'] != expected_candidate_id:
                    raise ValueError('Candidate selection changed during approval; review the saved selection')
                if row['review_status'] == 'APPROVED':
                    return dict(row)
                if row['review_status'] != 'NEEDS_REVIEW':
                    raise ValueError('Approval requires NEEDS_REVIEW')
                candidate = conn.execute('SELECT * FROM review_candidates WHERE id=? AND asset_id=?', (row['selected_candidate_id'], asset_id)).fetchone()
                if not candidate:
                    raise ValueError('Select a candidate explicitly before approval')
                if candidate['processing_status'] not in {'success', 'not_run'}:
                    raise ValueError('Failed or cancelled processing cannot be approved')
                validation = json.loads(candidate['validation_json'])
                if validation.get('status') in {'failed', 'cancelled'}:
                    raise ValueError('Failed validation cannot be approved')
                artifacts = json.loads(candidate['artifacts_json'])
                if blend_path:
                    artifacts['blend'] = blend_path
                if thumbnail_path:
                    artifacts['thumbnail'] = thumbnail_path
                source = Path(artifacts.get('glb') or '')
                if not source.is_file():
                    raise ValueError('Selected candidate artifact is unavailable')
                provenance = json.loads(candidate['provenance_json'])
                if provenance.get('artifact_sha256') and hashlib.sha256(source.read_bytes()).hexdigest() != provenance['artifact_sha256']:
                    raise ValueError('Selected candidate artifact changed; sync and select the new snapshot')
                metadata = json.loads(candidate['metadata_json'])
                lock, notes = self.style_lock_check(asset_id, metadata)
                if lock == 'FAIL' and not override_style_lock:
                    raise ValueError('Style Lock failed: ' + notes)
                if lock == 'FAIL' and override_style_lock:
                    lock = 'OVERRIDE'
                asset = conn.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
                previous_version = None
                for decision in conn.execute("SELECT metadata_json FROM review_events WHERE asset_id=? AND candidate_id=? AND action='APPROVE' ORDER BY rowid DESC",
                                             (asset_id, candidate['id'])):
                    previous_id = json.loads(decision['metadata_json']).get('version_id')
                    version = conn.execute('SELECT * FROM asset_versions WHERE id=? AND asset_id=?', (previous_id, asset_id)).fetchone()
                    if version and Path(version['glb_path']).is_file():
                        previous_version = version
                        break
                if previous_version:
                    version_id = previous_version['id']
                    number = previous_version['version_number']
                    target = Path(previous_version['glb_path'])
                    blend = Path(previous_version['blend_path']) if previous_version['blend_path'] else None
                else:
                    number = conn.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM asset_versions WHERE asset_id=?', (asset_id,)).fetchone()[0]
                    version_id = uuid.uuid4().hex[:12]
                    folder = Path(asset['output_dir']) / 'versions' / f'approved_{version_id}'
                    folder.mkdir(parents=True, exist_ok=False)
                    target = folder / 'approved.glb'
                    shutil.copy2(source, target)
                    blend = None
                    if artifacts.get('blend') and Path(artifacts['blend']).is_file():
                        blend = folder / 'approved.blend'
                        shutil.copy2(artifacts['blend'], blend)
                    thumbnail = None
                    if artifacts.get('thumbnail') and Path(artifacts['thumbnail']).is_file():
                        thumbnail = folder / 'thumbnail.png'
                        shutil.copy2(artifacts['thumbnail'], thumbnail)
                    manifest = folder / 'manifest.json'
                    manifest.write_text(_json({'asset_id': asset_id, 'version': number, 'candidate_id': candidate['id'],
                                              'candidate_snapshot': dict(candidate), 'created_at': _now()}), encoding='utf-8')
                    conn.execute('''INSERT INTO asset_versions(id,asset_id,version_number,style_id,approved_candidate,
                        score,style_lock_result,style_lock_notes,glb_path,blend_path,thumbnail_path,manifest_path,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (version_id, asset_id, number, asset['style_id'], candidate['candidate_number'],
                        candidate['score'], lock, notes, str(target), str(blend) if blend else None, str(thumbnail) if thumbnail else None, str(manifest), _now()))
                conn.execute("UPDATE assets SET current_version=?,status='completed',best_glb=?,best_blend=?,best_score=?,updated_at=? WHERE id=?",
                             (number, str(target), str(blend) if blend else None, candidate['score'], _now(), asset_id))
                conn.execute("""UPDATE review_assets SET review_status='APPROVED',approved_candidate_id=?,
                    approved_result_ref=?,approved_artifact_path=?,approval_timestamp=?,updated_at=? WHERE asset_id=?""",
                    (candidate['id'], version_id, str(target), _now(), _now(), asset_id))
                self._review_event(conn, asset_id, 'APPROVE', row['review_status'], 'APPROVED', candidate['id'], metadata={'version_id': version_id, 'override_style_lock': override_style_lock, 'reused_version': bool(previous_version)})
        except Exception:
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        publication_warnings = []
        try:
            self._publish_library_copy(asset_id, number)
        except OSError as exc:
            publication_warnings.append('Approval saved; library copy could not be published: ' + str(exc))
            with self.connect() as conn:
                self._review_event(conn, asset_id, 'LIBRARY_WARNING', 'APPROVED', 'APPROVED',
                                   candidate['id'], publication_warnings[0])
        result = self.get_asset_review(asset_id)
        if publication_warnings:
            result['warnings'] = publication_warnings
        return result

    def _review_decision(self, asset_id, state, action, reason=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._ensure_review(conn, asset_id)
            self._assert_review_idle(conn, asset_id)
            if row['review_status'] == state:
                return dict(row)
            if state == 'REJECTED':
                conn.execute("UPDATE review_assets SET review_status=?,rejection_timestamp=?,rejection_reason=?,updated_at=? WHERE asset_id=?",
                             (state, _now(), reason, _now(), asset_id))
            else:
                conn.execute("UPDATE review_assets SET review_status=?,retry_request_timestamp=?,updated_at=? WHERE asset_id=?",
                             (state, _now(), _now(), asset_id))
            self._review_event(conn, asset_id, action, row['review_status'], state, row['selected_candidate_id'], reason)
        return self.get_asset_review(asset_id)

    def reject_asset_review(self, asset_id, reason=None):
        return self._review_decision(asset_id, 'REJECTED', 'REJECT', reason)

    def request_asset_review_retry(self, asset_id, reason=None):
        return self._review_decision(asset_id, 'RETRY_REQUESTED', 'REQUEST_RETRY', reason)

    def complete_asset_review_retry(self, asset_id):
        """A finished retry becomes reviewable without erasing past approved references."""
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._ensure_review(conn, asset_id)
            pending = conn.execute("SELECT 1 FROM processing_batch_items WHERE asset_id=? AND status IN ('queued','processing')", (asset_id,)).fetchone()
            if row['review_status'] == 'RETRY_REQUESTED' and not pending:
                conn.execute("UPDATE review_assets SET review_status='NEEDS_REVIEW',updated_at=? WHERE asset_id=?", (_now(), asset_id))
                self._review_event(conn, asset_id, 'RETRY_COMPLETE', 'RETRY_REQUESTED', 'NEEDS_REVIEW')
        return self.get_asset_review(asset_id)
