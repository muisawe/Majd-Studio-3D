import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from majd_studio_3d.store import SCHEMA_VERSION, V9Store

NEW_COLUMNS = ("generation_run_id", "generation_owner", "generation_owner_pid", "failure_kind", "generation_metrics_json")


class GenerationStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.open_store()
        project = self.store.list_projects()[0]
        self.project = project["id"]
        self.style = project["default_style_id"]
        self.asset = self.new_asset("Chair", front="front.png")

    def open_store(self):
        return V9Store(self.root / "db.sqlite3", self.root / "projects", self.root / "library")

    def new_asset(self, name, front=None):
        asset_id = self.store.create_asset({"project_id": self.project, "style_id": self.style, "name": name,
                                            "asset_type": "Prop", "engine": "2.1"})
        if front:
            self.store.update_asset(asset_id, front_path=front)
        return asset_id

    def test_claim_needs_a_ready_asset_and_has_one_winner(self):
        self.assertIsNone(self.store.claim_asset_generation(self.new_asset("No front"), "t", 1))
        barrier = threading.Barrier(2)
        results = []

        def claim(token):
            barrier.wait()
            results.append(self.store.claim_asset_generation(self.asset, token, 1))
        threads = [threading.Thread(target=claim, args=(token,)) for token in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [row for row in results if row is not None]
        self.assertEqual(len(winners), 1)
        row = self.store.get_asset(self.asset)
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["generation_owner"], winners[0]["generation_owner"])
        self.assertTrue(row["generation_run_id"])

    def test_finish_requires_the_owner_and_success_clears_the_run(self):
        run_id = self.store.claim_asset_generation(self.asset, "owner", 1)["generation_run_id"]
        self.assertFalse(self.store.finish_asset_generation(self.asset, "intruder", "review"))
        self.assertTrue(self.store.finish_asset_generation(self.asset, "owner", "failed", failure_kind="oom", message="x"))
        row = self.store.get_asset(self.asset)
        self.assertEqual((row["status"], row["failure_kind"], row["generation_run_id"], row["generation_owner"]),
                         ("failed", "oom", run_id, None))
        self.assertEqual(self.store.claim_asset_generation(self.asset, "owner2", 1)["generation_run_id"], run_id)
        self.assertIsNone(self.store.get_asset(self.asset)["failure_kind"])
        self.assertTrue(self.store.finish_asset_generation(self.asset, "owner2", "review", progress=100))
        self.assertIsNone(self.store.get_asset(self.asset)["generation_run_id"])
        with self.assertRaises(ValueError):
            self.store.finish_asset_generation(self.asset, "owner2", "completed")

    def test_recovery_releases_dead_owners_only(self):
        other = self.new_asset("Lamp", front="front.png")
        run_id = self.store.claim_asset_generation(self.asset, "dead", 1)["generation_run_id"]
        self.store.claim_asset_generation(other, "alive", 2)
        recovered = self.store.recover_interrupted_generations(lambda token, pid: token == "alive")
        self.assertEqual(recovered, [self.asset])
        row = self.store.get_asset(self.asset)
        self.assertEqual((row["status"], row["failure_kind"], row["generation_run_id"]), ("failed", "interrupted", run_id))
        self.assertEqual(self.store.get_asset(other)["status"], "processing")
        # Rows from older code have no owner and are always stale.
        self.store.update_asset(self.asset, status="processing")
        self.assertEqual(self.store.recover_interrupted_generations(lambda token, pid: token is not None), [self.asset])

    def test_requeue_refuses_while_generating_and_starts_a_fresh_run(self):
        self.store.claim_asset_generation(self.asset, "owner", 1)
        with self.assertRaises(ValueError):
            self.store.requeue_asset_generation(self.asset, "again")
        self.store.finish_asset_generation(self.asset, "owner", "failed")
        self.store.update_asset(self.asset, candidates_json="[]", best_glb="x.glb")
        self.store.requeue_asset_generation(self.asset, "again")
        row = self.store.get_asset(self.asset)
        self.assertEqual((row["status"], row["generation_run_id"], row["candidates_json"], row["best_glb"], row["message"]),
                         ("pending", None, None, None, "again"))

    def test_schema98_database_gains_columns_idempotently(self):
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            for column in NEW_COLUMNS:
                conn.execute(f"ALTER TABLE assets DROP COLUMN {column}")
            conn.execute("UPDATE meta SET value='98' WHERE key='schema_version'")
            conn.commit()
        for _ in range(2):
            store = self.open_store()
        with closing(sqlite3.connect(store.db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertTrue(set(NEW_COLUMNS) <= columns)
        self.assertEqual(int(version), SCHEMA_VERSION)
        self.assertEqual(store.get_asset(self.asset)["name"], "Chair")


if __name__ == "__main__":
    unittest.main()
