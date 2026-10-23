"""HTTP-сервер дашборда: статика + JSON API (stdlib, без зависимостей)."""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from events import stream_events
from pulse import gitstats, scanner
from pulse.gitutil import commit_log_numstat
from report import send_report
from staticfiles import serve_static


class App:
    def __init__(self, root: Path, store, status: scanner.Status,
                 alert_drop: float = 5.0):
        self.root = root
        self.store = store
        self.status = status
        self.alert_drop = alert_drop  # порог алерта деградации за 7 дней
        self._git_cache: dict[str, object] = {}
        self._git_cache_ts: float = 0.0
        # SSE-подписчики: очередь на каждое открытое соединение /api/events
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()

    # ------------------------------------------------------------------ api
    @staticmethod
    def _delta(series: list[dict], now: float, days: int) -> float | None:
        """Дельта скоринга «за days дней»: последний снапшот минус база.

        База — первый снапшот старше (days-5) дней (шаг бэкфилла ~3 дня,
        поэтому база ищется чуть внутри периода); при короткой истории —
        самый первый снапшот. Нужно ≥2 снапшота и оба score не None.
        """
        if len(series) < 2:
            return None
        horizon = now - max(days - 5, 1) * 86400
        base = next((s for s in series if s["ts"] <= horizon), series[0])
        if base is series[-1] or base["score"] is None \
                or series[-1]["score"] is None:
            return None
        return round(series[-1]["score"] - base["score"], 1)

    @staticmethod
    def _delta7(series: list[dict], now: float) -> float | None:
        """Дельта строго за окно 7 дней: последний свежий снапшот минус первый.

        Свежие = снапшоты с ts >= now - 7*86400; нужно ≥2 снапшота
        и оба score не None, иначе None.
        """
        fresh = [s for s in series if s["ts"] >= now - 7 * 86400]
        if len(fresh) >= 2 and fresh[0]["score"] is not None \
                and fresh[-1]["score"] is not None:
            return round(fresh[-1]["score"] - fresh[0]["score"], 1)
        return None

    def _alert(self, delta7: float | None) -> bool:
        return delta7 is not None and delta7 <= -self.alert_drop

    def overview(self) -> dict:
        projects = self.store.projects()
        live_all = self.store.live_all()
        now = time.time()
        out = []
        for p in projects:
            name = p["name"]
            snaps = self.store.snapshots(name)
            series = [
                {"ts": s["ts"], "score": s["score"],
                 "subs": s["subscores"], "totals": _brief(s["metrics"]),
                 "coupling": _brief_coupling(s["metrics"])}
                for s in snaps
            ]
            score = series[-1]["score"] if series else None
            delta = self._delta(series, now, 30)
            delta7 = self._delta7(series, now)
            out.append({
                "name": name,
                "path": p["path"],
                "is_git": bool(p["is_git"]),
                "stack": p["stack"],
                "score": score,
                "delta30": delta,
                "delta7": delta7,
                "alert": self._alert(delta7),
                "series": series,
                "live": live_all.get(name),
            })
        out.sort(key=lambda x: (x["score"] is None, x["score"] or 0))
        return {"generated": int(time.time()), "projects": out}

    def project_detail(self, name: str) -> dict | None:
        projects = {p["name"]: p for p in self.store.projects()}
        if name not in projects:
            return None
        live = self.store.live_all().get(name)
        snaps = self.store.snapshots(name)
        series = []
        for s in snaps:
            m = s["metrics"] or {}
            series.append({
                "ts": s["ts"],
                "score": s["score"],
                "subs": s["subscores"],
                "totals": _brief(m),
                "coupling": _brief_coupling(m),
                "commit": s["commit_id"][:8],
            })

        # эволюционные метрики считаем на лету по текущему worktree (кэш 5 мин)
        key = f"detail:{name}"
        cached = self._git_cache.get(key)
        if isinstance(cached, tuple) and time.time() - cached[0] < 300:
            evo = cached[1]
        else:
            path = Path(projects[name]["path"])
            files_map = {}
            if snaps:
                files_map = (snaps[-1].get("metrics") or {}).get("files", {})
            commits = commit_log_numstat(path, 30) if (path / ".git").exists() else []
            evo = {
                "hotspots": gitstats.hotspots(commits, files_map),
                "temporal": gitstats.temporal_coupling(commits),
                "churn_weeks": gitstats.churn_weekly(commits),
                "age": gitstats.code_age(commits, files_map),
                "commits_30d": len(commits),
            }
            self._git_cache[key] = (time.time(), evo)

        latest = snaps[-1] if snaps else None
        coupling = (latest.get("metrics") or {}).get("coupling") if latest else None
        files = (latest.get("metrics") or {}).get("files", {}) if latest else {}
        worst_files = sorted(
            ({"file": p, **{k: r.get(k) for k in ("sloc", "cog", "cc", "mi", "lcom4_max")}}
             for p, r in files.items()),
            key=lambda f: f.get("cog", 0), reverse=True)[:12]
        delta7 = self._delta7(series, time.time())
        return {
            "name": name,
            "path": projects[name]["path"],
            "stack": projects[name]["stack"],
            "live": live,
            "series": series,
            "delta7": delta7,
            "alert": self._alert(delta7),
            "coupling": coupling,
            "evo": evo,
            "worst_files": worst_files,
        }

    def graph(self) -> dict:
        now = time.time()
        if now - self._git_cache_ts > 600:
            self._refresh_graph_cache()
        projects = self.store.projects()
        activity: dict[str, set] = self._git_cache.get("activity", {})
        nodes = []
        for p in projects:
            snaps = self.store.snapshots(p["name"])
            score = snaps[-1]["score"] if snaps else None
            metrics = (snaps[-1].get("metrics") or {}) if snaps else {}
            totals = metrics.get("totals", {})
            coupling = metrics.get("coupling", {})
            mi = totals.get("mi_avg", 100)
            sloc = totals.get("sloc", 0)
            debt = round((100 - mi) * (1 + sloc / 10000), 1)
            nodes.append({
                "name": p["name"],
                "score": score,
                "debt": debt,
                "sloc": sloc,
                "cycles": coupling.get("cycles", 0),
                "stack": p["stack"],
                "is_git": bool(p["is_git"]),
            })
        edges = gitstats.repo_temporal_edges(activity, min_days=4)
        return {"nodes": nodes, "edges": edges}

    def _refresh_graph_cache(self) -> None:
        activity: dict[str, set] = {}
        for p in self.store.projects():
            path = Path(p["path"])
            if not (path / ".git").exists():
                continue
            commits = commit_log_numstat(path, 30)
            dates = set()
            from datetime import datetime, timezone
            for c in commits:
                dates.add(datetime.fromtimestamp(c["ts"], tz=timezone.utc)
                          .strftime("%Y-%m-%d"))
            activity[p["name"]] = dates
        self._git_cache["activity"] = activity
        self._git_cache_ts = time.time()

    def invalidate(self) -> None:
        self._git_cache.clear()
        self._git_cache_ts = 0.0

    # ------------------------------------------------------------------ SSE
    def subscribe(self) -> queue.Queue:
        """Зарегистрировать нового подписчика /api/events."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, event: str, data: dict) -> None:
        """Разослать событие подписчикам.

        Медленный клиент (переполненная очередь) получает дроп события,
        но не блокирует поток скана; без подписчиков — мгновенный выход,
        поэтому фоновый скан из run.py ничего не стоит.
        """
        with self._subs_lock:
            subs = list(self._subs)
        if not subs:
            return
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    # --------------------------------------------------------------- потоки
    def run_refresh(self, names: set | None = None,
                    *, acquired: bool = False) -> bool:
        """Живое сканирование; names — только указанные проекты (вкл. из настроек).

        acquired=True — вызывающий уже занял фазу через status.try_begin()
        (HTTP-обработчик занимает её синхронно, до ответа клиенту).
        Возвращает False, если скан/бэкфилл уже идёт — второй запуск не начат.

        После скана — быстрый проход бэкфилла: уже посчитанные деревья
        пропускаются, добираются только новые сэмплы (в т.ч. по нововключённому
        проекту — сразу строится история за месяц).
        """
        if not acquired and not self.status.try_begin("scan"):
            return False
        self.emit("scan_started",
                  {"status": {**self.status.snapshot(), "phase": "scan"}})
        try:
            scanner.scan_all(self.root, self.store, self.status,
                             self.store.get_excludes(), names=names)
            if names:
                scanner.backfill(self.root, self.store, self.status,
                                 exclude=self.store.get_excludes())
        except Exception as exc:  # noqa: BLE001
            self.status.update(error=str(exc)[:300])
        finally:
            # end() в finally: при ошибке посреди скана фаза не останется занятой
            self.status.end()
            self.invalidate()
            self.emit("scan_done", {"status": self.status.snapshot()})
            self.emit("project_updated", {"names": sorted(names) if names else []})
        return True

    def run_backfill(self, days: int, step: int,
                     *, acquired: bool = False) -> bool:
        """Бэкфилл истории; False — операция уже идёт (см. run_refresh)."""
        if not acquired and not self.status.try_begin("backfill"):
            return False
        self.emit("scan_started",
                  {"status": {**self.status.snapshot(), "phase": "backfill"}})
        try:
            scanner.backfill(self.root, self.store, self.status, days, step,
                             exclude=self.store.get_excludes())
        except Exception as exc:  # noqa: BLE001
            self.status.update(error=str(exc)[:300])
        finally:
            self.status.end()
            self.invalidate()
            self.emit("scan_done", {"status": self.status.snapshot()})
            self.emit("project_updated", {"names": []})
        return True

    # ------------------------------------------------------------- настройки
    def settings(self) -> dict:
        """Список проектов на диске + текущие исключения."""
        excludes = self.store.get_excludes()
        known = {p["name"]: p for p in self.store.projects()}
        items = []
        for d in scanner.discover_projects(self.root, set()):
            item = {
                "name": d.name,
                "excluded": d.name in excludes,
                "is_git": (d / ".git").exists(),
                "tracked": d.name in known,
            }
            if item["tracked"]:
                item["stack"] = known[d.name]["stack"]
                snaps = self.store.snapshots(d.name)
                if snaps:
                    item["score"] = snaps[-1]["score"]
            items.append(item)
        items.sort(key=lambda x: (x["excluded"], x["name"].lower()))
        return {"root": str(self.root), "excludes": sorted(excludes), "projects": items}

    def set_excluded(self, name: str, excluded: bool) -> dict:
        d = self.root / name
        if not d.is_dir() or d.parent != self.root:
            return {"ok": False, "reason": "нет такого каталога в корне"}
        excludes = self.store.get_excludes()
        if excluded and name not in excludes:
            excludes.add(name)
            self.store.set_excludes(excludes)
            self.store.remove_project(name)  # мгновенно исчезает с дашборда
            self.invalidate()
        elif not excluded and name in excludes:
            excludes.discard(name)
            self.store.set_excludes(excludes)
            # фазу занимает триггер, а не фоновый поток — иначе гонка двойного скана
            started = self.status.try_begin("scan")
            if started:
                thread = threading.Thread(target=self.run_refresh, args=({name},),
                                          kwargs={"acquired": True}, daemon=True)
                try:
                    thread.start()
                except Exception:
                    # start() сорвался (например, нет потоков): без end() фаза
                    # останется занятой до рестарта — освободим и пробросим
                    self.status.end()
                    raise
            return {"ok": True, "excludes": sorted(excludes),
                    "scan_started": started}
        return {"ok": True, "excludes": sorted(excludes)}


def _brief(metrics: dict | None) -> dict:
    if not metrics:
        return {}
    t = metrics.get("totals", {})
    keep = ("sloc", "cog_per_kloc", "cc_per_func", "mi_avg", "god_files",
            "long_funcs", "lcom4_bad", "todos", "test_ratio", "funcs", "files")
    out = {k: t.get(k) for k in keep if t.get(k) is not None}
    out["dup_pct"] = round(metrics.get("dup_ratio", 0) * 100, 1)
    return out


def _brief_coupling(metrics: dict | None) -> dict:
    if not metrics:
        return {}
    c = metrics.get("coupling", {})
    return {k: c.get(k) for k in ("mean_I", "mean_D", "cycles", "violations", "pkg_count")}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # все ответы шлют Content-Length; SSE — стрим без длины
        timeout = 60  # keep-alive-соединение не должно держать поток вечно

        def log_message(self, fmt, *args):  # тише в консоль
            pass

        # ------------------------------------------------------- защита
        def _host_ok(self) -> bool:
            """Host — только loopback с нашим портом: отсекает DNS rebinding.

            Чужой домен, перепривязанный на 127.0.0.1, приходит со своим Host —
            без этой проверки его страница читала бы API (пути, ветки, файлы).
            """
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _post_ok(self) -> bool:
            """POST — только JSON и только со своей страницы: защита от CSRF.

            Требование application/json делает запрос «непростым» — браузер шлёт
            preflight OPTIONS, который сервер не обслуживает, и чужая страница
            не может отправить его вслепую. Origin, если пришёл, обязан быть нашим.
            """
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.lower() == f"http://{self.headers.get('Host', '').lower()}"

        # ------------------------------------------------------------ GET
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                self._json({"error": "forbidden host"}, 403)
                return
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                if route == "/api/overview":
                    self._json(app.overview())
                elif route == "/api/project":
                    name = parse_qs(parsed.query).get("name", [""])[0]
                    detail = app.project_detail(name)
                    if detail is None:
                        self._json({"error": "not found"}, 404)
                    else:
                        self._json(detail)
                elif route == "/api/graph":
                    self._json(app.graph())
                elif route == "/api/status":
                    self._json(app.status.snapshot())
                elif route == "/api/settings":
                    self._json(app.settings())
                elif route == "/api/events":
                    self._events()
                elif route == "/api/report":
                    self._report(parse_qs(parsed.query))
                elif route.startswith("/api/"):
                    self._json({"error": "unknown"}, 404)
                else:
                    self._static(route)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)

        # ----------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            if not self._host_ok() or not self._post_ok():
                self._json({"error": "forbidden"}, 403)
                return
            route = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise TypeError("body must be a JSON object")
                if route == "/api/refresh":
                    # try_begin атомарен: второй триггер получает «занят»,
                    # а не запускает параллельный скан (и не ловит 500)
                    if not app.status.try_begin("scan"):
                        self._json({"ok": False, "reason": "занят",
                                    "status": app.status.snapshot()})
                        return
                    thread = threading.Thread(target=app.run_refresh,
                                              kwargs={"acquired": True}, daemon=True)
                    try:
                        thread.start()
                    except Exception:
                        # RuntimeError от start() — иначе фаза занята до рестарта
                        app.status.end()
                        raise
                    self._json({"ok": True})
                elif route == "/api/settings/exclude":
                    name = str(body.get("name", ""))
                    excluded = bool(body.get("excluded", False))
                    result = app.set_excluded(name, excluded)
                    self._json(result, 200 if result.get("ok") else 400)
                elif route == "/api/backfill":
                    # границы как у /api/report: без них один запрос мог запустить
                    # бэкфилл на сотни лет по всем репозиториям
                    days = max(1, min(365, int(body.get("days", 30))))
                    step = max(1, min(30, int(body.get("step_days", 3))))
                    if not app.status.try_begin("backfill"):
                        self._json({"ok": False, "reason": "занят",
                                    "status": app.status.snapshot()})
                        return
                    thread = threading.Thread(target=app.run_backfill, args=(days, step),
                                              kwargs={"acquired": True}, daemon=True)
                    try:
                        thread.start()
                    except Exception:
                        # RuntimeError от start() — иначе фаза занята до рестарта
                        app.status.end()
                        raise
                    self._json({"ok": True})
                else:
                    self._json({"error": "unknown"}, 404)
            except (ValueError, TypeError) as exc:  # битый JSON / нечисловые days
                self._json({"ok": False, "error": f"bad request: {exc}"}, 400)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)

        # -------------------------------------------------------- отчёт .md
        def _report(self, qs: dict) -> None:
            """GET /api/report?name=&days= — скачиваемый markdown-отчёт по проекту."""
            send_report(self, app, qs)

        # ------------------------------------------------------------ SSE
        def _events(self) -> None:
            """SSE-поток: события от emit() + ретрансляция прогресса + keep-alive."""
            stream_events(self, app)

        # -------------------------------------------------------- helpers
        def _json(self, obj, code: int = 200) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, route: str) -> None:
            """Статика из web/: mime-типы и path-traversal guard — в staticfiles.py."""
            serve_static(self, route)

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"✦ Code Pulse: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
