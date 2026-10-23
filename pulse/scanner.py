"""Сканер: обнаружение проектов, живое git-состояние, анализ воркерами, бэкфилл."""
from __future__ import annotations

import io
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional

from pulse.analyze import analyze_sources
from pulse.gitutil import commit_log_trees, try_git
from pulse.score import quality_subscores
from pulse.store import worktree_id

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".ruff_cache", "__pycache__", "dist", "build", ".idea", ".vscode",
    "target", "vendor", "coverage", ".next", ".nuxt", ".pytest_cache",
    "migrations", "staticfiles", "media", "bower_components", ".pixi",
}
SOURCE_EXTS = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
MAX_FILE_BYTES = 400_000
MAX_FILES = 1200


def looks_generated(src: str) -> bool:
    """Минифицированные бандлы и сгенерированный код — не анализируем."""
    n_lines = src.count("\n")
    if n_lines == 0:
        return False
    avg = len(src) / n_lines
    return avg > 250 or (len(src) > 50_000 and n_lines < 120)


# ------------------------------------------------------------ обнаружение
def discover_projects(root: Path, exclude_names: set[str] | None = None) -> list[Path]:
    excl = DEFAULT_EXCLUDE_DIRS | (exclude_names or set())
    projects = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in excl or child.name.startswith("."):
            continue
        projects.append(child)
    return projects


def collect_worktree_files(root: Path) -> Dict[str, str]:
    files: Dict[str, str] = {}
    count = 0

    def walk(dir_path: Path, prefix: str) -> None:
        nonlocal count
        try:
            entries = sorted(dir_path.iterdir())
        except OSError:
            return
        for entry in entries:
            if count >= MAX_FILES:
                return
            name = entry.name
            if entry.is_dir():
                if name in DEFAULT_EXCLUDE_DIRS or name.startswith("."):
                    continue
                walk(entry, f"{prefix}{name}/")
            elif entry.is_file():
                ext = entry.suffix.lower()
                if ext not in SOURCE_EXTS or ".min." in name:
                    continue
                try:
                    if entry.stat().st_size > MAX_FILE_BYTES:
                        continue
                    src = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if looks_generated(src):
                    continue
                files[f"{prefix}{name}"] = src
                count += 1

    walk(root, "")
    return files


def stack_of(root: Path) -> str:
    marks = []
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() \
            or (root / "setup.py").exists():
        marks.append("python")
    if (root / "package.json").exists():
        marks.append("node")
    if (root / "go.mod").exists():
        marks.append("go")
    if (root / "Cargo.toml").exists():
        marks.append("rust")
    if (root / "docker-compose.yml").exists() or (root / "Dockerfile").exists():
        marks.append("docker")
    return ", ".join(marks) if marks else "—"


# --------------------------------------------------------------- воркеры
def worker_analyze(task: tuple) -> dict:
    """Задача для пула: (repo_path_str, tree_or_None) → метрики снапшота."""
    repo_str, tree = task
    repo = Path(repo_str)
    if tree:
        files = _archive_files(repo, tree)
    else:
        files = collect_worktree_files(repo)
    if not files:  # пустое дерево — хранить в истории нечего
        return {"task": task, "metrics": None, "n_files": 0}
    metrics = analyze_sources(files)
    return {"task": task, "metrics": metrics, "n_files": len(files)}


