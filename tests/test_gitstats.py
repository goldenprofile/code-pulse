"""Эволюционные метрики: churn по неделям, хотспоты, временное зацепление, code age.

Чистые функции проверяются на синтетических списках коммитов с фиксированными
метками времени; интеграционный кейс — временное git-репо с коммитами,
датированными через GIT_AUTHOR_DATE/GIT_COMMITTER_DATE (без sleep и «сейчас»
в assertions: для code_age/churn берутся возрасты, далёкие от границ 7/30/90).
"""
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pulse import gitutil
from pulse.gitstats import (
    WEEK,
    _week_start,
    churn_weekly,
    code_age,
    hotspots,
    repo_temporal_edges,
    temporal_coupling,
)
from pulse.metrics_py import analyze_python_file

NOW = int(datetime.now(tz=timezone.utc).timestamp())
DAY = 86400
# начало текущей ISO-недели + 1 час: гарантированно внутри «сегодняшнего» бакета
THIS_WEEK = _week_start(NOW) + 3600


def commit(ts: int, *files) -> dict:
    return {"ts": ts, "files": [(p, a, d) for p, a, d in files]}


class WeekStartTest(unittest.TestCase):
    def test_golden_values(self):
        # 1970-01-01 — четверг → начало недели 1969-12-29 (понедельник)
        self.assertEqual(_week_start(0), -3 * DAY)
        # вторник 2024-01-02 → понедельник 2024-01-01 00:00 UTC
        self.assertEqual(_week_start(1704153600), 1704067200)


class ChurnWeeklyTest(unittest.TestCase):
    def test_buckets_aggregate_current_week(self):
        commits = [
            commit(THIS_WEEK, ("a.py", 10, 2)),
            commit(THIS_WEEK + 3600, ("b.py", 5, 0)),
        ]
        buckets = churn_weekly(commits, weeks=6)
        self.assertEqual(len(buckets), 6)
        self.assertEqual([b["ts"] for b in buckets],
                         sorted(b["ts"] for b in buckets))
        last = buckets[-1]
        self.assertEqual((last["commits"], last["add"], last["del"]), (2, 15, 2))
        for b in buckets[:-1]:
            self.assertEqual((b["commits"], b["add"], b["del"]), (0, 0, 0))

    def test_old_commits_outside_window_are_ignored(self):
        buckets = churn_weekly([commit(NOW - 300 * DAY, ("a.py", 99, 99))], weeks=6)
        self.assertEqual(sum(b["commits"] for b in buckets), 0)
        self.assertEqual(sum(b["add"] for b in buckets), 0)

    def test_window_parameter(self):
        self.assertEqual(len(churn_weekly([], weeks=2)), 2)


class HotspotsTest(unittest.TestCase):
    FILES = {
        "a.py": {"cog": 3, "sloc": 10, "is_test": False},
        "b.py": {"cog": 10, "sloc": 5, "is_test": False},
        "t.py": {"cog": 100, "sloc": 50, "is_test": True},
    }

    COMMITS = [
        commit(THIS_WEEK, ("a.py", 100, 0), ("b.py", 10, 0)),
        commit(THIS_WEEK + 60, ("a.py", 5, 5)),
        commit(THIS_WEEK + 120, ("t.py", 50, 0)),      # тест — не хотспот
        commit(THIS_WEEK + 180, ("ghost.py", 9, 9)),   # нет в текущем дереве
    ]

    def test_score_is_cog_times_commits_and_sorted_desc(self):
        result = hotspots(self.COMMITS, self.FILES)
        # b.py (10*1=10) выше a.py (3*2=6), несмотря на вдвое меньше коммитов
        self.assertEqual(result, [
            {"file": "b.py", "commits": 1, "churn": 10, "cog": 10, "sloc": 5,
             "score": 10},
            {"file": "a.py", "commits": 2, "churn": 110, "cog": 3, "sloc": 10,
             "score": 6},
        ])

    def test_tests_and_unknown_files_excluded_top_limit(self):
        many = {f"f{i:03}.py": {"cog": 100, "sloc": 1, "is_test": False}
                for i in range(15)}
        commits = [commit(THIS_WEEK, *[(f, 1, 0) for f in many])]
        self.assertEqual(len(hotspots(commits, many)), 12)  # top по умолчанию
        # t.py и ghost.py из фикстуры выше не попадают в выдачу
        names = {r["file"] for r in hotspots(self.COMMITS, self.FILES)}
        self.assertEqual(names, {"a.py", "b.py"})


