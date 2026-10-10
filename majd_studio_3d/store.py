# -*- coding: utf-8 -*-
"""Majd Studio 3D V9 data layer.

Pure stdlib module for:
- Projects
- Style profiles
- Assets
- Asset versions
- Project/global library scope
- Project variants

This module intentionally has no Gradio/Torch/Blender dependency so it can be
unit-tested without loading AI models.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Iterable

from .atomic_io import atomic_write_json
from .review_store import ReviewStoreMixin, initialize_review_schema
from .landmark_store import LandmarkStoreMixin, initialize_landmark_schema
from .library_store import LibraryStoreMixin, initialize_library_schema

SCHEMA_VERSION = 99


def utcnow() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    import re
    value = (value or "item").strip()
    value = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return value or "item"


class _ClosingConnection(sqlite3.Connection):
    """`with` commits like sqlite3 does, then closes so Windows releases the database file."""

    def __exit__(self, *exc_info):
        try:
            return super().__exit__(*exc_info)
        finally:
            self.close()


class V9Store(ReviewStoreMixin, LibraryStoreMixin, LandmarkStoreMixin):
    def __init__(self, db_path: Path, projects_root: Path, library_root: Path):
        self.db_path = Path(db_path)
        self.projects_root = Path(projects_root)
        self.library_root = Path(library_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.library_root.mkdir(parents=True, exist_ok=True)
        self.init_schema()
        self.ensure_defaults()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    description TEXT,
                    default_style_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS style_profiles (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    name TEXT NOT NULL,
                    description TEXT,
                    locked INTEGER NOT NULL DEFAULT 1,
                    shape_language TEXT,
                    proportions TEXT,
                    palette TEXT,
                    materials TEXT,
                    topology_notes TEXT,
                    poly_budget INTEGER NOT NULL DEFAULT 50000,
                    min_silhouette_score REAL NOT NULL DEFAULT 0.0,
                    engine_mode TEXT NOT NULL DEFAULT 'Auto',
                    candidates INTEGER NOT NULL DEFAULT 3,
                    steps INTEGER NOT NULL DEFAULT 30,
                    guidance REAL NOT NULL DEFAULT 5.0,
                    resolution INTEGER NOT NULL DEFAULT 256,
                    reference_dir TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    style_id TEXT,
                    parent_asset_id TEXT,
                    name TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    library_category TEXT,
                    is_global INTEGER NOT NULL DEFAULT 0,
                    style_lock INTEGER NOT NULL DEFAULT 1,
                    current_version INTEGER NOT NULL DEFAULT 0,

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT,

                    engine TEXT,
                    candidates INTEGER NOT NULL DEFAULT 3,
                    steps INTEGER NOT NULL DEFAULT 30,
                    guidance REAL NOT NULL DEFAULT 5.0,
                    resolution INTEGER NOT NULL DEFAULT 256,
                    base_seed INTEGER NOT NULL DEFAULT 1234,
                    seed_strategy TEXT NOT NULL DEFAULT 'Sequential',
                    remove_bg INTEGER NOT NULL DEFAULT 1,
                    preserve_mesh INTEGER NOT NULL DEFAULT 1,
                    auto_blender INTEGER NOT NULL DEFAULT 1,
                    retry_count INTEGER NOT NULL DEFAULT 1,
                    target_size REAL NOT NULL DEFAULT 1.0,
                    unit TEXT NOT NULL DEFAULT 'm',

                    front_path TEXT,
                    back_path TEXT,
                    left_path TEXT,
                    right_path TEXT,
                    threeq_path TEXT,
                    detail_path TEXT,
                    output_dir TEXT,
                    candidates_json TEXT,
                    best_glb TEXT,
                    best_blend TEXT,
                    best_score REAL,

                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(style_id) REFERENCES style_profiles(id) ON DELETE SET NULL,
                    FOREIGN KEY(parent_asset_id) REFERENCES assets(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);
                CREATE INDEX IF NOT EXISTS idx_assets_style ON assets(style_id);
                CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);

                CREATE TABLE IF NOT EXISTS asset_versions (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    style_id TEXT,
                    approved_candidate INTEGER,
                    score REAL,
                    style_lock_result TEXT,
                    style_lock_notes TEXT,
                    glb_path TEXT NOT NULL,
                    blend_path TEXT,
                    thumbnail_path TEXT,
                    manifest_path TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(asset_id, version_number),
                    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE,
                    FOREIGN KEY(style_id) REFERENCES style_profiles(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS style_references (
                    id TEXT PRIMARY KEY,
                    style_id TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'General',
                    view_name TEXT NOT NULL DEFAULT 'Any',
                    label TEXT,
                    image_path TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(style_id) REFERENCES style_profiles(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_style_refs_style ON style_references(style_id);

                CREATE TABLE IF NOT EXISTS preflight_runs (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    style_id TEXT,
                    input_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    results_json TEXT NOT NULL,
                    calibrated_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE,
                    FOREIGN KEY(style_id) REFERENCES style_profiles(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_preflight_asset ON preflight_runs(asset_id, created_at);

                CREATE TABLE IF NOT EXISTS part_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_asset_id TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id),
                    FOREIGN KEY(parent_asset_id) REFERENCES assets(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS part_approvals (
                    run_id TEXT NOT NULL,
                    part_id INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    PRIMARY KEY(run_id,part_id,project_id),
                    FOREIGN KEY(run_id) REFERENCES part_runs(id),
                    FOREIGN KEY(asset_id) REFERENCES assets(id)
                );

                CREATE TABLE IF NOT EXISTS cleanup_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    asset_id TEXT,
                    version_id TEXT,
                    source_model_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success','failed','cancelled')),
                    faces_before INTEGER,
                    faces_after INTEGER,
                    islands_removed INTEGER,
                    island_faces_removed INTEGER,
                    duration_seconds REAL NOT NULL,
                    error_message TEXT,
                    artifact_paths_json TEXT NOT NULL,
                    config_snapshot_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(version_id) REFERENCES asset_versions(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cleanup_project ON cleanup_runs(project_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_cleanup_asset ON cleanup_runs(asset_id,created_at);
                CREATE TABLE IF NOT EXISTS processing_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    asset_id TEXT,
                    version_id TEXT,
                    raw_asset TEXT NOT NULL,
                    raw_snapshot TEXT,
                    processed_asset TEXT,
                    reduced_asset TEXT,
                    raw_sha256 TEXT,
                    config_digest TEXT,
                    engine TEXT NOT NULL,
                    pipeline_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success','failed','cancelled')),
                    face_reducer_status TEXT NOT NULL,
                    cleanup_status TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    original_faces INTEGER,
                    requested_face_budget INTEGER,
                    reduced_faces INTEGER,
                    final_faces INTEGER,
                    duration_seconds REAL NOT NULL,
                    error_message TEXT,
                    config_snapshot_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(version_id) REFERENCES asset_versions(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_processing_cache ON processing_runs(raw_sha256,config_digest,engine,status);
                CREATE INDEX IF NOT EXISTS idx_processing_asset ON processing_runs(asset_id,created_at);
                CREATE TABLE IF NOT EXISTS processing_batches (
                    id TEXT PRIMARY KEY, project_id TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','partially_failed','success','cancelled','interrupted')),
                    asset_count INTEGER NOT NULL DEFAULT 0, completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0, cancelled_count INTEGER NOT NULL DEFAULT 0,
                    reused_count INTEGER NOT NULL DEFAULT 0, config_snapshot_json TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, owner_token TEXT, owner_pid INTEGER,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS processing_batch_items (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    asset_id TEXT, candidate_index INTEGER, candidate_number INTEGER, asset_name TEXT,
                    engine TEXT NOT NULL, raw_source TEXT NOT NULL, raw_asset TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL, expected_candidates_json TEXT, source_input TEXT NOT NULL,
                    processing_run_id TEXT, active_run_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','processing','success','failed','cancelled','reused')),
                    stage TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    error_message TEXT, warnings_json TEXT NOT NULL DEFAULT '[]', result_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(batch_id) REFERENCES processing_batches(id),
                    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL,
                    FOREIGN KEY(processing_run_id) REFERENCES processing_runs(id) ON DELETE SET NULL,
                    UNIQUE(batch_id,asset_id,raw_sha256), UNIQUE(batch_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_processing_batch_project ON processing_batches(project_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_processing_batch_queue ON processing_batch_items(batch_id,status,ordinal);


                """
            )
            # V9 Phase 2 migrations for existing databases.
            def ensure_column(table: str, name: str, declaration: str):
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

            ensure_column("style_profiles", "preflight_required", "INTEGER NOT NULL DEFAULT 1")
            ensure_column("style_profiles", "min_preflight_score", "REAL NOT NULL DEFAULT 0.75")
            ensure_column("style_profiles", "calibration_enabled", "INTEGER NOT NULL DEFAULT 1")
            ensure_column("style_profiles", "calibration_canvas", "INTEGER NOT NULL DEFAULT 1024")
            ensure_column("style_profiles", "target_occupancy", "REAL NOT NULL DEFAULT 0.82")
            ensure_column("style_profiles", "min_style_geometry_score", "REAL NOT NULL DEFAULT 0.0")
            ensure_column("assets", "preflight_status", "TEXT NOT NULL DEFAULT 'NOT_RUN'")
            ensure_column("assets", "preflight_score", "REAL")
            ensure_column("assets", "use_calibrated", "INTEGER NOT NULL DEFAULT 1")
            # Schema 99: generation ownership, resumable runs and failure classification.
            ensure_column("assets", "generation_run_id", "TEXT")
            ensure_column("assets", "generation_owner", "TEXT")
            ensure_column("assets", "generation_owner_pid", "INTEGER")
            ensure_column("assets", "failure_kind", "TEXT")
            ensure_column("assets", "generation_metrics_json", "TEXT")

            initialize_review_schema(conn)
            initialize_library_schema(conn)
            initialize_landmark_schema(conn)
            ensure_column("processing_batch_items", "review_candidate_id", "TEXT")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------

    def ensure_defaults(self):
        projects = self.list_projects(include_archived=True)
        if projects:
            return
        project_id = self.create_project(
            "Majd Default Project",
            "Default workspace created by V9. You can rename or archive it later.",
            create_default_style=False,
        )
        style_id = self.create_style(
            name="Majd Soft 3D",
            project_id=project_id,
            description="Default stylized 3D production profile.",
            shape_language="Soft rounded masses; clear silhouettes; restrained detail.",
            proportions="Project-specific. Keep proportions consistent with approved sheets.",
            palette="Project-approved palette only.",
            materials="Soft PBR; controlled roughness; avoid noisy micro-detail.",
            topology_notes="Animation-friendly topology after production cleanup.",
            poly_budget=50000,
            min_silhouette_score=0.70,
            engine_mode="Auto",
            candidates=3,
            steps=30,
            guidance=5.0,
            resolution=256,
            locked=True,
        )
        self.set_default_style(project_id, style_id)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(self, name: str, description: str = "", create_default_style: bool = True) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("Project name is required")
        pid = uuid.uuid4().hex[:12]
        base = slugify(name)
        slug = base
        n = 2
        with self.connect() as conn:
            while conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                slug = f"{base}_{n}"
                n += 1
            now = utcnow()
            conn.execute(
                "INSERT INTO projects(id,name,slug,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (pid, name, slug, description or "", now, now),
            )
            conn.commit()
        self.project_dir(pid).mkdir(parents=True, exist_ok=True)
        if create_default_style:
            sid = self.create_style(
                name="Default Style",
                project_id=pid,
                description="Project default style profile.",
                locked=True,
            )
            self.set_default_style(pid, sid)
        return pid

    def list_projects(self, include_archived: bool = False):
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived=0"
        sql += " ORDER BY created_at ASC"
        with self.connect() as conn:
            return conn.execute(sql).fetchall()

    def get_project(self, project_id: str):
        if not project_id:
            return None
        with self.connect() as conn:
            return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def set_default_style(self, project_id: str, style_id: Optional[str]):
        with self.connect() as conn:
            conn.execute(
                "UPDATE projects SET default_style_id=?,updated_at=? WHERE id=?",
                (style_id, utcnow(), project_id),
            )
            conn.commit()

    def archive_project(self, project_id: str, archived: bool = True):
        with self.connect() as conn:
            conn.execute(
                "UPDATE projects SET archived=?,updated_at=? WHERE id=?",
                (int(bool(archived)), utcnow(), project_id),
            )
            conn.commit()

    def project_dir(self, project_id: str) -> Path:
        p = self.get_project(project_id)
        slug = p["slug"] if p else project_id
        return self.projects_root / f"{slug}_{project_id}"

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def create_style(
        self,
        name: str,
        project_id: Optional[str] = None,
        description: str = "",
        shape_language: str = "",
        proportions: str = "",
        palette: str = "",
        materials: str = "",
        topology_notes: str = "",
        poly_budget: int = 50000,
        min_silhouette_score: float = 0.0,
        engine_mode: str = "Auto",
        candidates: int = 3,
        steps: int = 30,
        guidance: float = 5.0,
        resolution: int = 256,
        locked: bool = True,
        reference_dir: str = "",
    ) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("Style name is required")
        sid = uuid.uuid4().hex[:12]
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO style_profiles(
                    id,project_id,name,description,locked,shape_language,proportions,palette,
                    materials,topology_notes,poly_budget,min_silhouette_score,engine_mode,
                    candidates,steps,guidance,resolution,reference_dir,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid, project_id, name, description or "", int(bool(locked)),
                    shape_language or "", proportions or "", palette or "", materials or "",
                    topology_notes or "", int(poly_budget or 0), float(min_silhouette_score or 0),
                    engine_mode or "Auto", int(candidates), int(steps), float(guidance),
                    int(resolution), reference_dir or "", now, now,
                ),
            )
            conn.commit()
        return sid

    def update_style(self, style_id: str, **fields):
        allowed = {
            "name","description","locked","shape_language","proportions","palette","materials",
            "topology_notes","poly_budget","min_silhouette_score","engine_mode","candidates",
            "steps","guidance","resolution","reference_dir","project_id",
            "preflight_required","min_preflight_score","calibration_enabled",
            "calibration_canvas","target_occupancy","min_style_geometry_score"
        }
        fields = {k:v for k,v in fields.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = utcnow()
        sql = "UPDATE style_profiles SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?"
        with self.connect() as conn:
            conn.execute(sql, list(fields.values()) + [style_id])
            conn.commit()

    def list_styles(self, project_id: Optional[str] = None, include_global: bool = True):
        with self.connect() as conn:
            if project_id and include_global:
                return conn.execute(
                    "SELECT * FROM style_profiles WHERE project_id=? OR project_id IS NULL ORDER BY project_id IS NULL DESC, created_at ASC",
                    (project_id,),
                ).fetchall()
            if project_id:
                return conn.execute(
                    "SELECT * FROM style_profiles WHERE project_id=? ORDER BY created_at ASC", (project_id,)
                ).fetchall()
            return conn.execute(
                "SELECT * FROM style_profiles WHERE project_id IS NULL ORDER BY created_at ASC"
            ).fetchall()

    def get_style(self, style_id: Optional[str]):
        if not style_id:
            return None
        with self.connect() as conn:
            return conn.execute("SELECT * FROM style_profiles WHERE id=?", (style_id,)).fetchone()

    def style_manifest(self, style_id: Optional[str]):
        row = self.get_style(style_id)
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def create_asset(self, values: dict) -> str:
        aid = values.get("id") or uuid.uuid4().hex[:12]
        project_id = values["project_id"]
        project = self.get_project(project_id)
        if not project:
            raise ValueError("Invalid project")

        name = (values.get("name") or "").strip()
        if not name:
            raise ValueError("Asset name is required")

        asset_dir = self.project_dir(project_id) / "assets" / f"{slugify(name)}_{aid}"
        (asset_dir / "input").mkdir(parents=True, exist_ok=True)
        (asset_dir / "output").mkdir(parents=True, exist_ok=True)

        now = utcnow()
        row = {
            "id": aid,
            "project_id": project_id,
            "style_id": values.get("style_id"),
            "parent_asset_id": values.get("parent_asset_id"),
            "name": name,
            "asset_type": values.get("asset_type") or "Prop",
            "library_category": values.get("library_category") or "",
            "is_global": int(bool(values.get("is_global", False))),
            "style_lock": int(bool(values.get("style_lock", True))),
            "created_at": now,
            "updated_at": now,
            "status": values.get("status") or "pending",
            "progress": int(values.get("progress", 0)),
            "message": values.get("message") or "",
            "engine": values.get("engine") or "2.1",
            "candidates": int(values.get("candidates", 3)),
            "steps": int(values.get("steps", 30)),
            "guidance": float(values.get("guidance", 5.0)),
            "resolution": int(values.get("resolution", 256)),
            "base_seed": int(values.get("base_seed", 1234)),
            "seed_strategy": values.get("seed_strategy") or "Sequential",
            "remove_bg": int(bool(values.get("remove_bg", True))),
            "preserve_mesh": int(bool(values.get("preserve_mesh", True))),
            "auto_blender": int(bool(values.get("auto_blender", True))),
            "retry_count": int(values.get("retry_count", 1)),
            "target_size": float(values.get("target_size", 1.0)),
            "unit": values.get("unit") or "m",
            "preflight_status": values.get("preflight_status") or "NOT_RUN",
            "preflight_score": values.get("preflight_score"),
            "use_calibrated": int(bool(values.get("use_calibrated", True))),
            "front_path": values.get("front_path"),
            "back_path": values.get("back_path"),
            "left_path": values.get("left_path"),
            "right_path": values.get("right_path"),
            "threeq_path": values.get("threeq_path"),
            "detail_path": values.get("detail_path"),
            "output_dir": str(asset_dir / "output"),
            "candidates_json": values.get("candidates_json"),
            "best_glb": values.get("best_glb"),
            "best_blend": values.get("best_blend"),
            "best_score": values.get("best_score"),
        }
        cols = ",".join(row.keys())
        marks = ",".join("?" for _ in row)
        with self.connect() as conn:
            conn.execute(f"INSERT INTO assets({cols}) VALUES({marks})", tuple(row.values()))
            conn.commit()
        return aid

    def get_asset(self, asset_id: Optional[str]):
        if not asset_id:
            return None
        with self.connect() as conn:
            return conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()

    def update_asset(self, asset_id: str, **fields):
        if not fields:
            return
        fields["updated_at"] = utcnow()
        sql = "UPDATE assets SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?"
        with self.connect() as conn:
            conn.execute(sql, list(fields.values()) + [asset_id])
            conn.commit()

    # ------------------------------------------------------------------
    # Generation claims: pending|failed -> processing -> review|failed|pending
    # ------------------------------------------------------------------

    def claim_asset_generation(self, asset_id: str, token: str, pid: int):
        """Move a ready pending/failed asset to processing for one owner; None when it is not claimable.

        The run id survives failures and interruptions so finished candidates can be
        reused; it is created here when the asset starts a fresh generation.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if (not row or row["status"] not in ("pending", "failed")
                    or row["engine"] not in ("2.1", "2mv") or not row["front_path"]):
                conn.rollback()
                return None
            conn.execute(
                """UPDATE assets SET status='processing',generation_owner=?,generation_owner_pid=?,
                   generation_run_id=?,failure_kind=NULL,updated_at=? WHERE id=?""",
                (token, pid, row["generation_run_id"] or uuid.uuid4().hex, utcnow(), asset_id),
            )
            row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            conn.commit()
            return row

    def finish_asset_generation(self, asset_id: str, token: str, status: str, **fields) -> bool:
        """Record the end of a claimed generation; ignored unless `token` still owns the claim."""
        if status not in ("review", "failed", "pending"):
            raise ValueError(f"Unsupported generation result: {status}")
        fields.update(status=status, generation_owner=None, generation_owner_pid=None, updated_at=utcnow())
        if status == "review":
            fields["generation_run_id"] = None
        sql = ("UPDATE assets SET " + ",".join(f"{k}=?" for k in fields)
               + " WHERE id=? AND status='processing' AND generation_owner=?")
        with self.connect() as conn:
            changed = conn.execute(sql, [*fields.values(), asset_id, token]).rowcount
            conn.commit()
        return changed == 1

    def recover_interrupted_generations(self, is_alive) -> list[str]:
        """Mark processing assets whose owner is gone as failed/interrupted, keeping their run id."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,generation_owner,generation_owner_pid FROM assets WHERE status='processing'").fetchall()
            stale = [row["id"] for row in rows if not is_alive(row["generation_owner"], row["generation_owner_pid"])]
            for asset_id in stale:
                conn.execute(
                    """UPDATE assets SET status='failed',failure_kind='interrupted',progress=0,
                       generation_owner=NULL,generation_owner_pid=NULL,message=?,updated_at=?
                       WHERE id=? AND status='processing'""",
                    ("انقطع التوليد قبل اكتماله؛ تشغيل الدفعة يستأنف من آخر Candidate محفوظ", utcnow(), asset_id),
                )
            conn.commit()
        return stale

    def requeue_asset_generation(self, asset_id: str, message: str) -> None:
        """Return an idle asset to pending for a fresh generation; approved versions are untouched."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not row:
                raise ValueError("Asset not found")
            if row["status"] == "processing":
                raise ValueError("التوليد جارٍ لهذا الأصل؛ انتظر انتهاءه أو أوقف الدفعة أولًا.")
            if conn.execute("SELECT 1 FROM processing_batch_items WHERE asset_id=? AND status='processing' LIMIT 1",
                            (asset_id,)).fetchone():
                raise ValueError("أوقف معالجة الأصل قبل إعادة التوليد.")
            conn.execute(
                """UPDATE assets SET status='pending',progress=0,candidates_json=NULL,best_glb=NULL,best_score=NULL,
                   generation_run_id=NULL,failure_kind=NULL,message=?,updated_at=? WHERE id=?""",
                (message, utcnow(), asset_id),
            )
            conn.commit()

    def update_processing_review(self, asset_id, expected_candidates, candidates, best_glb, source_matches):
        """Apply one review result atomically without changing approved versions.

        Generation marks the row processing before writing its reused filenames.
        The write transaction prevents that marker from racing the signature check.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT candidates_json,status FROM assets WHERE id=?", (asset_id,)).fetchone()
            if (not row or row["status"] not in {"review", "completed"}
                    or row["candidates_json"] != expected_candidates or not source_matches()):
                raise ValueError("Review candidate changed during processing; result is retained in history")
            conn.execute("""UPDATE assets SET candidates_json=?,
                best_glb=CASE WHEN status='completed' THEN best_glb ELSE ? END,updated_at=? WHERE id=?""",
                (candidates, best_glb, utcnow(), asset_id))

    def list_assets(
        self,
        project_id: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        include_global: bool = False,
        only_global: bool = False,
    ):
        where, args = [], []
        if only_global:
            where.append("is_global=1")
        elif project_id and include_global:
            where.append("(project_id=? OR is_global=1)")
            args.append(project_id)
        elif project_id:
            where.append("project_id=?")
            args.append(project_id)
        if statuses:
            statuses = list(statuses)
            where.append("status IN (%s)" % ",".join("?" for _ in statuses))
            args.extend(statuses)
        sql = "SELECT * FROM assets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC"
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    def promote_global(self, asset_id: str, value: bool = True):
        self.update_asset(asset_id, is_global=int(bool(value)))
        latest = self.latest_version(asset_id)
        if latest:
            self._publish_library_copy(asset_id, int(latest["version_number"]))

    def asset_input_dir(self, asset_id: str) -> Path:
        row = self.get_asset(asset_id)
        if not row:
            raise ValueError("Asset not found")
        return Path(row["output_dir"]).parent / "input"

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def next_version_number(self, asset_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 AS n FROM asset_versions WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            return int(row["n"])

    def create_version(
        self,
        asset_id: str,
        glb_path: str,
        blend_path: Optional[str] = None,
        thumbnail_path: Optional[str] = None,
        style_id: Optional[str] = None,
        approved_candidate: Optional[int] = None,
        score: Optional[float] = None,
        style_lock_result: str = "PASS",
        style_lock_notes: str = "",
        manifest: Optional[dict] = None,
    ):
        row = self.get_asset(asset_id)
        if not row:
            raise ValueError("Asset not found")
        n = self.next_version_number(asset_id)
        versions_dir = Path(row["output_dir"]) / "versions" / f"v{n:03d}"
        versions_dir.mkdir(parents=True, exist_ok=True)

        src_glb = Path(glb_path)
        final_glb = versions_dir / f"{slugify(row['name'])}_v{n:03d}.glb"
        shutil.copy2(src_glb, final_glb)

        final_blend = None
        if blend_path and Path(blend_path).exists():
            final_blend = versions_dir / f"{slugify(row['name'])}_v{n:03d}.blend"
            shutil.copy2(blend_path, final_blend)

        final_thumb = None
        if thumbnail_path and Path(thumbnail_path).exists():
            final_thumb = versions_dir / "thumbnail.png"
            shutil.copy2(thumbnail_path, final_thumb)

        manifest_path = versions_dir / "manifest.json"
        payload = manifest or {}
        payload.update({
            "asset_id": asset_id,
            "project_id": row["project_id"],
            "style_id": style_id or row["style_id"],
            "version": n,
            "created_at": utcnow(),
            "approved_candidate": approved_candidate,
            "score": score,
            "style_lock_result": style_lock_result,
            "style_lock_notes": style_lock_notes,
        })
        atomic_write_json(manifest_path, payload)

        vid = uuid.uuid4().hex[:12]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO asset_versions(
                    id,asset_id,version_number,style_id,approved_candidate,score,
                    style_lock_result,style_lock_notes,glb_path,blend_path,thumbnail_path,
                    manifest_path,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    vid, asset_id, n, style_id or row["style_id"], approved_candidate, score,
                    style_lock_result, style_lock_notes, str(final_glb),
                    str(final_blend) if final_blend else None,
                    str(final_thumb) if final_thumb else None,
                    str(manifest_path), utcnow(),
                ),
            )
            conn.execute(
                "UPDATE assets SET current_version=?,status='completed',best_glb=?,best_blend=?,best_score=?,updated_at=? WHERE id=?",
                (n, str(final_glb), str(final_blend) if final_blend else None, score, utcnow(), asset_id),
            )
            conn.commit()
        self._publish_library_copy(asset_id, n)
        return vid, n, str(final_glb), str(final_blend) if final_blend else None, str(final_thumb) if final_thumb else None

    def list_versions(self, asset_id: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version_number DESC",
                (asset_id,),
            ).fetchall()

    def latest_version(self, asset_id: str):
        rows = self.list_versions(asset_id)
        asset = self.get_asset(asset_id)
        # Reapproving a historical candidate reuses its version. Library readers
        # must follow the published version rather than the largest version number.
        return next((row for row in rows if asset and row["version_number"] == asset["current_version"]),
                    rows[0] if rows else None)

    def _publish_library_copy(self, asset_id: str, version_number: int):
        asset = self.get_asset(asset_id)
        project = self.get_project(asset["project_id"])
        version = None
        with self.connect() as conn:
            version = conn.execute(
                "SELECT * FROM asset_versions WHERE asset_id=? AND version_number=?",
                (asset_id, version_number),
            ).fetchone()
        if not version:
            return

        scope = "global" if asset["is_global"] else f"projects/{slugify(project['name'])}_{project['id']}"
        dest = self.library_root / scope / slugify(asset["library_category"] or asset["asset_type"]) / slugify(asset["name"]) / f"v{version_number:03d}"
        dest.mkdir(parents=True, exist_ok=True)
        for key in ("glb_path", "blend_path", "thumbnail_path", "manifest_path"):
            path = version[key]
            if path and Path(path).exists():
                shutil.copy2(path, dest / Path(path).name)

    # ------------------------------------------------------------------
    # V9 Phase 2: style references + preflight/calibration
    # ------------------------------------------------------------------

    def style_reference_dir(self, style_id: str) -> Path:
        style = self.get_style(style_id)
        if not style:
            raise ValueError("Style not found")
        if style["project_id"]:
            base = self.project_dir(style["project_id"]) / "style_references" / style_id
        else:
            base = self.library_root / "style_references" / style_id
        base.mkdir(parents=True, exist_ok=True)
        return base

    def add_style_reference(
        self,
        style_id: str,
        image_path: str,
        category: str = "General",
        view_name: str = "Any",
        label: str = "",
        weight: float = 1.0,
    ) -> str:
        style = self.get_style(style_id)
        if not style:
            raise ValueError("Style not found")
        src = Path(image_path)
        if not src.exists():
            raise ValueError("Reference image not found")
        rid = uuid.uuid4().hex[:12]
        ext = src.suffix.lower() or ".png"
        dst = self.style_reference_dir(style_id) / f"{rid}{ext}"
        shutil.copy2(src, dst)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO style_references(id,style_id,category,view_name,label,image_path,weight,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (rid, style_id, category or "General", view_name or "Any", label or src.stem,
                 str(dst), float(weight or 1.0), utcnow()),
            )
            conn.commit()
        return rid

    def list_style_references(self, style_id: str):
        if not style_id:
            return []
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM style_references WHERE style_id=? ORDER BY created_at ASC",
                (style_id,),
            ).fetchall()

    def delete_style_reference(self, reference_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM style_references WHERE id=?", (reference_id,)).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM style_references WHERE id=?", (reference_id,))
            conn.commit()
        try:
            Path(row["image_path"]).unlink(missing_ok=True)
        except Exception:
            pass

    def save_preflight(self, asset_id: str, result: dict):
        asset = self.get_asset(asset_id)
        if not asset:
            raise ValueError("Asset not found")
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO preflight_runs(
                    id,asset_id,style_id,input_signature,status,score,results_json,calibrated_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, asset_id, asset["style_id"], result.get("input_signature") or "",
                    result.get("status") or "FAIL", float(result.get("score") or 0),
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(result.get("calibrated") or {}, ensure_ascii=False), utcnow(),
                ),
            )
            conn.execute(
                "UPDATE assets SET preflight_status=?,preflight_score=?,updated_at=? WHERE id=?",
                (result.get("status") or "FAIL", float(result.get("score") or 0), utcnow(), asset_id),
            )
            conn.commit()
        return run_id

    def latest_preflight(self, asset_id: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM preflight_runs WHERE asset_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (asset_id,),
            ).fetchone()

    def preflight_result(self, asset_id: str):
        row = self.latest_preflight(asset_id)
        if not row:
            return None
        try:
            return json.loads(row["results_json"])
        except Exception:
            return None

    def style_conformance_check(self, asset_id: str, candidate: dict):
        asset = self.get_asset(asset_id)
        if not asset:
            return {"status":"FAIL","overall":0.0,"metrics":{},"notes":["Asset not found"]}
        style = self.get_style(asset["style_id"])
        preflight = self.preflight_result(asset_id)
        refs = self.list_style_references(asset["style_id"]) if asset["style_id"] else []

        silhouette = float(candidate.get("score", 0) or 0)
        preflight_score = float((preflight or {}).get("score", 0) or 0)
        faces = int(candidate.get("faces", 0) or 0)

        if style and int(style["poly_budget"] or 0) > 0:
            poly_score = min(1.0, float(style["poly_budget"]) / max(faces, 1))
        else:
            poly_score = 1.0

        # Settings compliance is measurable and deterministic. It is deliberately
        # separate from a learned visual-style embedding, which is not faked here.
        settings_parts = []
        if style:
            settings_parts.append(1.0 if int(asset["resolution"]) == int(style["resolution"]) else 0.65)
            settings_parts.append(1.0 if int(asset["steps"]) == int(style["steps"]) else 0.75)
            settings_parts.append(1.0 if abs(float(asset["guidance"]) - float(style["guidance"])) < 1e-9 else 0.75)
        settings_score = sum(settings_parts)/len(settings_parts) if settings_parts else 1.0
        geometry_score = candidate.get("style_geometry_score")
        geometry_component = float(geometry_score) if geometry_score is not None else 0.0

        # Phase 2.1 geometry references are deliberately a broad silhouette-language signal,
        # not a learned semantic/artistic embedding. It contributes only when available.
        if geometry_score is None:
            overall = 0.45*silhouette + 0.25*preflight_score + 0.15*poly_score + 0.15*settings_score
        else:
            overall = 0.35*silhouette + 0.20*preflight_score + 0.15*poly_score + 0.10*settings_score + 0.20*geometry_component
        notes = []
        if refs:
            notes.append(f"{len(refs)} approved style reference(s) attached. Deep visual embedding is not enabled in Phase 2.1.")
        else:
            notes.append("No approved style references are attached yet.")

        lock_status, lock_notes = self.style_lock_check(asset_id, candidate)
        notes.append(lock_notes)
        status = "FAIL" if lock_status == "FAIL" else ("WARN" if lock_status == "WARN" else "PASS")
        return {
            "status": status,
            "overall": round(overall, 6),
            "metrics": {
                "silhouette": round(silhouette, 6),
                "preflight": round(preflight_score, 6),
                "poly_budget": round(poly_score, 6),
                "settings": round(settings_score, 6),
                "style_geometry": None if geometry_score is None else round(float(geometry_score), 6),
                "reference_count": len(refs),
            },
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Style lock v1
    # ------------------------------------------------------------------

    def style_lock_check(self, asset_id: str, candidate: dict):
        asset = self.get_asset(asset_id)
        if not asset or not asset["style_lock"]:
            return "OFF", "Style Lock disabled for this asset."
        style = self.get_style(asset["style_id"])
        if not style or not style["locked"]:
            return "OFF", "No locked style profile is active."

        failures, warnings = [], []

        preflight = self.preflight_result(asset_id)
        if int(style["preflight_required"] or 0):
            if not preflight:
                failures.append("Preflight has not been run for this asset.")
            else:
                pf_status = preflight.get("status") or "FAIL"
                pf_score = float(preflight.get("score") or 0)
                if pf_status == "FAIL":
                    failures.append("Preflight status is FAIL.")
                if pf_score < float(style["min_preflight_score"] or 0):
                    failures.append(
                        f"Preflight {pf_score*100:.1f}% is below style minimum {float(style['min_preflight_score'])*100:.1f}%."
                    )

        faces = int(candidate.get("faces", 0) or 0)
        score = float(candidate.get("score", 0) or 0)

        if style["poly_budget"] and faces > int(style["poly_budget"]):
            failures.append(f"Faces {faces:,} exceed style budget {int(style['poly_budget']):,}.")
        if style["min_silhouette_score"] and score < float(style["min_silhouette_score"]):
            failures.append(
                f"Silhouette {score*100:.1f}% is below style minimum {float(style['min_silhouette_score'])*100:.1f}%."
            )

        min_geom = float(style["min_style_geometry_score"] or 0) if "min_style_geometry_score" in style.keys() else 0.0
        geom = candidate.get("style_geometry_score")
        if min_geom > 0:
            if geom is None:
                warnings.append("No compatible Style Reference geometry score is available.")
            elif float(geom) < min_geom:
                failures.append(
                    f"Style geometry {float(geom)*100:.1f}% is below style minimum {min_geom*100:.1f}%."
                )

        if asset["resolution"] != style["resolution"]:
            warnings.append(f"Resolution differs from locked style ({asset['resolution']} vs {style['resolution']}).")
        if asset["steps"] != style["steps"]:
            warnings.append(f"Steps differ from locked style ({asset['steps']} vs {style['steps']}).")
        if abs(float(asset["guidance"]) - float(style["guidance"])) > 1e-9:
            warnings.append("Guidance differs from locked style.")

        if failures:
            return "FAIL", " ".join(failures + warnings)
        if warnings:
            return "WARN", " ".join(warnings)
        return "PASS", "Production constraints match the locked style profile."

    # ------------------------------------------------------------------
    # Variants
    # ------------------------------------------------------------------

    def create_variant(self, source_asset_id: str, target_project_id: str, target_style_id: Optional[str], new_name: str = ""):
        source = self.get_asset(source_asset_id)
        latest = self.latest_version(source_asset_id)
        if not source or not latest:
            raise ValueError("Source asset has no approved version")
        name = (new_name or f"{source['name']} Variant").strip()
        aid = self.create_asset({
            "project_id": target_project_id,
            "style_id": target_style_id,
            "parent_asset_id": source_asset_id,
            "name": name,
            "asset_type": source["asset_type"],
            "library_category": source["library_category"],
            "style_lock": True,
            "status": "completed",
            "progress": 100,
            "message": f"Variant from {source['name']}",
            "engine": source["engine"],
            "candidates": source["candidates"],
            "steps": source["steps"],
            "guidance": source["guidance"],
            "resolution": source["resolution"],
            "base_seed": source["base_seed"],
            "seed_strategy": source["seed_strategy"],
            "remove_bg": bool(source["remove_bg"]),
            "preserve_mesh": bool(source["preserve_mesh"]),
            "auto_blender": bool(source["auto_blender"]),
            "retry_count": source["retry_count"],
            "target_size": source["target_size"],
            "unit": source["unit"],
        })
        self.create_version(
            aid,
            glb_path=latest["glb_path"],
            blend_path=latest["blend_path"],
            thumbnail_path=latest["thumbnail_path"],
            style_id=target_style_id,
            approved_candidate=None,
            score=latest["score"],
            style_lock_result="VARIANT",
            style_lock_notes=f"Created from asset {source_asset_id} version {latest['version_number']}",
            manifest={"source_asset_id": source_asset_id, "source_version": latest["version_number"]},
        )
        return aid

    def save_cleanup_run(self, project_id=None, asset_id=None, version_id=None, *, result: dict, config_snapshot: dict):
        """Record one immutable run; never alter the source or approve a version.

        A failed result may reference the raw source as its fallback GLB, but
        only successful runs expose model exports in artifact_paths_json.
        Identical retries of the same record are idempotent.
        """
        if not isinstance(result, dict) or not isinstance(config_snapshot, dict):
            raise ValueError("Cleanup result and config snapshot must be dictionaries")  # noqa: TRY004 -- match store validation errors
        run_id = result.get("job_id")
        source = result.get("source")
        status = result.get("status")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Cleanup run requires a job_id")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Cleanup run requires a source model path")
        if status not in {"success", "failed", "cancelled"}:
            raise ValueError("Cleanup status must be success, failed, or cancelled")
        counts = {}
        for field in ("faces_before", "faces_after", "islands_removed", "island_faces_removed"):
            value = result.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{field} must be a non-negative integer or null")
            if status == "success" and value is None:
                raise ValueError(f"Successful cleanup requires {field}")
            counts[field] = value
        duration = result.get("duration_seconds")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise ValueError("Cleanup duration must be a finite non-negative number")
        error = result.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("Cleanup error must be text or null")
        paths = {}
        for field in ("glb_path", "export_path", "output_dir", "log_path"):
            value = result.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} must be a path string or null")
            if status == "cancelled" and value is not None:
                raise ValueError("Cancelled cleanup cannot expose removed artifact paths")
            if value is not None and (status == "success" or field in {"output_dir", "log_path"}):
                paths[field] = value
        if status == "success":
            if not counts["faces_after"] or not paths.get("glb_path"):
                raise ValueError("Successful cleanup requires a non-empty cleaned GLB")
            if Path(paths["glb_path"]).resolve() == Path(source).resolve():
                raise ValueError("Successful cleanup must preserve the source model")
        elif status == "failed":
            if result.get("export_path") is not None:
                raise ValueError("Failed cleanup cannot expose a partial export")
            fallback = result.get("glb_path")
            if fallback is not None and Path(fallback).resolve() != Path(source).resolve():
                raise ValueError("Failed cleanup may only expose the raw source fallback")
        # Serialize before opening a transaction to snapshot caller-owned data.
        snapshot_json = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
        used_snapshot = result.get("config_snapshot")
        if used_snapshot is not None and json.dumps(used_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False) != snapshot_json:
            raise ValueError("Cleanup config snapshot must match the settings used for this run")
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        paths_json = json.dumps(paths, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.connect() as conn:
            if version_id is not None:
                version = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (version_id,)).fetchone()
                if not version or (asset_id is not None and version["asset_id"] != asset_id):
                    raise ValueError("Cleanup version does not belong to the source asset")
                asset_id = version["asset_id"]
            if asset_id is not None:
                asset = conn.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
                if not asset or (project_id is not None and asset["project_id"] != project_id):
                    raise ValueError("Cleanup source asset does not belong to this project")
                project_id = asset["project_id"]
            if project_id is not None and not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("Cleanup project is invalid")
            existing = conn.execute("SELECT * FROM cleanup_runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                if (existing["result_json"] != result_json or existing["config_snapshot_json"] != snapshot_json
                        or (existing["project_id"], existing["asset_id"], existing["version_id"])
                        != (project_id, asset_id, version_id)):
                    raise ValueError("Cleanup run is immutable; use a new job_id for re-cleaning")
                return run_id
            conn.execute(
                """INSERT INTO cleanup_runs(
                    id,project_id,asset_id,version_id,source_model_path,status,
                    faces_before,faces_after,islands_removed,island_faces_removed,duration_seconds,
                    error_message,artifact_paths_json,config_snapshot_json,result_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, project_id, asset_id, version_id, source, status,
                    counts["faces_before"], counts["faces_after"], counts["islands_removed"],
                    counts["island_faces_removed"], duration, error, paths_json, snapshot_json, result_json, utcnow()),
            )
        return run_id

    def get_cleanup_run(self, run_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cleanup_runs WHERE id=?", (run_id,)).fetchone()

    def list_cleanup_runs(self, project_id=None, asset_id=None):
        conditions = []
        parameters = []
        for field, value in (("project_id", project_id), ("asset_id", asset_id)):
            if value is not None:
                conditions.append(f"{field}=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cleanup_runs" + where + " ORDER BY created_at DESC,rowid DESC", parameters).fetchall()

    @staticmethod
    def _processing_references(conn, project_id=None, asset_id=None, version_id=None):
        if version_id is not None:
            version = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (version_id,)).fetchone()
            if not version or (asset_id is not None and version["asset_id"] != asset_id):
                raise ValueError("Processing version does not belong to the source asset")
            asset_id = version["asset_id"]
        if asset_id is not None:
            asset = conn.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not asset or (project_id is not None and asset["project_id"] != project_id):
                raise ValueError("Processing source asset does not belong to this project")
            project_id = asset["project_id"]
        if project_id is not None and not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise ValueError("Processing project is invalid")
        return project_id, asset_id, version_id

    def save_processing_run(self, project_id=None, asset_id=None, version_id=None, *, result: dict, config_snapshot: dict):
        """Persist immutable post-generation provenance without approving or changing assets."""
        if not isinstance(result, dict) or not isinstance(config_snapshot, dict):
            raise ValueError("Processing result and config snapshot must be dictionaries")  # noqa: TRY004
        for field in ("job_id", "raw_asset", "engine"):
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise ValueError(f"Processing run requires {field}")
        statuses = {
            "status": {"success", "failed", "cancelled"},
            "face_reducer_status": {"success", "failed", "disabled", "not_needed", "skipped_textured", "cancelled", "not_run"},
            "cleanup_status": {"success", "failed", "cancelled", "not_run"},
            "validation_status": {"success", "failed", "cancelled", "not_run"},
        }
        for field, choices in statuses.items():
            if result.get(field) not in choices:
                raise ValueError(f"Invalid processing {field}")
        for field in ("raw_sha256", "config_digest"):
            value = result.get(field)
            if value is None and result["status"] != "success":
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Processing run requires {field} when successful")

        for field in ("original_faces", "requested_face_budget", "reduced_faces", "final_faces"):
            value = result.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{field} must be a non-negative integer or null")
        if result.get("pipeline_version") != 1 or isinstance(result.get("pipeline_version"), bool):
            raise ValueError("Unsupported processing pipeline_version")
        duration = result.get("duration_seconds")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise ValueError("Processing duration must be a finite non-negative number")
        if result.get("error") is not None and not isinstance(result["error"], str):
            raise ValueError("Processing error must be text or null")
        if not isinstance(result.get("warnings", []), list) or any(not isinstance(w, str) for w in result.get("warnings", [])):
            raise ValueError("Processing warnings must be a list of text")
        paths = {}
        for field in ("raw_snapshot", "processed_asset", "reduced_asset", "output_dir"):
            value = result.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} must be a path string or null")
            if result["status"] == "cancelled" and value is not None:
                raise ValueError("Cancelled processing cannot expose removed artifact paths")
            paths[field] = str(Path(value).resolve()) if value else None
        if result["status"] == "success":
            if (not result.get("original_faces") or not result.get("requested_face_budget")
                    or result.get("reduced_faces") is None
                    or not result.get("final_faces") or not paths["processed_asset"]
                    or result["cleanup_status"] != "success" or result["validation_status"] != "success"
                    or result["face_reducer_status"] in {"failed", "cancelled", "not_run"}):
                raise ValueError("Successful processing requires a validated cleaned model")
            if Path(paths["processed_asset"]) == Path(result["raw_asset"]).resolve():
                raise ValueError("Successful processing must preserve the raw asset")
        if result["status"] == "failed":
            allowed_fallbacks = {str(Path(result["raw_asset"]).resolve()), paths["raw_snapshot"]}
            if paths["processed_asset"] not in allowed_fallbacks or result.get("raw_fallback") is not True:
                raise ValueError("Failed processing must explicitly expose a raw fallback")
        snapshot_json = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
        used_snapshot = result.get("configuration_snapshot")
        if used_snapshot is not None and json.dumps(used_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False) != snapshot_json:
            raise ValueError("Processing config snapshot must match the settings used for this run")
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.connect() as conn:
            references = self._processing_references(conn, project_id, asset_id, version_id)
            existing = conn.execute("SELECT * FROM processing_runs WHERE id=?", (result["job_id"],)).fetchone()
            if existing:
                if (existing["result_json"] != result_json or existing["config_snapshot_json"] != snapshot_json
                        or (existing["project_id"], existing["asset_id"], existing["version_id"]) != references):
                    raise ValueError("Processing run is immutable; use a new job_id for reprocessing")
                return result["job_id"]
            conn.execute(
                """INSERT INTO processing_runs(
                    id,project_id,asset_id,version_id,raw_asset,raw_snapshot,processed_asset,reduced_asset,
                    raw_sha256,config_digest,engine,pipeline_version,status,face_reducer_status,cleanup_status,
                    validation_status,original_faces,requested_face_budget,reduced_faces,final_faces,
                    duration_seconds,error_message,config_snapshot_json,result_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result["job_id"], *references, str(Path(result["raw_asset"]).resolve()), paths["raw_snapshot"],
                 paths["processed_asset"], paths["reduced_asset"], result.get("raw_sha256"), result.get("config_digest"),
                 result["engine"], result["pipeline_version"], result["status"], result["face_reducer_status"],
                 result["cleanup_status"], result["validation_status"], result.get("original_faces"),
                 result.get("requested_face_budget"), result.get("reduced_faces"), result.get("final_faces"),
                 duration, result.get("error"), snapshot_json, result_json, utcnow()),
            )
        return result["job_id"]

    def get_processing_run(self, job_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_runs WHERE id=?", (job_id,)).fetchone()

    def find_processing_run(self, raw_sha256, config_digest, engine, project_id=None, asset_id=None, version_id=None, raw_asset=None):
        """Only reuse successful runs in the same model/version context."""
        with self.connect() as conn:
            references = self._processing_references(conn, project_id, asset_id, version_id)
            source_filter = " AND raw_asset=?" if raw_asset is not None else ""
            parameters = (raw_sha256, config_digest, engine, *references)
            if raw_asset is not None:
                parameters += (str(Path(raw_asset).resolve()),)
            return conn.execute(
                """SELECT * FROM processing_runs WHERE raw_sha256=? AND config_digest=? AND engine=?
                   AND pipeline_version=1 AND status='success'
                   AND project_id IS ? AND asset_id IS ? AND version_id IS ?""" + source_filter
                + " ORDER BY created_at DESC,rowid DESC LIMIT 1", parameters,
            ).fetchone()

    def find_processing_artifact(self, path):
        """Recognize outputs and snapshots, never an original generation's mutable path."""
        normalized = str(Path(path).resolve())
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM processing_runs
                   WHERE raw_snapshot=? OR (processed_asset=? AND processed_asset<>raw_asset) OR reduced_asset=?
                   ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (normalized, normalized, normalized),
            ).fetchone()

    def list_processing_runs(self, project_id=None, asset_id=None, version_id=None):
        conditions = []
        parameters = []
        for field, value in (("project_id", project_id), ("asset_id", asset_id), ("version_id", version_id)):
            if value is not None:
                conditions.append(f"{field}=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_runs" + where + " ORDER BY created_at DESC,rowid DESC", parameters).fetchall()

    # Durable queue records contain immutable selection/config snapshots. A lease
    # serializes workers; terminal item transitions serialize cancellation races.
    def create_processing_batch(self, project_id, config_snapshot, items, batch_id=None, *, review_asset_id=None, review_reason=None):
        if not isinstance(config_snapshot, dict):
            raise ValueError("Batch config snapshot must be a dictionary")
        snapshot = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
        batch_id = batch_id or uuid.uuid4().hex
        selected, seen = [], set()
        for item in items:
            key = (item.get("asset_id"), item["raw_sha256"])
            if key not in seen:
                seen.add(key)
                selected.append(dict(item))
        if not selected:
            raise ValueError("A processing batch needs at least one item")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._processing_references(conn, project_id)
            if review_asset_id is not None:
                self._assert_review_idle(conn, review_asset_id)
                for event in conn.execute("SELECT metadata_json FROM review_events WHERE asset_id=? AND action='REQUEST_RETRY' ORDER BY rowid DESC", (review_asset_id,)):
                    previous_id = json.loads(event["metadata_json"]).get("batch_id")
                    previous = conn.execute("SELECT config_snapshot_json FROM processing_batches WHERE id=?", (previous_id,)).fetchone()
                    if (previous and previous["config_snapshot_json"] == snapshot
                            and conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status IN ('queued','processing')", (previous_id,)).fetchone()):
                        return previous_id
            conn.execute("""INSERT INTO processing_batches(id,project_id,created_at,status,asset_count,config_snapshot_json)
                            VALUES(?,?,?,'pending',?,?)""", (batch_id, project_id, now, len(selected), snapshot))
            for ordinal, item in enumerate(selected):
                self._processing_references(conn, project_id, item.get("asset_id"))
                expected = item.get("expected_candidates_json")
                if expected is not None and not isinstance(expected, str):
                    expected = json.dumps(expected, ensure_ascii=False, allow_nan=False)
                conn.execute("""INSERT INTO processing_batch_items(
                    id,batch_id,ordinal,asset_id,candidate_index,candidate_number,asset_name,engine,
                    raw_source,raw_asset,raw_sha256,expected_candidates_json,source_input,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                    (item.get("id") or uuid.uuid4().hex, batch_id, ordinal, item.get("asset_id"),
                     item.get("candidate_index"), item.get("candidate_number"), item.get("asset_name"),
                     item["engine"], item["raw_source"], item["raw_asset"], item["raw_sha256"], expected,
                     item.get("source_input") or item["raw_asset"], now))
                if item.get("review_candidate_id"):
                    conn.execute("UPDATE processing_batch_items SET review_candidate_id=? WHERE id=?",
                        (item["review_candidate_id"], item["id"]))
            if review_asset_id is not None:
                if any(item.get("asset_id") != review_asset_id for item in selected):
                    raise ValueError("Review retry batch must belong to its reviewed asset")
                review = self._ensure_review(conn, review_asset_id)
                conn.execute("""UPDATE review_assets SET review_status='RETRY_REQUESTED',
                    retry_request_timestamp=?,updated_at=? WHERE asset_id=?""", (now, now, review_asset_id))
                self._review_event(conn, review_asset_id, "REQUEST_RETRY", review["review_status"],
                    "RETRY_REQUESTED", review["selected_candidate_id"], review_reason, {"batch_id": batch_id})
        return batch_id

    def get_processing_batch(self, batch_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()

    def list_processing_batches(self, project_id=None, include_hidden=False):
        conditions, parameters = [], []
        if project_id is not None:
            conditions.append("project_id=?")
            parameters.append(project_id)
        if not include_hidden:
            conditions.append("hidden=0")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_batches" + where + " ORDER BY created_at DESC,rowid DESC", parameters).fetchall()

    def list_processing_batch_items(self, batch_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_batch_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()

    def get_processing_batch_item(self, item_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM processing_batch_items WHERE id=?", (item_id,)).fetchone()

    @staticmethod
    def _refresh_processing_batch(conn, batch_id):
        parent = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM processing_batch_items WHERE batch_id=? GROUP BY status", (batch_id,))}
        completed = counts.get("success", 0) + counts.get("reused", 0)
        remaining = counts.get("queued", 0) + counts.get("processing", 0)
        status = parent["status"]
        if parent["cancel_requested"]:
            status = "cancelled"
        elif not remaining:
            status = "partially_failed" if counts.get("failed", 0) else ("cancelled" if counts.get("cancelled", 0) else "success")
        elif parent["owner_token"]:
            status = "running"
        elif status != "interrupted":
            status = "pending"
        conn.execute("""UPDATE processing_batches SET completed_count=?,failed_count=?,cancelled_count=?,
            reused_count=?,status=?,completed_at=? WHERE id=?""",
            (completed, counts.get("failed", 0), counts.get("cancelled", 0), counts.get("reused", 0),
             status, utcnow() if not remaining else None, batch_id))

    def claim_processing_batch(self, batch_id, owner_token, owner_pid):
        if not owner_token:
            raise ValueError("A batch worker requires an owner token")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not row or row["owner_token"] or row["cancel_requested"] or row["status"] not in {"pending", "interrupted"}:
                return False
            if conn.execute("SELECT 1 FROM processing_batches WHERE owner_token IS NOT NULL LIMIT 1").fetchone():
                return False
            if not conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status='queued' LIMIT 1", (batch_id,)).fetchone():
                return False
            conn.execute("""UPDATE processing_batches SET status='running',owner_token=?,owner_pid=?,
                started_at=COALESCE(started_at,?),completed_at=NULL WHERE id=?""", (owner_token, owner_pid, utcnow(), batch_id))
            return True

    def claim_next_processing_item(self, batch_id, owner_token):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not parent or parent["owner_token"] != owner_token or parent["status"] != "running" or parent["cancel_requested"]:
                return None
            if conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status='processing'", (batch_id,)).fetchone():
                return None
            row = conn.execute("SELECT * FROM processing_batch_items WHERE batch_id=? AND status='queued' ORDER BY ordinal LIMIT 1", (batch_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE processing_batch_items SET status='processing',stage='pending',started_at=? WHERE id=?", (utcnow(), row["id"]))
            return conn.execute("SELECT * FROM processing_batch_items WHERE id=?", (row["id"],)).fetchone()

    def update_processing_item_stage(self, item_id, stage, active_run_id=None):
        with self.connect() as conn:
            cursor = conn.execute("""UPDATE processing_batch_items SET stage=?,active_run_id=COALESCE(?,active_run_id)
                                    WHERE id=? AND status='processing'""", (stage, active_run_id, item_id))
            return bool(cursor.rowcount)

    def finish_processing_batch_item(self, item_id, status, result=None, error=None, warnings=None, processing_run_id=None):
        if status not in {"success", "failed", "cancelled", "reused"}:
            raise ValueError("Invalid terminal batch item status")
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False) if result is not None else None
        warning_json = json.dumps(warnings or [], ensure_ascii=False, allow_nan=False)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batch_items WHERE id=?", (item_id,)).fetchone()
            if not row or row["status"] != "processing":
                return False
            parent = conn.execute("SELECT * FROM processing_batches WHERE id=?", (row["batch_id"],)).fetchone()
            if row["cancel_requested"] or (parent["cancel_requested"] and row["stage"] not in {"success", "reused", "failed", "cancelled"}):
                status, encoded, processing_run_id = "cancelled", None, None
                error = error or "Cancelled"
            if processing_run_id and not conn.execute("SELECT 1 FROM processing_runs WHERE id=?", (processing_run_id,)).fetchone():
                raise ValueError("Processing run reference is invalid")
            conn.execute("""UPDATE processing_batch_items SET status=?,stage=?,result_json=?,error_message=?,warnings_json=?,
                processing_run_id=?,active_run_id=NULL,completed_at=? WHERE id=?""",
                (status, status, encoded, error, warning_json, processing_run_id, utcnow(), item_id))
            self._refresh_processing_batch(conn, row["batch_id"])
            if status in {"success", "reused"} and row["asset_id"]:
                review = conn.execute("SELECT * FROM review_assets WHERE asset_id=?", (row["asset_id"],)).fetchone()
                pending = conn.execute("SELECT 1 FROM processing_batch_items WHERE asset_id=? AND status IN ('queued','processing')",
                    (row["asset_id"],)).fetchone()
                if review and review["review_status"] == "RETRY_REQUESTED" and not pending:
                    conn.execute("UPDATE review_assets SET review_status='NEEDS_REVIEW',updated_at=? WHERE asset_id=?",
                        (utcnow(), row["asset_id"]))
                    self._review_event(conn, row["asset_id"], "RETRY_COMPLETE", "RETRY_REQUESTED", "NEEDS_REVIEW")
            return True

    def cancel_processing_batch_item(self, item_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batch_items WHERE id=?", (item_id,)).fetchone()
            if (not row or row["status"] not in {"queued", "processing"}
                    or (row["status"] == "processing" and row["stage"] in {"success", "reused", "failed", "cancelled"})):
                return False
            if row["status"] == "queued":
                conn.execute("UPDATE processing_batch_items SET status='cancelled',stage='cancelled',cancel_requested=1,completed_at=? WHERE id=?", (utcnow(), item_id))
            else:
                conn.execute("UPDATE processing_batch_items SET cancel_requested=1 WHERE id=?", (item_id,))
            self._refresh_processing_batch(conn, row["batch_id"])
            return True

    def cancel_processing_batch(self, batch_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not row or not conn.execute("""SELECT 1 FROM processing_batch_items WHERE batch_id=?
                    AND (status='queued' OR (status='processing' AND stage NOT IN ('success','reused','failed','cancelled')))""", (batch_id,)).fetchone():
                return False
            conn.execute("UPDATE processing_batches SET cancel_requested=1,status='cancelled' WHERE id=?", (batch_id,))
            conn.execute("""UPDATE processing_batch_items SET cancel_requested=1 WHERE batch_id=? AND status='processing'
                            AND stage NOT IN ('success','reused','failed','cancelled')""", (batch_id,))
            conn.execute("""UPDATE processing_batch_items SET status='cancelled',stage='cancelled',cancel_requested=1,completed_at=?
                            WHERE batch_id=? AND status='queued'""", (utcnow(), batch_id))
            self._refresh_processing_batch(conn, batch_id)
            return True

    def retry_processing_batch_items(self, batch_id, item_id=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not parent or parent["owner_token"] or conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status='processing'", (batch_id,)).fetchone():
                return 0
            selection = " AND id=?" if item_id is not None else ""
            parameters = (batch_id, item_id) if item_id is not None else (batch_id,)
            cursor = conn.execute("""UPDATE processing_batch_items SET status='queued',stage='pending',retry_count=retry_count+1,
                error_message=NULL,result_json=NULL,warnings_json='[]',processing_run_id=NULL,active_run_id=NULL,
                started_at=NULL,completed_at=NULL,cancel_requested=0 WHERE batch_id=? AND status='failed'""" + selection, parameters)
            if cursor.rowcount:
                conn.execute("UPDATE processing_batches SET cancel_requested=0,status='pending',completed_at=NULL WHERE id=?", (batch_id,))
                self._refresh_processing_batch(conn, batch_id)
            return cursor.rowcount

    def release_processing_batch(self, batch_id, owner_token):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not row or row["owner_token"] != owner_token:
                return False
            if conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status='processing'", (batch_id,)).fetchone():
                return False
            conn.execute("UPDATE processing_batches SET owner_token=NULL,owner_pid=NULL WHERE id=?", (batch_id,))
            self._refresh_processing_batch(conn, batch_id)
            return True

    def recover_processing_batch(self, batch_id, owner_token):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not row or not row["owner_token"] or row["owner_token"] != owner_token:
                return False
            unfinished = conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status IN ('queued','processing') LIMIT 1", (batch_id,)).fetchone()
            conn.execute("""UPDATE processing_batch_items SET status='failed',stage='interrupted',active_run_id=NULL,
                completed_at=?,error_message='Interrupted during processing; retry from preserved raw'
                WHERE batch_id=? AND status='processing'""", (utcnow(), batch_id))
            conn.execute("UPDATE processing_batches SET owner_token=NULL,owner_pid=NULL,status=? WHERE id=?",
                         ("cancelled" if row["cancel_requested"] else "interrupted", batch_id))
            self._refresh_processing_batch(conn, batch_id)
            # Preserve a completed result when only lease release was interrupted.
            if unfinished and not row["cancel_requested"]:
                conn.execute("UPDATE processing_batches SET status='interrupted' WHERE id=?", (batch_id,))
            return True

    def hide_processing_batch(self, batch_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM processing_batches WHERE id=?", (batch_id,)).fetchone()
            if not row or row["owner_token"] or conn.execute("SELECT 1 FROM processing_batch_items WHERE batch_id=? AND status IN ('queued','processing')", (batch_id,)).fetchone():
                return False
            conn.execute("UPDATE processing_batches SET hidden=1 WHERE id=?", (batch_id,))
            return True

    def save_part_run(self, project_id: str, parent_asset_id: Optional[str], result: dict):
        if not self.get_project(project_id) or not result.get("parts"):
            raise ValueError("Part result or project is invalid")
        parent = self.get_asset(parent_asset_id) if parent_asset_id else None
        if parent_asset_id and (not parent or (parent["project_id"] != project_id and not parent["is_global"])):
            raise ValueError("Source asset is outside this project")
        with self.connect() as conn:
            conn.execute("INSERT INTO part_runs(id,project_id,parent_asset_id,result_json,created_at) VALUES(?,?,?,?,?)",
                (result["job_id"], project_id, parent_asset_id, json.dumps(result, ensure_ascii=False), utcnow()))

    def get_part_run(self, run_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM part_runs WHERE id=?", (run_id,)).fetchone()

    def list_part_runs(self, project_id: str):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM part_runs WHERE project_id=? ORDER BY created_at DESC,rowid DESC", (project_id,)).fetchall()

    def approve_part(self, run_id: str, part_id: int, name: str, project_id: str, style_id: str) -> str:
        run = self.get_part_run(run_id)
        style = self.get_style(style_id)
        if not run or run["project_id"] != project_id or not style or style["project_id"] not in (None, project_id):
            raise ValueError("Select a part run and a style belonging to this project")
        if not (name or "").strip():
            raise ValueError("A part name is required")
        with self.connect() as conn:
            approved = conn.execute("SELECT asset_id FROM part_approvals WHERE run_id=? AND part_id=? AND project_id=?",
                                    (run_id, int(part_id), project_id)).fetchone()
        if approved:
            existing_version=self.latest_version(approved["asset_id"])
            if existing_version and Path(existing_version["glb_path"]).is_file():
                self._publish_library_copy(approved["asset_id"],existing_version["version_number"])
                return approved["asset_id"]
        result = json.loads(run["result_json"])
        part = next((item for item in result["parts"] if int(item["id"]) == int(part_id)), None)
        if not part:
            raise ValueError("Part not found")
        base = Path(result["output_dir"]).resolve()
        source = (base / part["path"]).resolve()
        if base not in source.parents or not source.is_file():
            raise ValueError("Part mesh is missing or outside its job folder")
        parent = self.get_asset(run["parent_asset_id"]) if run["parent_asset_id"] else None
        if approved:
            aid=approved["asset_id"]
        else:
            aid = self.create_asset({"project_id": project_id, "style_id": style_id, "parent_asset_id": run["parent_asset_id"],
                "name": name, "asset_type": parent["asset_type"] if parent else "Prop", "library_category": "Parts",
                "style_lock": False, "engine": "XPart" if result["settings"].get("reconstruct") else "P3-SAM",
                "target_size": parent["target_size"] if parent else 1.0,
                "unit": parent["unit"] if parent else "m",
                "message": "جار حفظ الجزء المعتمد"})
            # Claim the target before copying files so a failed save retries the same asset.
            with self.connect() as conn:
                conn.execute("INSERT INTO part_approvals(run_id,part_id,project_id,asset_id) VALUES(?,?,?,?)",
                             (run_id, int(part_id), project_id, aid))
        self.create_version(aid, str(source), style_id=style_id, style_lock_result="PART_APPROVED",
            style_lock_notes="Human-approved part; whole-object conformance is not inherited.",
            manifest={"source_asset_id": run["parent_asset_id"], "source_version": result.get("source_version"),
                      "part_job_id": run_id, "part": part, "source_sha256": result.get("source_sha256"),
                      "settings": result["settings"], "approval": {"approved_by": "human", "source": "parts_tab"}})
        self.update_asset(aid,message="جزء معتمد بشريًا؛ قواعد النمط الهندسية تحتاج مراجعة مستقلة")
        return aid

    # ------------------------------------------------------------------
    # Project summary
    # ------------------------------------------------------------------

    def project_summary(self, project_id: str):
        rows = self.list_assets(project_id=project_id)
        counts = {"pending":0,"processing":0,"review":0,"completed":0,"failed":0}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"total":len(rows), **counts}
