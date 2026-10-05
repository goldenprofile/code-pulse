import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pulse.store import Store, worktree_id

DAY = 86400


class WorktreeSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_snapshot_per_day(self):
        store = Store(self.db)
        now = time.time()
        for ts, score in ((now - 2 * DAY, 50), (now - DAY, 60), (now, 70), (now, 75)):
            store.save_snapshot("p", worktree_id(ts), int(ts), {"score": score}, {})
        snaps = store.snapshots("p")
        # три разных дня; повторный скан в тот же день обновил точку, а не добавил
        self.assertEqual([s["score"] for s in snaps], [50, 60, 75])
        store.close()

    def test_daily_worktree_is_not_history(self):
        store = Store(self.db)
        store.save_snapshot("p", worktree_id(time.time()), int(time.time()), {"score": 1}, {})
        self.assertEqual(store.snapshot_count(history_only=True), 0)
        store.save_snapshot("p", "a" * 40, int(time.time()), {"score": 1}, {})
        self.assertEqual(store.snapshot_count(history_only=True), 1)
        store.close()

    def test_legacy_worktree_key_is_migrated(self):
        store = Store(self.db)
        store.close()
        ts = int(time.time()) - 3 * DAY
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO snapshots(project, commit_id, ts, score) VALUES('p', 'WORKTREE', ?, 42)",
                     (ts,))
        conn.commit()
        conn.close()

        store = Store(self.db)
        snaps = store.snapshots("p")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["commit_id"], worktree_id(ts))
        self.assertEqual(snaps[0]["score"], 42)
        store.close()


if __name__ == "__main__":
    unittest.main()
