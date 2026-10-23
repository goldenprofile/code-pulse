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


class DoubleScanTest(unittest.TestCase):
    """Занятый триггер: честный ответ без 500, второй поток не стартует."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.root = tmp / "projects"
        (cls.root / "proj").mkdir(parents=True)
        cls.store = Store(tmp / "t.db")
        cls.app = App(cls.root, cls.store, scanner.Status())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        # обрыв соединения клиентом (WinError 10053) — шум в stderr, не ошибка теста
        cls.server.handle_error = lambda request, client_address: None
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()
        cls._tmp.cleanup()

    def post(self, path, body=None):
        hdrs = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
                "Connection": "close"}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=json.dumps(body or {}), headers=hdrs)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_busy_post_is_not_500(self):
        self.assertTrue(self.app.status.try_begin("scan"))
        try:
            status, body = self.post("/api/refresh")
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertEqual(body["reason"], "занят")
            self.assertEqual(body["status"]["phase"], "scan")
            # та же вежливость для бэкфилла
            status, body = self.post("/api/backfill", {"days": 7, "step_days": 3})
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertEqual(self.app.status.phase, "scan")  # фазу не сбило
        finally:
            self.app.status.end()

    def test_run_refresh_single_winner(self):
        calls = []
        orig = scanner.scan_all
        entered = threading.Event()
        release = threading.Event()

        def fake_scan_all(*a, **k):
            calls.append(1)
            entered.set()
            release.wait(5)  # победитель ждёт внутри критической секции

        scanner.scan_all = fake_scan_all
        try:
            results = []

            def run():
                results.append(self.app.run_refresh())

            t1 = threading.Thread(target=run)
            t1.start()
            self.assertTrue(entered.wait(5))  # фаза уже занята победителем
            t2 = threading.Thread(target=run)
            t2.start()
            t2.join(5)
            release.set()
            t1.join(5)
            # проверка детерминирована: try_begin второго строго внутри фазы первого
            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(results), [False, True])
        finally:
            scanner.scan_all = orig

    def test_no_idle_window_between_scan_and_backfill(self):
        """CAS-инвариант: run_refresh держит фазу между scan_all и backfill.

        Раньше scan_all сам ставил phase="idle": в окне до конца backfill
        (discover_projects + git-сабпроцессы) второй try_begin проходил.
        """
        seen = []

        def fake_scan_all(*a, **k):
            pass

        def fake_backfill(*a, **k):
            # «окно»: здесь у настоящего backfill идут discover и git-процессы
            seen.append((self.app.status.phase, self.app.status.try_begin("scan")))

        orig = (scanner.scan_all, scanner.backfill)
        scanner.scan_all, scanner.backfill = fake_scan_all, fake_backfill
        try:
            self.assertTrue(self.app.run_refresh(names={"proj"}))
        finally:
            scanner.scan_all, scanner.backfill = orig
        self.assertEqual(seen, [("scan", False)])  # фаза занята, второй триггер проиграл
        self.assertEqual(self.app.status.phase, "idle")

    def test_thread_start_failure_releases_phase(self):
        """RuntimeError от Thread.start() — фаза освобождена, а не занята навечно."""
        real_start = threading.Thread.start

        def flaky_start(thread_self):
            if getattr(thread_self._target, "__name__", "") in ("run_refresh", "run_backfill"):
                raise RuntimeError("can't start new thread")
            return real_start(thread_self)

        threading.Thread.start = flaky_start
        try:
            status, body = self.post("/api/refresh")
            self.assertEqual(status, 500)
            self.assertFalse(body["ok"])
            self.assertEqual(self.app.status.phase, "idle")
            status, body = self.post("/api/backfill", {"days": 7, "step_days": 3})
            self.assertEqual(status, 500)
            self.assertFalse(body["ok"])
            self.assertEqual(self.app.status.phase, "idle")
            status, body = self.post("/api/settings/exclude", {"name": "proj", "excluded": True})
            self.assertEqual(status, 200)  # добавление в исключения — без потока скана
            status, body = self.post("/api/settings/exclude", {"name": "proj", "excluded": False})
            self.assertEqual(status, 500)  # возврат проекта — триггер со start()
            self.assertFalse(body["ok"])
            self.assertEqual(self.app.status.phase, "idle")
        finally:
            threading.Thread.start = real_start


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