def _archive_files(repo: Path, tree: str) -> Dict[str, str]:
    """Файлы git-дерева без checkout: git archive → tar → память."""
    proc = subprocess.run(["git", "archive", "--format=tar", tree],
                          cwd=str(repo), capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {tree[:8]}: {proc.stderr.decode()[:200]}")
    files: Dict[str, str] = {}
    count = 0
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        for member in tf:
            if count >= MAX_FILES:
                break
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/")
            if "/.git/" in name or any(
                    f"/{d}/" in f"/{name}" for d in DEFAULT_EXCLUDE_DIRS):
                continue
            parts = name.split("/")
            if parts[0].startswith("."):
                continue
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if ext not in SOURCE_EXTS or ".min." in name:
                continue
            if member.size > MAX_FILE_BYTES:
                continue
            fobj = tf.extractfile(member)
            if fobj is None:
                continue
            text = fobj.read().decode("utf-8", errors="replace")
            if looks_generated(text):
                continue
            files[name] = text
            count += 1
    return files


# ---------------------------------------------------------- live-состояние
def live_state(repo: Path) -> Optional[dict]:
    if not (repo / ".git").exists():
        return None
    branch = try_git(repo, "symbolic-ref", "--short", "HEAD")
    detached = False
    if not branch:
        branch = (try_git(repo, "rev-parse", "--short", "HEAD") or "?")[:7]
        detached = True

    status_out = try_git(repo, "status", "--porcelain=v1", "-b") or ""
    ahead = behind = 0
    dirty: Dict[str, int] = {"M": 0, "A": 0, "D": 0, "?": 0, "U": 0}
    dirty_files: list[str] = []
    for line in status_out.splitlines():
        if line.startswith("##"):
            if "..." in line:
                ab = line.split("...", 1)[1]
                for part in ab.replace("[", " ").replace("]", " ").split():
                    if part.startswith("+") and part[1:].isdigit():
                        ahead = int(part[1:])
                    elif part.startswith("-") and part[1:].isdigit():
                        behind = int(part[1:])
            continue
        if not line.strip():
            continue
        x, y = (line[0], line[1]) if len(line) >= 2 else ("?", " ")
        fname = line[3:].strip().strip('"')
        if x == "?" or y == "?":
            dirty["?"] += 1
        elif x == "U" or y == "U" or x == "A" and y == "A":
            dirty["U"] += 1
        elif x in ("M", "R", "C") or y in ("M", "R", "C"):
            dirty["M"] += 1
        elif x == "A" or y == "A":
            dirty["A"] += 1
        elif x == "D" or y == "D":
            dirty["D"] += 1
        if len(dirty_files) < 40:
            dirty_files.append(fname)
    dirty_total = sum(dirty.values())

    last = try_git(repo, "log", "-1", "--format=%h%x1f%ct%x1f%s") or ""
    last_hash, _, rest = last.partition("\x1f")
    last_ts_s, _, last_msg = rest.partition("\x1f")
    try:
        last_ts = int(last_ts_s)
    except ValueError:
        last_ts = 0

    def rev_count(*args: str) -> int:
        out = try_git(repo, "rev-list", "--count", *args)
        try:
            return int((out or "0").strip() or 0)
        except ValueError:
            return 0

    stash_out = try_git(repo, "stash", "list") or ""
    return {
        "branch": branch.strip(),
        "detached": detached,
        "dirty_total": dirty_total,
        "dirty": dirty,
        "dirty_files": dirty_files,
        "ahead": ahead,
        "behind": behind,
        "has_upstream": "..." in (status_out.splitlines()[0] if status_out else ""),
        "last_commit": {
            "hash": last_hash.strip(),
            "ts": last_ts,
            "msg": last_msg.strip()[:120],
        },
        "commits_7d": rev_count("--since=7.days", "HEAD"),
        "commits_30d": rev_count("--since=30.days", "HEAD"),
        "stash": len([x for x in stash_out.splitlines() if x.strip()]),
    }


# -------------------------------------------------------------- сканирование
class Status:
    """Общий статус фоновых операций (для /api/status)."""

    def __init__(self) -> None:
        self.phase = "idle"          # idle | scan | backfill
        self.done = 0
        self.total = 0
        self.current = ""
        self.started = 0.0
        self.error = ""
        self._lock = threading.Lock()

    def update(self, **kw) -> None:
        with self._lock:
            self.__dict__.update(kw)

    def try_begin(self, phase: str) -> bool:
        """Атомарно занять фазу (compare-and-set): False — операция уже идёт.

        Занимать фазу должен триггер (до старта фонового потока), иначе два
        одновременных запуска оба успеют увидеть idle и начнут параллельный скан.
        """
        with self._lock:
            if self.phase != "idle":
                return False
            self.phase = phase
            return True

    def end(self) -> None:
        """Освободить фазу по завершении фоновой операции."""
        with self._lock:
            self.phase = "idle"
            self.current = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase, "done": self.done, "total": self.total,
                "current": self.current, "started": self.started,
                "error": self.error,
            }


def _subs_for(metrics: dict) -> dict:
    return quality_subscores(
        metrics.get("totals", {}), metrics.get("coupling"), metrics.get("dup_ratio", 0))


