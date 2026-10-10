"""Daily and pre-migration SQLite snapshots taken at startup; failures are logged, never raised."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("majd.backup")
DAILY_KEEP = 14
PREMIGRATION_KEEP = 5


def _schema_version(db_path: Path):
    try:
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _snapshot(source: Path, destination: Path) -> None:
    """Copy through SQLite's backup API, verify it, then move it into place."""
    partial = destination.with_name(destination.name + ".partial")
    try:
        with closing(sqlite3.connect(source)) as live, closing(sqlite3.connect(partial)) as saved:
            live.backup(saved)
            if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError(f"Backup of {source} failed quick_check")
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()


def _prune(directory: Path, pattern: str, keep: int) -> None:
    for old in sorted(directory.glob(pattern))[:-keep]:
        try:
            old.unlink()
        except OSError as exc:
            LOG.warning("Could not remove old backup %s: %s", old, exc)


def backup_database(db_path, backup_dir, schema_version: int, now: datetime | None = None) -> list[Path]:
    """Take today's snapshot once, plus one before a schema upgrade; return the files created."""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    created = []
    try:
        if not db_path.is_file():
            return created
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now()).strftime("%Y%m%d")
        current = _schema_version(db_path)
        if current is not None and current < schema_version:
            premigration = backup_dir / f"premigration-{stamp}-schema{current}-to-{schema_version}.sqlite3"
            if not premigration.exists():
                _snapshot(db_path, premigration)
                created.append(premigration)
        daily = backup_dir / f"daily-{stamp}.sqlite3"
        if not daily.exists():
            _snapshot(db_path, daily)
            created.append(daily)
        _prune(backup_dir, "daily-*.sqlite3", DAILY_KEEP)
        _prune(backup_dir, "premigration-*.sqlite3", PREMIGRATION_KEEP)
        for path in created:
            LOG.info("Database snapshot saved to %s", path)
    except (OSError, sqlite3.Error) as exc:
        LOG.warning("Database backup failed: %s", exc)
    return created
