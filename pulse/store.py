"""SQLite-хранилище: проекты, снапшоты метрик, живое состояние, meta."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    is_git INTEGER NOT NULL DEFAULT 0,
    stack TEXT NOT NULL DEFAULT '',
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    project TEXT NOT NULL,
    commit_id TEXT NOT NULL,          -- tree sha или 'WORKTREE:YYYY-MM-DD'
    ts INTEGER NOT NULL,              -- время состояния (коммита или скана)
    score REAL,
    subscores TEXT,                   -- json
    metrics TEXT,                     -- json: totals, coupling, langs, dup_ratio, files
    PRIMARY KEY (project, commit_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_project_ts ON snapshots(project, ts);
CREATE TABLE IF NOT EXISTS live (
    project TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    state TEXT                        -- json: ветка, dirty, ahead/behind, ...
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


WORKTREE_PREFIX = "WORKTREE:"


def worktree_id(ts: float) -> str:
    """Ключ снапшота рабочего дерева: один на локальный день, повторный скан его обновляет."""
    return WORKTREE_PREFIX + time.strftime("%Y-%m-%d", time.localtime(ts))


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # раньше снапшот рабочего дерева был один на проект ('WORKTREE');
            # переводим его в дневной ключ, чтобы точка осталась в истории
            self._conn.execute(
                "UPDATE OR REPLACE snapshots SET commit_id = ? || date(ts, 'unixepoch', 'localtime') "
                "WHERE commit_id = 'WORKTREE'", (WORKTREE_PREFIX,))
            self._conn.commit()

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def get_excludes(self) -> set:
        try:
            return set(json.loads(self.get_meta("exclude_projects", "[]")))
        except (json.JSONDecodeError, TypeError):
            return set()

    def set_excludes(self, names) -> None:
        self.set_meta("exclude_projects", json.dumps(sorted(names), ensure_ascii=False))

    # ------------------------------------------------------------ projects
    def upsert_project(self, name: str, path: str, is_git: bool, stack: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects(name, path, is_git, stack, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET path=excluded.path, is_git=excluded.is_git, "
                "stack=excluded.stack, updated_at=excluded.updated_at",
                (name, path, int(is_git), stack, int(time.time())))
            self._conn.commit()

    def remove_project(self, name: str) -> None:
        with self._lock:
            for table in ("projects", "live", "snapshots"):
                self._conn.execute(f"DELETE FROM {table} WHERE {'project' if table != 'projects' else 'name'}=?",
                                   (name,))
            self._conn.commit()

    def projects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- snapshots
    def save_snapshot(self, project: str, commit_id: str, ts: int,
                      subscores: dict, metrics: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots(project, commit_id, ts, score, subscores, metrics) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(project, commit_id) DO UPDATE SET "
                "ts=excluded.ts, score=excluded.score, subscores=excluded.subscores, "
                "metrics=excluded.metrics",
                (project, commit_id, ts, subscores.get("score", 0),
                 json.dumps(subscores, ensure_ascii=False),
                 json.dumps(metrics, ensure_ascii=False)))
            self._conn.commit()

    def has_snapshot(self, project: str, commit_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM snapshots WHERE project=? AND commit_id=?",
                (project, commit_id)).fetchone()
            return row is not None

    def snapshots(self, project: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT commit_id, ts, score, subscores, metrics FROM snapshots "
                "WHERE project=? ORDER BY ts", (project,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "commit_id": r["commit_id"],
                "ts": r["ts"],
                "score": r["score"],
                "subscores": json.loads(r["subscores"] or "{}"),
                "metrics": json.loads(r["metrics"] or "{}"),
            })
        return out

    def snapshot_count(self, history_only: bool = True) -> int:
        with self._lock:
            q = "SELECT COUNT(*) AS n FROM snapshots"
            if history_only:
                q += " WHERE commit_id NOT LIKE 'WORKTREE%'"
            return self._conn.execute(q).fetchone()["n"]

    # ---------------------------------------------------------------- live
    def save_live(self, project: str, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO live(project, ts, state) VALUES(?,?,?) "
                "ON CONFLICT(project) DO UPDATE SET ts=excluded.ts, state=excluded.state",
                (project, int(time.time()), json.dumps(state, ensure_ascii=False)))
            self._conn.commit()

    def live_all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT project, ts, state FROM live").fetchall()
        return {r["project"]: {**json.loads(r["state"] or "{}"), "scanned_at": r["ts"]}
                for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
