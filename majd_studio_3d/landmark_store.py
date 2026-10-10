"""Persistence for manually marked (and later detected) character landmarks (stdlib only)."""

from __future__ import annotations

import json
import uuid

from .landmarks import LANDMARK_VIEWS, validate_points


def initialize_landmark_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS landmark_sets (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            view TEXT NOT NULL,
            points_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            confidence REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_landmark_sets_asset ON landmark_sets(asset_id, view, created_at);
        """
    )


class LandmarkStoreMixin:
    """Each save appends a row; the newest row per view is the current set, so edits keep history."""

    def save_landmarks(self, asset_id: str, view: str, points: dict, source: str = "manual", confidence=None) -> str:
        from .store import utcnow
        if view not in LANDMARK_VIEWS:
            raise ValueError(f"Landmarks are not supported for view {view}")
        clean = validate_points(points)
        landmark_id = uuid.uuid4().hex
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone():
                raise ValueError("Asset not found")
            conn.execute(
                "INSERT INTO landmark_sets(id,asset_id,view,points_json,source,confidence,created_at) VALUES(?,?,?,?,?,?,?)",
                (landmark_id, asset_id, view, json.dumps(clean, sort_keys=True), source, confidence, utcnow()),
            )
            conn.commit()
        return landmark_id

    def landmarks_for_asset(self, asset_id: str) -> dict:
        """Current landmarks as {view: {name: {"x", "y"}}}; views whose newest set is empty are omitted."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT view, points_json FROM landmark_sets WHERE asset_id=?
                   ORDER BY created_at, rowid""", (asset_id,)).fetchall()
        current = {}
        for row in rows:
            current[row["view"]] = json.loads(row["points_json"])
        return {view: points for view, points in current.items() if points}
