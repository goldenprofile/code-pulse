"""Тесты вынесенных из server.py чистых функций: report.py, events.py, staticfiles.py."""
import io

from events import encode_frame
from report import filename_slug, fmt, fmt_signed, render_report
from staticfiles import serve_static


# ------------------------------------------------------------ report.py
def test_fmt_and_signed():
    assert fmt(None) == "—"
    assert fmt(7.25) == "7.25"
    assert fmt_signed(None) == "—"
    assert fmt_signed(3.2) == "+3.2"
    assert fmt_signed(-0.5) == "−0.5"
    assert fmt_signed(0) == "±0"


def test_filename_slug_escapes_crlf_and_quotes():
    # CRLF-инъекция и кавычки в Content-Disposition не должны проходить
    assert filename_slug('a\r\nb"c') == "a__b_c"
    assert filename_slug("проект-1.py") == "______-1.py"  # каждый не-ASCII символ → _
    assert filename_slug("ok.name-9") == "ok.name-9"


def test_render_report_sections():
    detail = {
        "path": "D:/proj", "stack": "python", "series": [
            {"ts": 1, "score": 80.0, "subs": {"mi": 90}},
        ],
        "delta7": -2.5, "alert": True, "live": {"dirty_files": ["a.py"]},
        "coupling": {}, "evo": {},
    }
    text = render_report("proj", detail, 30, [], 5.0, delta30=1.5, now=1_700_000_000)
    assert text.startswith("# Code Pulse — proj")
    assert "- скор: 80" in text
    assert "- изменение за 30 дней: +1.5" in text
    assert "- изменение за 7 дней: −2.5 (порог алерта 5)" in text
    assert "Нет коммитов за период." in text
    assert "`a.py`" in text
    assert text.endswith("\n")


# ------------------------------------------------------------ events.py
def test_encode_frame_multiline_payload():
    frame = encode_frame("scan_done", {"msg": "строка1\nстрока2"})
    lines = frame.decode("utf-8").split("\n")
    # json.dumps экранирует \n, поэтому данные — одна data:-строка:
    # фрейминг не ломается даже если бы перевод строки попал в payload
    assert lines[0] == "event: scan_done"
    assert lines[1] == 'data: {"msg": "строка1\\nстрока2"}'
    assert frame.endswith(b"\n\n")


# -------------------------------------------------------- staticfiles.py
class DummyHandler:
    """Стаб хендлера: ловит то, что serve_static отправил бы в сокет."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def _json(self, obj, code=200):
        self.status = code
        self.body = obj

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.out.write(data)


def test_static_serves_index():
    h = DummyHandler()
    serve_static(h, "/")
    assert h.status == 200
    assert h.headers["Content-Type"].startswith("text/html")
    assert h.wfile.getvalue()  # index.html не пустой


def test_static_missing_is_404():
    h = DummyHandler()
    serve_static(h, "/no-such-file.js")
    assert h.status == 404
    assert h.body == {"error": "not found"}


def test_static_path_traversal_is_403():
    h = DummyHandler()
    serve_static(h, "/../server.py")
    assert h.status == 403
    assert h.body == {"error": "forbidden"}
