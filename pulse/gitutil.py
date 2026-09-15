"""Тонкая обвязка над git: запуск команд, устойчивые парсеры."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class GitError(RuntimeError):
    pass


def run_git(repo: Path | str, *args: str, timeout: int = 120) -> str:
    """Выполнить git-команду в репозитории, вернуть stdout как текст."""
    cmd = ["git", "-c", "core.quotepath=false", *args]
    proc = subprocess.run(
        cmd, cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def try_git(repo: Path | str, *args: str, timeout: int = 120) -> Optional[str]:
    """Как run_git, но при ошибке возвращает None (не падает)."""
    try:
        return run_git(repo, *args, timeout=timeout)
    except (GitError, subprocess.TimeoutExpired, OSError):
        return None


def commit_log_trees(repo: Path, days: int) -> list[tuple[str, str, int]]:
    """[(sha, tree, ts)] коммитов за последние `days` дней, от старых к новым."""
    out = try_git(repo, "log", f"--since={days}.days", "--format=%H %T %ct", "--reverse")
    if not out:
        return []
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            result.append((parts[0], parts[1], int(parts[2])))
    return result


def commit_log_numstat(repo: Path, days: int) -> list[dict]:
    """Коммиты за `days` дней: [{sha, ts, files: [(path, add, del)]}]. От новых к старым."""
    out = try_git(repo, "log", f"--since={days}.days", "--numstat", "--no-renames",
                  "--format=%x01%H%x1f%ct")
    if not out:
        return []
    commits: list[dict] = []
    for block in out.split("\x01"):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        sha_s, _, ts_s = head.partition("\x1f")
        if not sha_s.strip():
            continue
        files: list[tuple[str, int, int]] = []
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                add, dele, fpath = parts[0], parts[1], "\t".join(parts[2:])
                files.append((
                    fpath,
                    int(add) if add.isdigit() else 0,
                    int(dele) if dele.isdigit() else 0,
                ))
        try:
            commits.append({"sha": sha_s.strip(), "ts": int(ts_s), "files": files})
        except ValueError:
            continue
    return commits
