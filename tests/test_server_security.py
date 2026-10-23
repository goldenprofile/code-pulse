import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from pulse import scanner
from pulse.store import Store
from server import App, make_handler
from staticfiles import WEB_DIR


class ServerSecurityTest(unittest.TestCase):
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

    def request(self, method, path, body=None, headers=None):
        hdrs = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def post_json(self, path, obj, headers=None):
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return self.request("POST", path, json.dumps(obj), hdrs)

    # ------------------------------------------------------------- Host
    def test_get_with_own_host_is_served(self):
        status, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_get_with_foreign_host_is_rejected(self):
        # DNS rebinding: запрос дошёл до loopback, но Host — чужой домен
        status, _ = self.request("GET", "/api/overview", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_post_with_foreign_host_is_rejected(self):
        status, _ = self.post_json("/api/settings/exclude", {"name": "proj", "excluded": True},
                                   headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    # ------------------------------------------------------------- CSRF
    def test_post_without_json_content_type_is_rejected(self):
        # «простой» кросс-доменный запрос из формы/fetch no-cors
        status, _ = self.request("POST", "/api/backfill", '{"days": 30}', {"Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/refresh")
        self.assertEqual(status, 403)

    def test_post_with_foreign_origin_is_rejected(self):
        status, _ = self.post_json("/api/settings/exclude", {"name": "proj", "excluded": True},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_post_from_own_page_is_served(self):
        status, body = self.post_json("/api/settings/exclude", {"name": "proj", "excluded": True},
                                      headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200, body)
        self.assertIn("proj", self.store.get_excludes())

    # ------------------------------------------------------- валидация
    def test_bad_json_is_400_not_dropped_connection(self):
        status, _ = self.request("POST", "/api/backfill", "{not json",
                                 {"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    # ------------------------------------------------------------- статика
    def test_static_sibling_dir_is_403(self):
        # «webfoo» — сосед «web»: guard на str.startswith(str(web)) его пропускал,
        # и GET /../webfoo/... отдавал файл (is_relative_to требует разделитель)
        sibling = WEB_DIR.parent / "webfoo"
        (sibling / "sub").mkdir(parents=True, exist_ok=True)
        (sibling / "sub" / "secret.txt").write_text("leak", encoding="utf-8")
        try:
            status, body = self.request("GET", "/../webfoo/sub/secret.txt")
        finally:
            shutil.rmtree(sibling, ignore_errors=True)
        self.assertEqual(status, 403)
        self.assertNotIn(b"leak", body)

    def test_non_numeric_days_is_400(self):
        status, _ = self.post_json("/api/backfill", {"days": "много"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
