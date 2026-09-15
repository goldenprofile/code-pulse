"""Сборка полного анализа дерева исходников (Python + JS/TS)."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Dict

from pulse.coupling import coupling_metrics
from pulse.duplication import duplication_ratio
from pulse.metrics_js import JS_EXTS, analyze_js
from pulse.metrics_py import analyze_python


def analyze_sources(files: Dict[str, str]) -> dict:
    """files: {relative_path: source} → полный словарь метрик проекта/снапшота."""
    py_files = {p: s for p, s in files.items() if p.endswith(".py")}
    js_files = {p: s for p, s in files.items()
                if PurePosixPath(p).suffix.lower() in JS_EXTS}

    file_reports: Dict[str, dict] = {}
    totals = {
        "files": 0, "sloc": 0, "loc": 0, "cog": 0, "cc": 0, "funcs": 0,
        "classes": 0, "abstract": 0, "lcom4_bad": 0, "lcom4_max": 0,
        "long_funcs": 0, "god_files": 0, "todos": 0, "test_sloc": 0,
        "mi_avg": 100.0, "cog_per_kloc": 0.0, "cc_per_func": 0.0,
    }

    for analyzer, part in ((analyze_python, py_files), (analyze_js, js_files)):
        if not part:
            continue
        res = analyzer(part)
        file_reports.update(res["files"])
        for key, val in res["totals"].items():
            if key in ("mi_avg", "cog_per_kloc", "cc_per_func"):
                continue
            totals[key] = totals.get(key, 0) + val

    sloc = totals["sloc"]
    if sloc:
        totals["mi_avg"] = round(
            sum(r["mi"] * r["sloc"] for r in file_reports.values()) / sloc, 2)
        totals["cog_per_kloc"] = round(totals["cog"] * 1000 / sloc, 2)
    if totals["funcs"]:
        totals["cc_per_func"] = round(totals["cc"] / totals["funcs"], 2)
        totals["cog_per_func"] = round(totals["cog"] / totals["funcs"], 2)
    totals["test_ratio"] = round(totals["test_sloc"] / sloc, 3) if sloc else 0.0
    totals["todo_per_kloc"] = round(totals["todos"] * 1000 / sloc, 2) if sloc else 0.0

    dup = duplication_ratio(py_files | js_files)
    coupling = coupling_metrics(file_reports)

    langs: Dict[str, int] = {}
    for rep in file_reports.values():
        langs[rep["lang"]] = langs.get(rep["lang"], 0) + rep["sloc"]

    # облегчённая карта файлов (без imports) — для хотспотов и UI
    files_map = {
        p: {k: v for k, v in r.items() if k != "imports"}
        for p, r in file_reports.items()
    }

    return {
        "totals": totals,
        "langs": langs,
        "dup_ratio": dup,
        "coupling": coupling,
        "files": files_map,
    }
