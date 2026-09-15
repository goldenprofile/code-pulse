"""Эволюционные метрики из git-истории (Your Code as a Crime Scene).

- хотспоты: сложность файла × частота изменений;
- временное зацепление: файлы, меняющиеся одним коммитом без общей причины;
- churn по неделям;
- code age: доля кода, который не трогали ≥90 дней;
- активность по дням — для рёбер временного зацепления между репозиториями.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List


WEEK = 7 * 86400


def _week_start(ts: int) -> int:
    """Начало ISO-недели (понедельник, 00:00 UTC) для данного ts."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - timedelta(days=dt.weekday())).timestamp())


def churn_weekly(commits: List[dict], weeks: int = 6) -> list[dict]:
    now = int(datetime.now(tz=timezone.utc).timestamp())
    buckets: Dict[int, dict] = {}
    start = _week_start(now) - (weeks - 1) * WEEK
    for w in range(weeks):
        buckets[start + w * WEEK] = {"ts": start + w * WEEK, "add": 0, "del": 0, "commits": 0}
    for c in commits:
        ws = _week_start(c["ts"])
        b = buckets.get(ws)
        if b is None:
            continue
        b["commits"] += 1
        for path, add, dele in c["files"]:
            b["add"] += add
            b["del"] += dele
    return sorted(buckets.values(), key=lambda b: b["ts"])


def hotspots(commits: List[dict], files_map: Dict[str, dict], top: int = 12) -> list[dict]:
    """score = текущая когнитивная сложность файла × число коммитов за период."""
    stats: Dict[str, dict] = {}
    for c in commits:
        for path, add, dele in c["files"]:
            st = stats.setdefault(path, {"commits": 0, "add": 0, "del": 0})
            st["commits"] += 1
            st["add"] += add
            st["del"] += dele
    result = []
    for path, st in stats.items():
        rep = files_map.get(path)
        if not rep or rep.get("is_test"):
            continue
        cog = rep.get("cog", 0)
        sloc = rep.get("sloc", 0)
        result.append({
            "file": path,
            "commits": st["commits"],
            "churn": st["add"] + st["del"],
            "cog": cog,
            "sloc": sloc,
            "score": cog * st["commits"],
        })
    result.sort(key=lambda r: r["score"], reverse=True)
    return result[:top]


def temporal_coupling(commits: List[dict], min_shared: int = 3,
                      share: float = 0.4, top: int = 15) -> list[dict]:
    """Пары файлов, стабильно меняющихся одним коммитом."""
    pair_counts: Counter = Counter()
    file_counts: Counter = Counter()
    for c in commits:
        files = sorted({p for p, _a, _d in c["files"]})
        if not 2 <= len(files) <= 12:
            continue  # мега-коммиты шумят
        for f in files:
            file_counts[f] += 1
        for i, a in enumerate(files):
            for b in files[i + 1:]:
                pair_counts[(a, b)] += 1
    result = []
    for (a, b), n in pair_counts.most_common(top * 3):
        if n < min_shared or n < share * min(file_counts[a], file_counts[b]):
            continue
        result.append({"a": a, "b": b, "shared": n})
        if len(result) >= top:
            break
    return result


def code_age(commits: List[dict], files_map: Dict[str, dict]) -> dict:
    """Доля SLOC по возрасту последнего изменения файла."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if commits:
        last_touch: Dict[str, int] = {}
        for c in commits:  # от новых к старым — первое вхождение и есть последнее
            for path, _a, _d in c["files"]:
                last_touch.setdefault(path, c["ts"])
    else:
        last_touch = {}
    buckets = {"d7": 0, "d30": 0, "d90": 0, "stale": 0}
    for path, rep in files_map.items():
        sloc = rep.get("sloc", 0)
        ts = last_touch.get(path)
        if ts is None:
            buckets["stale"] += sloc  # не менялся за период анализа
        else:
            age_days = (now - ts) / 86400
            key = "d7" if age_days < 7 else "d30" if age_days < 30 else \
                  "d90" if age_days < 90 else "stale"
            buckets[key] += sloc
    total = sum(buckets.values())
    return {
        "buckets": buckets,
        "stale_share": round(buckets["stale"] / total, 3) if total else 0.0,
    }


def activity_dates(commits: List[dict]) -> set:
    """Дни (YYYY-MM-DD), когда были коммиты — для межрепозиторных рёбер."""
    dates = set()
    for c in commits:
        dates.add(datetime.fromtimestamp(c["ts"], tz=timezone.utc).strftime("%Y-%m-%d"))
    return dates


def repo_temporal_edges(activity: Dict[str, set], min_days: int = 4) -> list[dict]:
    """Рёбра между репозиториями: ≥min_days общих активных дней за период."""
    names = sorted(activity)
    edges = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = len(activity[a] & activity[b])
            if shared >= min_days:
                edges.append({"a": a, "b": b, "days": shared})
    edges.sort(key=lambda e: e["days"], reverse=True)
    return edges