def scan_all(root: Path, store, status: Status, exclude: set | None = None,
             workers: int = 6, names: set | None = None,
             cleanup: bool = True) -> dict:
    """Живое сканирование проектов.

    names — если задано, сканируются только эти проекты (без очистки БД);
    cleanup — убрать из БД исключённые и исчезнувшие каталоги.
    Фазу занимает/освобождает обёртка (App.run_refresh) через try_begin/end —
    здесь только прогресс, иначе CAS-инвариант фазы нарушается.
    """
    status.update(done=0, error="", started=time.time())
    projects = discover_projects(root, exclude)
    if names:
        projects = [p for p in projects if p.name in names]
        cleanup = False
    tasks = [(str(p), None) for p in projects]
    status.update(total=len(tasks), current="")
    results: Dict[str, dict] = {}
    if tasks:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker_analyze, t): t for t in tasks}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    res = fut.result()
                    results[Path(t[0]).name] = res
                except Exception as exc:  # noqa: BLE001 — фиксируем и идём дальше
                    results[Path(t[0]).name] = {"metrics": None, "error": str(exc)}
                status.update(done=status.done + 1, current=Path(t[0]).name)

    now = int(time.time())
    summary = {}
    for proj_dir in projects:
        name = proj_dir.name
        res = results.get(name, {})
        metrics = res.get("metrics")
        is_git = (proj_dir / ".git").exists()
        store.upsert_project(name, str(proj_dir), is_git, stack_of(proj_dir))
        state = live_state(proj_dir)
        if state is None:
            state = {"branch": "—", "no_git": True, "dirty_total": 0,
                     "dirty": {}, "dirty_files": [], "ahead": 0, "behind": 0,
                     "has_upstream": False,
                     "last_commit": {"hash": "", "ts": 0, "msg": ""},
                     "commits_7d": 0, "commits_30d": 0, "stash": 0}
        store.save_live(name, state)
        if metrics:
            subs = _subs_for(metrics)
            store.save_snapshot(name, worktree_id(now), now, subs, metrics)
            summary[name] = {"score": subs["score"], "sloc": metrics["totals"]["sloc"]}
        else:
            summary[name] = {"score": None, "sloc": 0,
                             "error": res.get("error", "нет исходников")}

    # убираем из БД исключённые конфигом и исчезнувшие каталоги
    if cleanup:
        keep = {p.name for p in projects}
        for row in store.projects():
            if row["name"] not in keep or not Path(row["path"]).exists():
                store.remove_project(row["name"])
    status.update(current="", done=len(tasks), total=len(tasks))
    return summary


def backfill(root: Path, store, status: Status, days: int = 30, step_days: int = 3,
             exclude: set[str] | None = None, workers: int = 6) -> int:
    """Бэкфилл истории: метрики по сэмплам git-деревьев за `days` дней.

    Возвращает число новых снапшотов.
    Фазу занимает/освобождает обёртка (App.run_backfill) через try_begin/end —
    здесь только прогресс, иначе CAS-инвариант фазы нарушается.
    """
    status.update(done=0, error="", started=time.time())
    tasks: list[tuple] = []
    now = int(time.time())
    targets = [now - days * 86400 + k * step_days * 86400
               for k in range(days // step_days + 1)]

    for proj in discover_projects(root, exclude):
        repo = proj / ".git"
        if not repo.exists():
            continue
        commits = commit_log_trees(proj, days)
        if not commits:
            continue
        picked = {}
        for target_ts in targets:
            for sha, tree, ts in commits:  # от старых к новым
                if ts >= target_ts:
                    picked[tree] = (sha, ts)
                    break
        for tree, (_sha, ts) in picked.items():
            if not store.has_snapshot(proj.name, tree):
                tasks.append((str(proj), tree, ts))
    status.update(total=len(tasks), current="")
    done = 0
    if tasks:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker_analyze, (t[0], t[1])): t for t in tasks}
            for fut in as_completed(futures):
                t = futures[fut]
                proj_name = Path(t[0]).name
                try:
                    res = fut.result()
                    if res.get("metrics") is not None:
                        subs = _subs_for(res["metrics"])
                        store.save_snapshot(proj_name, t[1], t[2], subs, res["metrics"])
                except Exception as exc:  # noqa: BLE001
                    status.update(error=str(exc)[:300])
                done += 1
                status.update(done=done, current=proj_name)
    status.update(current="")
    return done
