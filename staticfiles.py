"""Раздача статики web/ для GET-роутов без префикса /api/.

Вынесено из server.py (Handler._static). Guard от path traversal
(resolve + is_relative_to) — не prefix-проверка: sibling-каталог
вроде webfoo/ рядом с web/ не должен проходить
"""
from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def serve_static(handler, route: str) -> None:
    if route in ("/", "/index.html"):
        path = WEB_DIR / "index.html"
    else:
        rel = route.lstrip("/")
        path = (WEB_DIR / rel).resolve()
        if not path.is_relative_to(WEB_DIR.resolve()):
            handler._json({"error": "forbidden"}, 403)
            return
    if not path.exists():
        handler._json({"error": "not found"}, 404)
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type",
                        CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)
