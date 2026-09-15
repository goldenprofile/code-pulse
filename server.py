"""HTTP-сервер дашборда: статика + JSON API (stdlib, без зависимостей)."""
from __future__ import annotations

import json
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
    def __init__(self, root: Path, store, status: scanner.Status):
        self.root = root
        self.store = store
        self.status = status
        self._git_cache: dict[str, object] = {}
        self._git_cache_ts: float = 0.0

    # ------------------------------------------------------------------ api
    def overview(self) -> dict:
        projects = self.store.projects()
        live_all = self.store.live_all()
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
            delta = None
            if series and len(series) >= 2:
                base = next((s for s in series if s["ts"] <= time.time() - 25 * 86400),
                            series[0])
                if base is not series[-1] and base["score"] is not None \
                        and score is not None:
                    delta = round(score - base["score"], 1)
            out.append({
                "name": name,
                "path": p["path"],
                "is_git": bool(p["is_git"]),
                "stack": p["stack"],
                "score": score,
                "delta30": delta,
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
        return {
            "name": name,
            "path": projects[name]["path"],
            "stack": projects[name]["stack"],
            "live": live,
            "series": series,
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

    # --------------------------------------------------------------- потоки
    def run_refresh(self, names: set | None = None) -> None:
        """Живое сканирование; names — только указанные проекты (вкл. из настроек).

        После скана — быстрый проход бэкфилла: уже посчитанные деревья
        пропускаются, добираются только новые сэмплы (в т.ч. по нововключённому
        проекту — сразу строится история за месяц).
        """
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

    def run_backfill(self, days: int, step: int) -> None:
        try:
            scanner.backfill(self.root, self.store, self.status, days, step,
                             exclude=self.store.get_excludes())
        except Exception as exc:  # noqa: BLE001
            self.status.update(error=str(exc)[:300])
        finally:
            self.invalidate()

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
        def log_message(self, fmt, *args):  # тише в консоль
            pass

        # ------------------------------------------------------------ GET
        def do_GET(self):  # noqa: N802
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
                elif route.startswith("/api/"):
                    self._json({"error": "unknown"}, 404)
                else:
                    self._static(route)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)

        # ----------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            route = urlparse(self.path).path
            if app.status.phase != "idle":
                self._json({"ok": False, "reason": "занят", "status": app.status.snapshot()})
                return
            if route == "/api/refresh":
                threading.Thread(target=app.run_refresh, daemon=True).start()
                self._json({"ok": True})
            elif route == "/api/settings/exclude":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                name = str(body.get("name", ""))
                excluded = bool(body.get("excluded", False))
                result = app.set_excluded(name, excluded)
                self._json(result, 200 if result.get("ok") else 400)
            elif route == "/api/backfill":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                days = int(body.get("days", 30))
                step = int(body.get("step_days", 3))
                threading.Thread(target=app.run_backfill, args=(days, step),
                                 daemon=True).start()
                self._json({"ok": True})
            else:
                self._json({"error": "unknown"}, 404)

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
