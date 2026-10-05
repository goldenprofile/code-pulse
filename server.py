"""HTTP-сервер дашборда: статика + JSON API (stdlib, без зависимостей)."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pulse import gitstats, scanner
from pulse.gitutil import commit_log_numstat

WEB_DIR = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


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
    def run_refresh(self, names: set | None = None) -> None:
        """Живое сканирование; names — только указанные проекты (вкл. из настроек).

        После скана — быстрый проход бэкфилла: уже посчитанные деревья
        пропускаются, добираются только новые сэмплы (в т.ч. по нововключённому
        проекту — сразу строится история за месяц).
        """
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
            self.invalidate()
            self.emit("scan_done", {"status": self.status.snapshot()})
            self.emit("project_updated", {"names": sorted(names) if names else []})

    def run_backfill(self, days: int, step: int) -> None:
        self.emit("scan_started",
                  {"status": {**self.status.snapshot(), "phase": "backfill"}})
        try:
            scanner.backfill(self.root, self.store, self.status, days, step,
                             exclude=self.store.get_excludes())
        except Exception as exc:  # noqa: BLE001
            self.status.update(error=str(exc)[:300])
        finally:
            self.invalidate()
            self.emit("scan_done", {"status": self.status.snapshot()})
            self.emit("project_updated", {"names": []})

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
            threading.Thread(target=self.run_refresh, args=({name},),
                             daemon=True).start()
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
            if app.status.phase != "idle":
                self._json({"ok": False, "reason": "занят", "status": app.status.snapshot()})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise TypeError("body must be a JSON object")
                if route == "/api/refresh":
                    threading.Thread(target=app.run_refresh, daemon=True).start()
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
                    threading.Thread(target=app.run_backfill, args=(days, step),
                                     daemon=True).start()
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
            name = qs.get("name", [""])[0]
            try:
                days = int(qs.get("days", ["30"])[0])
            except ValueError:
                days = 30
            days = max(1, min(365, days))
            detail = app.project_detail(name)
            if detail is None:
                self._json({"error": "not found"}, 404)
                return
            # всё из БД уже вычитано в detail (копии, лок не держится);
            # ниже — форматирование и, при days != 30, gitstats без кэша
            series = detail.get("series") or []
            score = series[-1]["score"] if series else None
            subs = (series[-1].get("subs") or {}) if series else {}
            now = time.time()
            delta30 = App._delta(series, now, 30)
            delta7 = detail.get("delta7")
            live = detail.get("live") or {}
            coupling = detail.get("coupling") or {}
            evo = detail.get("evo") or {}
            if days == 30:
                hotspots = evo.get("hotspots") or []
            else:
                # произвольный период: пересчёт на лету; снапшоты (копии из БД)
                # вычитаны ДО тяжёлого git-вызова
                snaps = app.store.snapshots(name)
                files_map = (snaps[-1].get("metrics") or {}).get("files", {}) if snaps else {}
                path = Path(detail["path"])
                commits = commit_log_numstat(path, days) if (path / ".git").exists() else []
                hotspots = gitstats.hotspots(commits, files_map)

            def fmt(v) -> str:
                return "—" if v is None else f"{v:g}"

            def fmt_signed(v) -> str:
                if v is None:
                    return "—"
                sign = "+" if v > 0 else ("−" if v < 0 else "±")
                return f"{sign}{abs(v):g}"

            lines = [
                f"# Code Pulse — {name}",
                "",
                f"- путь: `{detail['path']}`",
                f"- стек: {detail['stack'] or '—'}",
                f"- дата: {time.strftime('%Y-%m-%d %H:%M')}",
                f"- период: {days} дней",
                "",
                "## Итог",
                "",
                f"- скор: {fmt(score)}",
                f"- изменение за 30 дней: {fmt_signed(delta30)}",
                f"- изменение за 7 дней: {fmt_signed(delta7)}"
                + (f" (порог алерта {app.alert_drop:g})" if detail.get("alert") else ""),
                "",
                "## Подындексы",
                "",
            ]
            for key, label in (("mi", "MI (ремонтопригодность)"),
                               ("cog", "Когнитивная сложность"),
                               ("dup", "Дублирование"),
                               ("arch", "Архитектура"),
                               ("test", "Тесты"),
                               ("todo", "TODO")):
                lines.append(f"- {label}: {fmt(subs.get(key))}")

            lines += ["", "## Хотспоты", ""]
            if hotspots:
                lines += ["| файл | cog | SLOC | коммитов | churn | score |",
                          "|---|---:|---:|---:|---:|---:|"]
                lines += [
                    f"| {h.get('file', '')} | {h.get('cog', 0)} | {h.get('sloc', 0)} | "
                    f"{h.get('commits', 0)} | {h.get('churn', 0)} | {h.get('score', 0)} |"
                    for h in hotspots[:10]
                ]
            else:
                lines.append("Нет коммитов за период.")

            lines += ["", "## Пакеты (Ca / Ce / I / A / D)", ""]
            pkgs = coupling.get("packages") or []
            if pkgs:
                lines += ["| пакет | SLOC | Ca | Ce | I | A | D |",
                          "|---|---:|---:|---:|---:|---:|---:|"]
                lines += [
                    f"| {p.get('pkg', '')} | {p.get('sloc', 0)} | {p.get('ca', 0)} | "
                    f"{p.get('ce', 0)} | {p.get('I', 0):.2f} | {p.get('A', 0):.2f} | "
                    f"{p.get('D', 0):.2f} |"
                    for p in pkgs
                ]
                lines += ["",
                          f"Циклов зависимостей: {coupling.get('cycles', 0)} · "
                          f"нарушений (I>0.5 при Ca≥3): {coupling.get('violations', 0)}"]
            else:
                lines.append("Нет данных о пакетах.")

            age = (evo.get("age") or {})
            buckets = age.get("buckets") or {}
            lines += ["", "## Возраст кода", ""]
            if buckets:
                stale = buckets.get("stale", 0)
                lines += [
                    f"- < 7 дней: {buckets.get('d7', 0)} SLOC",
                    f"- < 30 дней: {buckets.get('d30', 0)} SLOC",
                    f"- < 90 дней: {buckets.get('d90', 0)} SLOC",
                    f"- застой (≥ 90 дней): {stale} SLOC"
                    f" ({round((age.get('stale_share') or 0) * 100)}%)",
                ]
            else:
                lines.append("Нет данных.")

            lines += ["", "## Незакоммиченные файлы", ""]
            dirty = live.get("dirty_files") or []
            if dirty:
                lines += [f"- `{f}`" for f in dirty]
            elif live.get("no_git"):
                lines.append("Git-репозитория нет — состояние файлов не отслеживается.")
            else:
                lines.append("Рабочее дерево чистое.")

            text = "\n".join(lines) + "\n"
            # slug защищает заголовок Content-Disposition от CRLF/кавычек/не-ASCII
            slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="code-pulse-{slug}-{days}d.md"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ------------------------------------------------------------ SSE
        def _events(self) -> None:
            """SSE-поток: события от emit() + ретрансляция прогресса + keep-alive."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            q = app.subscribe()

            def sse(event: str, obj: dict) -> None:
                # payload уходит в data: построчно — переводы строк не ломают фрейминг
                payload = json.dumps(obj, ensure_ascii=False)
                body = b"".join(b"data: " + ln.encode("utf-8") + b"\n"
                                for ln in payload.split("\n"))
                self.wfile.write(b"event: " + event.encode("ascii") + b"\n" + body + b"\n")
                self.wfile.flush()

            last_sent: dict | None = None   # статус, уже отражённый клиенту
            last_activity = time.time()
            try:
                self.wfile.write(b"retry: 3000\n\n")  # авто-reconnect EventSource
                self.wfile.flush()
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
                            self.wfile.write(b": ping\n\n")  # keep-alive + детект мёртвого сокета
                            self.wfile.flush()
                            last_activity = time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # клиент отключился — finally снимет подписку
            finally:
                app.unsubscribe(q)

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
            if route in ("/", "/index.html"):
                path = WEB_DIR / "index.html"
            else:
                rel = route.lstrip("/")
                path = (WEB_DIR / rel).resolve()
                if not str(path).startswith(str(WEB_DIR.resolve())):
                    self._json({"error": "forbidden"}, 403)
                    return
            if not path.exists():
                self._json({"error": "not found"}, 404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"✦ Code Pulse: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
