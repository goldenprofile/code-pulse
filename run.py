#!/usr/bin/env python
"""Code Pulse — дашборд проектов с телеметрией качества кода.

    python run.py              # старт: скан + бэкфилл (если пусто) + веб-интерфейс
    python run.py --no-backfill
    python run.py --rescan     # только живое сканирование, без сервера
    python run.py --backfill-only --days 30
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from pulse import scanner  # noqa: E402
from pulse.store import Store  # noqa: E402
from server import App, serve  # noqa: E402

CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_CONFIG = {
    "root": "D:/Projects",
    "exclude_projects": ["node_modules"],
    "port": 8787,
    "days": 30,
    "step_days": 3,
    "auto_open": True,
    "alerts": {"score_drop_7d": 5},
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Code Pulse dashboard")
    ap.add_argument("--rescan", action="store_true", help="только скан, без сервера")
    ap.add_argument("--backfill-only", action="store_true",
                    help="только бэкфилл истории, без сервера")
    ap.add_argument("--no-backfill", action="store_true",
                    help="не бэкфиллить историю при первом запуске")
    ap.add_argument("--days", type=int, default=None, help="глубина истории, дней")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    days = args.days or cfg["days"]
    root = Path(cfg["root"])
    if not root.exists():
        print(f"✗ Корневой каталог не найден: {root}")
        sys.exit(1)

    store = Store(ROOT_DIR / "data" / "code-pulse.db")
    status = scanner.Status()
    # исключения теперь живут в БД и правятся через интерфейс (⚙);
    # config.json — только первичный сид при первом запуске
    if store.get_meta("exclude_projects") is None:
        store.set_excludes(set(cfg.get("exclude_projects", [])))
    # порог алерта деградации: ключ alerts в config.json опционален,
    # иначе берётся значение из DEFAULT_CONFIG
    alert_drop = float((cfg.get("alerts") or {}).get("score_drop_7d", 5))
    app = App(root, store, status, alert_drop=alert_drop)

    def console_status() -> None:
        while status.phase != "idle":
            s = status.snapshot()
            print(f"\r  {s['phase']}: {s['done']}/{s['total']} {s['current'][:40]:<40}",
                  end="", flush=True)
            threading.Event().wait(0.5)
        s = status.snapshot()
        print(f"\r  готово: {s['done']}/{max(s['total'], 1)}" + " " * 30)

    if not store.projects():
        print(f"✦ Скан проектов в {root} …")
        threading.Thread(target=app.run_refresh, daemon=True).start()
        console_status()

    if not args.no_backfill and store.snapshot_count(history_only=True) == 0:
        print(f"✦ Бэкфилл истории за {days} дней (шаг {cfg['step_days']} дн.) …")
        threading.Thread(target=app.run_backfill, args=(days, cfg["step_days"]),
                         daemon=True).start()
        console_status()

    if args.rescan:
        print("✦ Живое сканирование …")
        app.run_refresh()
        for info in sorted(app.overview()["projects"], key=lambda x: x["score"] or 0):
            live = info.get("live") or {}
            dirty = live.get("dirty_total", 0)
            flag = f" ⚠{dirty}" if dirty else ""
            print(f"  {info['name']:<28} score={info['score']!s:>5}{flag}")
        return
    if args.backfill_only:
        app.run_backfill(days, cfg["step_days"])
        print("✦ Бэкфилл завершён.")
        return

    port = args.port or cfg["port"]
    # фоновое живое сканирование при каждом старте — так пишется ежедневный снапшот
    threading.Thread(target=app.run_refresh, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"✦ Code Pulse → {url}  (Ctrl+C — выход)")
    if cfg.get("auto_open", True):
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    serve(app, port=port)


if __name__ == "__main__":
    main()
