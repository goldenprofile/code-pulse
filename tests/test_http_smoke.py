"""Смоук разбитого сервера: реальный serve() в потоке, проверка контрактов через urllib."""
import contextlib
import io
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

import server
from pulse import scanner
from pulse.store import Store
from server import App, serve


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServeSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        root = tmp / "projects"
        root.mkdir()
        cls.port = _free_port()
        cls._store = Store(tmp / "t.db")
        app = App(root, cls._store, scanner.Status())

        # serve() не отдаёт наружу объект сервера — перехватываем для shutdown()
        holder = {}
        ready = threading.Event()
        real = server.ThreadingHTTPServer

        class Capturing(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["server"] = self
                ready.set()

        server.ThreadingHTTPServer = Capturing
        try:
            def run_serve():
                # print("✦ …") в serve() падает на cp1251-stdout — глушим его в потоке
                with contextlib.redirect_stdout(io.StringIO()):
                    serve(app, port=cls.port)

            cls._thread = threading.Thread(target=run_serve, daemon=True)
            cls._thread.start()
            if not ready.wait(timeout=5):  # serve() упал до bind — покажем это как ошибку
                raise RuntimeError("serve() не поднялся за 5с")
            cls._server = holder["server"]
            # обрыв соединения клиентом (WinError 10053) — шум в stderr, не ошибка теста
            cls._server.handle_error = lambda request, client_address: None
        finally:
            server.ThreadingHTTPServer = real

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()
        cls._store.close()
        cls._tmp.cleanup()

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()

    def test_index_is_html(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertTrue(body)

    def test_overview_json_not_empty(self):
        status, headers, body = self.get("/api/overview")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("generated", data)
        self.assertIn("projects", data)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/definitely-not-here")
        self.assertEqual(cm.exception.code, 404)

    def test_refresh_with_bad_origin_is_403(self):
        url = f"http://127.0.0.1:{self.port}/api/refresh"
        req = urllib.request.Request(
            url, data=json.dumps({}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