class TemporalCouplingTest(unittest.TestCase):
    def test_pairs_golden(self):
        commits = [commit(THIS_WEEK, ("a.py", 1, 0), ("b.py", 1, 0))
                   for _ in range(3)]
        commits.append(commit(THIS_WEEK + 60, ("c.py", 1, 0), ("d.py", 1, 0)))
        commits.append(commit(THIS_WEEK + 120,
                              *((f"e{i:02}.py", 0, 0) for i in range(13))))  # мега-коммит
        result = temporal_coupling(commits)
        # (c.py, d.py): 2 < min_shared=3; мега-коммит (13 файлов) шумит — пропущен
        self.assertEqual(result, [{"a": "a.py", "b": "b.py", "shared": 3}])

    def test_share_filter_drops_weak_pairs(self):
        commits = [commit(THIS_WEEK, ("y.py", 1, 0), ("z.py", 1, 0))
                   for _ in range(6)]
        commits += [commit(THIS_WEEK + 60, ("v.py", 1, 0), ("u.py", 1, 0))
                    for _ in range(3)]
        commits.append(commit(THIS_WEEK + 120, ("y.py", 1, 0), ("v.py", 1, 0)))
        result = temporal_coupling(commits, min_shared=1)
        # (v, y): shared=1 < 0.4 * min(count(y)=7, count(v)=4) = 1.6 → отброшено;
        # пары формируются по отсортированным именам файлов в коммите
        self.assertEqual(result, [
            {"a": "y.py", "b": "z.py", "shared": 6},
            {"a": "u.py", "b": "v.py", "shared": 3},
        ])


class CodeAgeTest(unittest.TestCase):
    def test_buckets_by_last_touch(self):
        commits = [
            commit(NOW - 2 * DAY, ("fresh.py", 1, 0)),
            commit(NOW - 15 * DAY, ("recent.py", 1, 0)),
            commit(NOW - 60 * DAY, ("old.py", 1, 0)),
            commit(NOW - 400 * DAY, ("ancient.py", 1, 0)),
        ]
        files = {name: {"sloc": 10}
                 for name in ("fresh.py", "recent.py", "old.py", "ancient.py")}
        files["frozen.py"] = {"sloc": 10}  # не трогали за весь период
        res = code_age(commits, files)
        self.assertEqual(res["buckets"],
                         {"d7": 10, "d30": 10, "d90": 10, "stale": 20})
        self.assertEqual(res["stale_share"], 0.4)

    def test_no_commits_means_all_stale(self):
        res = code_age([], {"a.py": {"sloc": 5}})
        self.assertEqual(res["buckets"], {"d7": 0, "d30": 0, "d90": 0, "stale": 5})
        self.assertEqual(res["stale_share"], 1.0)

    def test_fully_empty_is_zero(self):
        res = code_age([], {})
        self.assertEqual(res["buckets"], {"d7": 0, "d30": 0, "d90": 0, "stale": 0})
        self.assertEqual(res["stale_share"], 0.0)


class RepoTemporalEdgesTest(unittest.TestCase):
    def test_shared_days_and_sorting(self):
        activity = {
            "a": {1, 2, 3, 4, 5},
            "b": {1, 2, 3, 4, 5},
            "c": {1, 2, 3, 4},
        }
        edges = repo_temporal_edges(activity)
        self.assertEqual([(e["a"], e["b"], e["days"]) for e in edges],
                         [("a", "b", 5), ("a", "c", 4), ("b", "c", 4)])
        self.assertEqual(repo_temporal_edges(activity, min_days=5),
                         [{"a": "a", "b": "b", "days": 5}])


class GitHistoryIntegrationTest(unittest.TestCase):
    """Реальное git-репо с датированными коммитами → numstat → хотспоты."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        repo = Path(cls._tmp.name)
        gitutil.run_git(repo, "init", "-q")
        cls._date_commit(repo, {"app.py": "x = 1\n"}, "2024-01-02T12:00:00+00:00")
        cls._date_commit(repo, {"app.py": "x = 2\n", "util.py": "y = 3\n"},
                         "2024-01-03T12:00:00+00:00")
        cls.repo = repo

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @staticmethod
    def _date_commit(repo: Path, files: dict, date: str) -> None:
        for name, content in files.items():
            (repo / name).write_text(content, encoding="utf-8")
        gitutil.run_git(repo, "add", "-A")
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-q", "-m", "step"],
            cwd=str(repo), check=True, capture_output=True, env=env,
        )

    def test_numstat_to_hotspots(self):
        commits = gitutil.commit_log_numstat(self.repo, days=5000)
        self.assertEqual(len(commits), 2)
        # коммиты от новых к старым; 2024-01-03 12:00 UTC = 1704283200
        self.assertEqual(commits[0]["ts"], 1704283200)
        files_map = {
            "app.py": analyze_python_file("app.py", "x = 2\n"),
            "util.py": analyze_python_file("util.py", "y = 3\n"),
        }
        result = hotspots(commits, files_map)
        self.assertEqual({r["file"]: (r["commits"], r["churn"]) for r in result},
                         {"app.py": (2, 3), "util.py": (1, 1)})

    def test_dated_commits_outside_churn_window(self):
        commits = gitutil.commit_log_numstat(self.repo, days=5000)
        buckets = churn_weekly(commits, weeks=6)
        self.assertEqual(sum(b["commits"] for b in buckets), 0)


if __name__ == "__main__":
    unittest.main()
