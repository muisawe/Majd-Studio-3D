"""Durable production identities and immutable publications of Phase F approvals."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now():
    from .store import utcnow
    return utcnow()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def initialize_library_schema(conn):
    """Add empty library tables. Legacy approvals remain explicitly unassigned."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS library_assets (
            id TEXT PRIMARY KEY, stable_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'object',
            category TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}', archived INTEGER NOT NULL DEFAULT 0
                CHECK(archived IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            current_version_id TEXT REFERENCES library_asset_versions(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS library_asset_versions (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES library_assets(id) ON DELETE RESTRICT,
            version_number INTEGER NOT NULL CHECK(version_number>0), created_at TEXT NOT NULL,
            source_review_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            source_review_decision_id TEXT NOT NULL REFERENCES review_events(id) ON DELETE RESTRICT,
            approved_candidate_id TEXT NOT NULL REFERENCES review_candidates(id) ON DELETE RESTRICT,
            source_processing_attempt_id TEXT REFERENCES processing_runs(id) ON DELETE RESTRICT,
            source_batch_id TEXT REFERENCES processing_batches(id) ON DELETE RESTRICT,
            source_batch_item_id TEXT REFERENCES processing_batch_items(id) ON DELETE RESTRICT,
            source_approved_result_ref TEXT NOT NULL REFERENCES asset_versions(id) ON DELETE RESTRICT,
            provenance_json TEXT NOT NULL, config_snapshot_json TEXT NOT NULL,
            candidate_metadata_json TEXT NOT NULL, review_snapshot_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL, validation_json TEXT NOT NULL, artifacts_json TEXT NOT NULL,
            approval_timestamp TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'APPROVED' CHECK(status='APPROVED'),
            UNIQUE(asset_id,version_number), UNIQUE(approved_candidate_id), UNIQUE(source_approved_result_ref)
        );
        CREATE TABLE IF NOT EXISTS library_events (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES library_assets(id) ON DELETE RESTRICT,
            version_id TEXT REFERENCES library_asset_versions(id) ON DELETE RESTRICT,
            action TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_library_versions_asset ON library_asset_versions(asset_id,version_number);
        CREATE INDEX IF NOT EXISTS idx_library_events_asset ON library_events(asset_id,created_at);
        CREATE TRIGGER IF NOT EXISTS library_current_insert BEFORE INSERT ON library_assets
        WHEN NEW.current_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM library_asset_versions WHERE id=NEW.current_version_id AND asset_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Current version must belong to the asset'); END;
        CREATE TRIGGER IF NOT EXISTS library_current_update BEFORE UPDATE OF current_version_id ON library_assets
        WHEN NEW.current_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM library_asset_versions WHERE id=NEW.current_version_id AND asset_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Current version must belong to the asset'); END;
        CREATE TRIGGER IF NOT EXISTS library_current_clear BEFORE UPDATE OF current_version_id ON library_assets
        WHEN NEW.current_version_id IS NULL AND EXISTS (
            SELECT 1 FROM library_asset_versions WHERE asset_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'An asset with versions must retain a current version'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_immutable_update BEFORE UPDATE ON library_asset_versions
        BEGIN SELECT RAISE(ABORT,'Library versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_immutable_delete BEFORE DELETE ON library_asset_versions
        BEGIN SELECT RAISE(ABORT,'Library versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS library_event_immutable_update BEFORE UPDATE ON library_events
        BEGIN SELECT RAISE(ABORT,'Library history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS library_event_immutable_delete BEFORE DELETE ON library_events
        BEGIN SELECT RAISE(ABORT,'Library history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_sources BEFORE INSERT ON library_asset_versions
        WHEN NOT EXISTS (SELECT 1 FROM review_candidates WHERE id=NEW.approved_candidate_id
                         AND asset_id=NEW.source_review_asset_id)
          OR NOT EXISTS (SELECT 1 FROM asset_versions WHERE id=NEW.source_approved_result_ref
                         AND asset_id=NEW.source_review_asset_id)
          OR NOT EXISTS (SELECT 1 FROM review_events WHERE id=NEW.source_review_decision_id
                         AND asset_id=NEW.source_review_asset_id AND candidate_id=NEW.approved_candidate_id
                         AND action='APPROVE')
        BEGIN SELECT RAISE(ABORT,'Library version source lineage is invalid'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_approval BEFORE INSERT ON library_asset_versions
        WHEN NOT EXISTS (SELECT 1 FROM review_assets WHERE asset_id=NEW.source_review_asset_id
            AND review_status='APPROVED' AND approved_candidate_id=NEW.approved_candidate_id
            AND approved_result_ref=NEW.source_approved_result_ref)
        BEGIN SELECT RAISE(ABORT,'Library publication requires current APPROVED result'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_processing_lineage BEFORE INSERT ON library_asset_versions
        WHEN (NEW.source_processing_attempt_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM processing_runs WHERE id=NEW.source_processing_attempt_id
                AND asset_id=NEW.source_review_asset_id))
          OR (NEW.source_batch_item_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM processing_batch_items WHERE id=NEW.source_batch_item_id
                AND asset_id=NEW.source_review_asset_id AND batch_id=NEW.source_batch_id
                AND processing_run_id=NEW.source_processing_attempt_id))
          OR (NEW.source_batch_id IS NOT NULL AND NEW.source_batch_item_id IS NULL)
        BEGIN SELECT RAISE(ABORT,'Library processing lineage is invalid'); END;
        CREATE TRIGGER IF NOT EXISTS library_version_sequence BEFORE INSERT ON library_asset_versions
        WHEN NEW.version_number != (SELECT COALESCE(MAX(version_number),0)+1
                                    FROM library_asset_versions WHERE asset_id=NEW.asset_id)
        BEGIN SELECT RAISE(ABORT,'Library version numbers must be monotonic'); END;
    """)


class LibraryStoreMixin:
    @staticmethod
    def _library_event(conn, asset_id, action, version_id=None, metadata=None):
        conn.execute('INSERT INTO library_events VALUES(?,?,?,?,?,?)',
                     (uuid.uuid4().hex, asset_id, version_id, action, _json(metadata or {}), _now()))

    @staticmethod
    def _library_asset_record(row):
        return {**dict(row), 'asset_id': row['id']} if row else None

    @staticmethod
    def _library_version_record(row):
        return {**dict(row), 'version_id': row['id']} if row else None

    def get_library_asset(self, asset_id):
        with self.connect() as conn:
            row = conn.execute('''SELECT a.*,COUNT(v.id) AS version_count,
                c.version_number AS current_version_number,r.review_status
                FROM library_assets a LEFT JOIN library_asset_versions v ON v.asset_id=a.id
                LEFT JOIN library_asset_versions c ON c.id=a.current_version_id
                LEFT JOIN review_assets r ON r.asset_id=c.source_review_asset_id
                WHERE a.id=? GROUP BY a.id''', (asset_id,)).fetchone()
            return self._library_asset_record(row)

    def list_library_assets(self, search='', asset_type=None, archived=False, tags=None,
                            min_versions=None, max_versions=None, updated_after=None,
                            updated_before=None, review_status=None, category=None):
        with self.connect() as conn:
            rows = conn.execute('''SELECT a.*,COUNT(v.id) AS version_count,
                c.version_number AS current_version_number,r.review_status
                FROM library_assets a LEFT JOIN library_asset_versions v ON v.asset_id=a.id
                LEFT JOIN library_asset_versions c ON c.id=a.current_version_id
                LEFT JOIN review_assets r ON r.asset_id=c.source_review_asset_id
                GROUP BY a.id ORDER BY a.updated_at DESC,a.rowid DESC''').fetchall()
        wanted_tags = {str(t).strip().casefold() for t in (tags or [])}
        result = []
        for row in rows:
            if archived is not None and bool(row['archived']) != bool(archived):
                continue
            if search and search.casefold() not in row['display_name'].casefold() and search.casefold() not in row['stable_name'].casefold():
                continue
            if asset_type and row['asset_type'] != asset_type:
                continue
            if category and row['category'] != category:
                continue
            if review_status and review_status != row['review_status']:
                continue
            if min_versions is not None and row['version_count'] < int(min_versions):
                continue
            if max_versions is not None and row['version_count'] > int(max_versions):
                continue
            if updated_after and row['updated_at'][:10] < str(updated_after)[:10]:
                continue
            if updated_before and row['updated_at'][:10] > str(updated_before)[:10]:
                continue
            if wanted_tags and not wanted_tags.issubset({str(t).casefold() for t in json.loads(row['tags_json'])}):
                continue
            result.append(self._library_asset_record(row))
        return result

    def get_library_version(self, version_id):
        with self.connect() as conn:
            return self._library_version_record(conn.execute('SELECT * FROM library_asset_versions WHERE id=?', (version_id,)).fetchone())

    def list_library_versions(self, asset_id):
        with self.connect() as conn:
            return [self._library_version_record(row) for row in conn.execute(
                'SELECT * FROM library_asset_versions WHERE asset_id=? ORDER BY version_number DESC', (asset_id,))]

    @staticmethod
    def _approved_source(conn, source_asset_id):
        review = conn.execute('SELECT * FROM review_assets WHERE asset_id=?', (source_asset_id,)).fetchone()
        if not review or review['review_status'] != 'APPROVED':
            raise ValueError('Only an APPROVED Phase F result may be published')
        candidate = conn.execute('SELECT * FROM review_candidates WHERE id=? AND asset_id=?',
                                 (review['approved_candidate_id'], source_asset_id)).fetchone()
        approved = conn.execute('SELECT * FROM asset_versions WHERE id=? AND asset_id=?',
                                (review['approved_result_ref'], source_asset_id)).fetchone()
        decision = None
        for event in conn.execute("SELECT * FROM review_events WHERE asset_id=? AND candidate_id=? AND action='APPROVE' ORDER BY rowid DESC",
                                  (source_asset_id, review['approved_candidate_id'])):
            if json.loads(event['metadata_json']).get('version_id') == review['approved_result_ref']:
                decision = event
                break
        if not candidate or not approved or not decision:
            raise ValueError('Approved source lineage is incomplete')
        return review, candidate, approved, decision

    @staticmethod
    def _source_batch(conn, source_asset_id, candidate):
        if candidate['processing_run_id']:
            return conn.execute('''SELECT * FROM processing_batch_items WHERE asset_id=?
                AND processing_run_id=? ORDER BY rowid DESC LIMIT 1''',
                (source_asset_id, candidate['processing_run_id'])).fetchone()
        # Legacy candidates have no processing attempt. Do not infer a batch by name or rank.
        return None

    def list_unassigned_approved_results(self):
        with self.connect() as conn:
            rows = conn.execute('''SELECT r.*,a.name AS item_name,a.project_id FROM review_assets r
                JOIN assets a ON a.id=r.asset_id WHERE r.review_status='APPROVED'
                AND NOT EXISTS (SELECT 1 FROM library_asset_versions v
                    WHERE v.source_approved_result_ref=r.approved_result_ref OR v.approved_candidate_id=r.approved_candidate_id)
                ORDER BY r.approval_timestamp DESC''').fetchall()
            result = []
            for row in rows:
                try:
                    _, candidate, approved, decision = self._approved_source(conn, row['asset_id'])
                except ValueError:
                    continue
                batch = self._source_batch(conn, row['asset_id'], candidate)
                result.append({**dict(row), 'source_asset_id': row['asset_id'],
                    'source_review_decision_id': decision['id'], 'source_batch_id': batch['batch_id'] if batch else None,
                    'source_batch_item_id': batch['id'] if batch else None,
                    'config_snapshot_json': candidate['config_snapshot_json'],
                    'candidate_metadata_json': candidate['metadata_json'], 'provenance_json': candidate['provenance_json'],
                    'artifacts_json': _json({'glb': approved['glb_path'], 'blend': approved['blend_path'],
                                            'thumbnail': approved['thumbnail_path'], 'manifest': approved['manifest_path']})})
            return result

    def publish_library_result(self, source_asset_id, target_asset_id=None, display_name=None,
                               asset_type=None, category='', tags=None, expected_approved_result_ref=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            review, candidate, approved, decision = self._approved_source(conn, source_asset_id)
            if expected_approved_result_ref is not None and review['approved_result_ref'] != expected_approved_result_ref:
                raise ValueError('Approved result changed; refresh and select the intended approval again')
            existing = conn.execute('''SELECT * FROM library_asset_versions
                WHERE source_approved_result_ref=? OR approved_candidate_id=?''',
                (review['approved_result_ref'], candidate['id'])).fetchone()
            if existing:
                if target_asset_id and target_asset_id != existing['asset_id']:
                    raise ValueError('Approved result is already published to another asset: ' + existing['asset_id'])
                return {'asset': self.get_library_asset(existing['asset_id']),
                        'version': self._library_version_record(existing), 'reused': True}
            original_artifacts = {'glb': approved['glb_path'], 'blend': approved['blend_path'],
                                  'thumbnail': approved['thumbnail_path'], 'manifest': approved['manifest_path']}
            main_path = Path(approved['glb_path'])
            if not main_path.is_file():
                raise ValueError('Approved artifact is unavailable')
            provenance = json.loads(candidate['provenance_json'])
            main_hash = _digest(main_path)
            expected = provenance.get('artifact_sha256')
            if expected and main_hash != expected:
                raise ValueError('Approved artifact changed; publication refused')
            hashes = {key: _digest(value) for key, value in original_artifacts.items() if value and Path(value).is_file()}
            artifacts = {**original_artifacts, 'sha256': hashes}
            if target_asset_id:
                target = conn.execute('SELECT * FROM library_assets WHERE id=?', (target_asset_id,)).fetchone()
                if not target:
                    raise ValueError('Library asset not found')
                if target['archived']:
                    raise ValueError('Restore archived asset before adding a version')
                asset_id = target_asset_id
            else:
                name = str(display_name or '').strip()
                if not name:
                    raise ValueError('A display name is required for a new asset')
                source = conn.execute('SELECT * FROM assets WHERE id=?', (source_asset_id,)).fetchone()
                asset_id = uuid.uuid4().hex
                timestamp = _now()
                conn.execute('''INSERT INTO library_assets(id,stable_name,display_name,asset_type,category,
                    tags_json,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (asset_id, 'asset_' + asset_id, name, str(asset_type or source['asset_type'] or 'object'),
                     str(category or ''), _json(self._library_tags(tags)), '{}', timestamp, timestamp))
                self._library_event(conn, asset_id, 'ASSET_CREATED', metadata={'source_asset_id': source_asset_id})
            number = conn.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM library_asset_versions WHERE asset_id=?', (asset_id,)).fetchone()[0]
            version_id = uuid.uuid4().hex
            run = conn.execute('SELECT * FROM processing_runs WHERE id=?', (candidate['processing_run_id'],)).fetchone()
            batch = self._source_batch(conn, source_asset_id, candidate)
            processing = json.loads(candidate['metadata_json']).get('processing') or {}
            provenance = {**provenance, 'raw_source': candidate['raw_source'],
                          'raw_asset': processing.get('raw_asset'), 'processing_run_id': candidate['processing_run_id'],
                          'processing_result': json.loads(run['result_json']) if run else processing}
            snapshot = {**dict(review), 'decision': dict(decision)}
            conn.execute('''INSERT INTO library_asset_versions(id,asset_id,version_number,created_at,
                source_review_asset_id,source_review_decision_id,approved_candidate_id,source_processing_attempt_id,
                source_batch_id,source_batch_item_id,source_approved_result_ref,provenance_json,config_snapshot_json,
                candidate_metadata_json,review_snapshot_json,warnings_json,validation_json,artifacts_json,approval_timestamp)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (version_id, asset_id, number, _now(), source_asset_id, decision['id'], candidate['id'],
                 run['id'] if run else None, batch['batch_id'] if batch else None, batch['id'] if batch else None,
                 review['approved_result_ref'], _json(provenance), candidate['config_snapshot_json'],
                 candidate['metadata_json'], _json(snapshot), candidate['warnings_json'], candidate['validation_json'],
                 _json(artifacts), review['approval_timestamp'] or decision['created_at']))
            previous = conn.execute('SELECT current_version_id FROM library_assets WHERE id=?', (asset_id,)).fetchone()[0]
            self._library_event(conn, asset_id, 'VERSION_CREATED', version_id, {'version_number': number})
            conn.execute('UPDATE library_assets SET current_version_id=?,updated_at=? WHERE id=?', (version_id, _now(), asset_id))
            self._library_event(conn, asset_id, 'CURRENT_VERSION_CHANGED', version_id, {'previous_version_id': previous})
        return {'asset': self.get_library_asset(asset_id), 'version': self.get_library_version(version_id), 'reused': False}

    @staticmethod
    def _library_tags(tags):
        if isinstance(tags, str):
            tags = tags.split(',')
        return list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip()))

    def set_current_library_version(self, asset_id, version_id):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            asset = conn.execute('SELECT * FROM library_assets WHERE id=?', (asset_id,)).fetchone()
            version = conn.execute('SELECT * FROM library_asset_versions WHERE id=? AND asset_id=?', (version_id, asset_id)).fetchone()
            if not asset or not version:
                raise ValueError('Version does not belong to this library asset')
            if asset['current_version_id'] != version_id:
                conn.execute('UPDATE library_assets SET current_version_id=?,updated_at=? WHERE id=?', (version_id, _now(), asset_id))
                self._library_event(conn, asset_id, 'CURRENT_VERSION_CHANGED', version_id,
                                    {'previous_version_id': asset['current_version_id']})
        return self.get_library_asset(asset_id)

    def update_library_asset_metadata(self, asset_id, *, display_name=None, asset_type=None,
                                      category=None, tags=None, metadata=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            asset = conn.execute('SELECT * FROM library_assets WHERE id=?', (asset_id,)).fetchone()
            if not asset:
                raise ValueError('Library asset not found')
            changes = {}
            for key, value in (('display_name', display_name), ('asset_type', asset_type), ('category', category)):
                if value is not None:
                    value = str(value).strip()
                    if key in {'display_name', 'asset_type'} and not value:
                        raise ValueError(key + ' must not be empty')
                    if value != asset[key]:
                        changes[key] = value
            if tags is not None and _json(self._library_tags(tags)) != asset['tags_json']:
                changes['tags_json'] = _json(self._library_tags(tags))
            if metadata is not None:
                if not isinstance(metadata, dict):
                    raise ValueError('Metadata must be an object')
                if _json(metadata) != asset['metadata_json']:
                    changes['metadata_json'] = _json(metadata)
            if changes:
                assignments = ','.join(key + '=?' for key in changes)
                conn.execute('UPDATE library_assets SET ' + assignments + ',updated_at=? WHERE id=?',
                             (*changes.values(), _now(), asset_id))
                action = 'ASSET_RENAMED' if 'display_name' in changes else 'METADATA_UPDATED'
                self._library_event(conn, asset_id, action, metadata={'changes': changes, 'previous_display_name': asset['display_name']})
        return self.get_library_asset(asset_id)

    def rename_library_asset(self, asset_id, display_name):
        return self.update_library_asset_metadata(asset_id, display_name=display_name)

    def archive_library_asset(self, asset_id, archived=True):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            asset = conn.execute('SELECT * FROM library_assets WHERE id=?', (asset_id,)).fetchone()
            if not asset:
                raise ValueError('Library asset not found')
            if bool(asset['archived']) != bool(archived):
                conn.execute('UPDATE library_assets SET archived=?,updated_at=? WHERE id=?', (int(bool(archived)), _now(), asset_id))
                self._library_event(conn, asset_id, 'ARCHIVED' if archived else 'RESTORED')
        return self.get_library_asset(asset_id)

    def library_history(self, asset_id):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM library_events WHERE asset_id=? ORDER BY rowid DESC', (asset_id,))]

    def library_counters(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()[:10]
        with self.connect() as conn:
            return {'active_assets': conn.execute('SELECT COUNT(*) FROM library_assets WHERE archived=0').fetchone()[0],
                    'archived_assets': conn.execute('SELECT COUNT(*) FROM library_assets WHERE archived=1').fetchone()[0],
                    'total_versions': conn.execute('SELECT COUNT(*) FROM library_asset_versions').fetchone()[0],
                    'unassigned_approved_results': len(self.list_unassigned_approved_results()),
                    'recently_updated': conn.execute('SELECT COUNT(*) FROM library_assets WHERE substr(updated_at,1,10)>=?', (cutoff,)).fetchone()[0]}

    def library_integrity_issues(self):
        issues = []
        with self.connect() as conn:
            for row in conn.execute('PRAGMA foreign_key_check'):
                if row[0].startswith('library_'):
                    issues.append({'kind': 'dangling_reference', 'table': row[0], 'rowid': row[1]})
            for row in conn.execute('''SELECT a.id FROM library_assets a LEFT JOIN library_asset_versions v
                    ON v.id=a.current_version_id AND v.asset_id=a.id WHERE v.id IS NULL'''):
                issues.append({'kind': 'invalid_current_version', 'asset_id': row['id']})
            for row in conn.execute('''SELECT a.id FROM library_assets a WHERE a.archived=1
                    AND NOT EXISTS(SELECT 1 FROM library_asset_versions v WHERE v.asset_id=a.id)'''):
                issues.append({'kind': 'archived_without_versions', 'asset_id': row['id']})
            for row in conn.execute('SELECT * FROM library_asset_versions'):
                artifacts = json.loads(row['artifacts_json'])
                for key, expected in artifacts.get('sha256', {}).items():
                    path = artifacts.get(key)
                    if not path or not Path(path).is_file():
                        issues.append({'kind': 'missing_artifact', 'version_id': row['id'], 'artifact': key})
                    elif _digest(path) != expected:
                        issues.append({'kind': 'changed_artifact', 'version_id': row['id'], 'artifact': key})
        return issues
