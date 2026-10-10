import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from majd_studio_3d.db_backup import DAILY_KEEP, backup_database


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "majd_v9.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO meta VALUES('schema_version','98')")
            conn.execute("CREATE TABLE assets(id TEXT)")
            conn.execute("INSERT INTO assets VALUES('a1')")
            conn.commit()
        self.backups = self.root / "backups"

    def test_daily_snapshot_once_and_premigration_copy(self):
        day = datetime(2026, 10, 10)
        created = backup_database(self.db, self.backups, 99, now=day)
        self.assertEqual(sorted(p.name for p in created),
                         ["daily-20261010.sqlite3", "premigration-20261010-schema98-to-99.sqlite3"])
        with closing(sqlite3.connect(created[0])) as conn:
            self.assertEqual(conn.execute("SELECT id FROM assets").fetchall(), [("a1",)])
        self.assertEqual(backup_database(self.db, self.backups, 99, now=day), [])
        self.assertEqual(backup_database(self.db, self.backups, 98, now=day + timedelta(days=1)),
                         [self.backups / "daily-20261011.sqlite3"])
        self.assertFalse(list(self.backups.glob("*.partial")))

    def test_keeps_only_recent_daily_snapshots(self):
        start = datetime(2026, 1, 1)
        for offset in range(DAILY_KEEP + 3):
            backup_database(self.db, self.backups, 98, now=start + timedelta(days=offset))
        daily = sorted(p.name for p in self.backups.glob("daily-*.sqlite3"))
        self.assertEqual(len(daily), DAILY_KEEP)
        self.assertEqual(daily[0], "daily-20260104.sqlite3")

    def test_missing_or_unreadable_database_never_raises(self):
        self.assertEqual(backup_database(self.root / "missing.sqlite3", self.backups, 99), [])
        broken = self.root / "broken.sqlite3"
        broken.write_bytes(b"not a database")
        self.assertEqual(backup_database(broken, self.backups, 99), [])


if __name__ == "__main__":
    unittest.main()
