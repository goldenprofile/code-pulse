"""Гонка «двойной скан» (атомарный try_begin) и набор файлов ворктри/истории."""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from pulse import scanner
from pulse.store import Store
from server import App, make_handler


class TryBeginTest(unittest.TestCase):
    def test_sequential_cas(self):
        st = scanner.Status()
        self.assertTrue(st.try_begin("scan"))
        self.assertFalse(st.try_begin("scan"))      # фаза занята
        self.assertFalse(st.try_begin("backfill"))  # любая занятая фаза блокирует
        st.end()
        self.assertTrue(st.try_begin("backfill"))

    def test_exactly_one_winner(self):
        st = scanner.Status()
        n = 16
        barrier = threading.Barrier(n)
        results = []

        def worker():
            barrier.wait()
            results.append(st.try_begin("scan"))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # check-and-set целиком под замком ⇒ победитель ровно один
        # при любом чередовании потоков, без sleep-гонок на удачу
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), n - 1)

class WorktreeFileSetTest(unittest.TestCase):
    def test_git_repo_respects_gitignore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=d, check=True,
                           capture_output=True)
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "ignored.py").write_text("y = 2\n", encoding="utf-8")
            files = scanner.collect_worktree_files(root)
            self.assertIn("app.py", files)
            self.assertNotIn("ignored.py", files)

    def test_no_git_falls_back_to_walk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.py").write_text("z = 3\n", encoding="utf-8")
            files = scanner.collect_worktree_files(root)
            self.assertIn("app.py", files)
            self.assertNotIn("node_modules/dep.py", files)


if __name__ == "__main__":
    unittest.main()
