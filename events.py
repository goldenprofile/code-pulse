"""SSE-транспорт /api/events: кадры event:/data:, ретрансляция прогресса, keep-alive.

Вынесено из server.py (Handler._events). Механика подписок
(App.subscribe/emit/unsubscribe) остаётся в App — сюда приходит готовый app.
Формат кадров (event:, data:-строки, retry:) — контракт фронтенда, не менять.
"""
from __future__ import annotations

import json
import queue
import time


def encode_frame(event: str, obj: dict) -> bytes:
    """Один SSE-кадр; payload уходит в data: построчно —
    переводы строк не ломают фрейминг."""
    payload = json.dumps(obj, ensure_ascii=False)
    body = b"".join(b"data: " + ln.encode("utf-8") + b"\n"
                    for ln in payload.split("\n"))
    return b"event: " + event.encode("ascii") + b"\n" + body + b"\n"


def stream_events(handler, app) -> None:
    """SSE-поток: события от emit() + ретрансляция прогресса + keep-alive."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.close_connection = True
    q = app.subscribe()

    def sse(event: str, obj: dict) -> None:
        handler.wfile.write(encode_frame(event, obj))
        handler.wfile.flush()

    last_sent: dict | None = None   # статус, уже отражённый клиенту
    last_activity = time.time()
    try:
        handler.wfile.write(b"retry: 3000\n\n")  # авто-reconnect EventSource
        handler.wfile.flush()
        while True:
            try:
                event, data = q.get(timeout=1)
                sse(event, data)
                last_activity = time.time()
                last_sent = app.status.snapshot()
            except queue.Empty:
                # прогресс скана ретранслируем сами, читая status.snapshot()
                snap = app.status.snapshot()
                if last_sent is None:
                    last_sent = snap
                if snap != last_sent or snap["phase"] != "idle":
                    sse("scan_progress",
                        {"phase": snap["phase"], "done": snap["done"],
                         "total": snap["total"], "current": snap["current"]})
                    last_sent = snap
                    last_activity = time.time()
                elif time.time() - last_activity >= 15:
                    handler.wfile.write(b": ping\n\n")  # keep-alive + детект мёртвого сокета
                    handler.wfile.flush()
                    last_activity = time.time()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # клиент отключился — finally снимет подписку
    finally:
        app.unsubscribe(q)
