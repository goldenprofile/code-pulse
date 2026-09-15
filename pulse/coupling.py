"""Архитектурное зацепление по графу внутренних зависимостей.

Для каждого пакета проекта (первый уровень каталога с учётом src-layout):
- Ca (afferent) — сколько пакетов от него зависят;
- Ce (efferent) — от скольких пакетов он зависит;
- Instability I = Ce / (Ca + Ce) — домен должен стремиться к 0, UI к 1;
- Abstractness A — доля абстрактных классов (ABC/Protocol/abstractmethod);
- Distance from main sequence D = |A + I - 1| — баланс абстрактности и стабильности;
- циклические зависимости (SCC размером ≥2) — главный запах архитектуры.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List

SRC_ROOTS = {"src", "lib", "app", "sources"}


def pkg_of_dir(parts: List[str]) -> str:
    parts = [p for p in parts if p not in ("", ".")]
    if parts and parts[0] in SRC_ROOTS and len(parts) > 1:
        parts = parts[1:]
    return parts[0] if parts else "."


def pkg_of_file(path: str) -> str:
    parts = path.replace("\\", "/").split("/")[:-1]
    return pkg_of_dir(parts)


def _resolve_py(file_pkg_parts: List[str], level: int, module: str,
                known: set) -> str | None:
    """Резолв python-импорта в имя внутреннего пакета (или None — внешний)."""
    base = list(file_pkg_parts)
    if level > 0:
        up = level - 1
        if up > len(base):
            return None
        base = base[:len(base) - up] if up else base
        if module:
            base += module.split(".")
    else:
        if not module:
            return None
        base = module.split(".")
    pkg = pkg_of_dir(base)
    return pkg if pkg in known else None


def _resolve_js(file_dir_parts: List[str], spec: str, known: set) -> str | None:
    if not spec.startswith("."):
        pkg = spec.split("/")[0].lstrip("@")
        return pkg if pkg in known else None
    parts = list(file_dir_parts)
    rest = spec
    while rest.startswith("./") or rest.startswith("../") or rest in (".", ".."):
        if rest.startswith("../"):
            parts = parts[:-1] if parts else parts
            rest = rest[3:]
        elif rest.startswith("./"):
            rest = rest[2:]
        elif rest == "..":
            parts = parts[:-1] if parts else parts
            rest = ""
        else:
            rest = ""
    if rest:
        parts += [p for p in rest.split("/") if p]
    pkg = pkg_of_dir(parts)
    return pkg if pkg in known else None


def _scc_count(nodes: set, edges: Dict[str, set]) -> int:
    """Число компонент сильной связности размера ≥2 (алгоритм Косарайю, итеративно)."""
    if not nodes:
        return 0
    radj: Dict[str, set] = defaultdict(set)
    for u, vs in edges.items():
        for v in vs:
            radj[v].add(u)
    # 1-й проход: порядок завершения
    visited: set = set()
    order: list = []
    for start in nodes:
        if start in visited:
            continue
        stack = [(start, iter(edges.get(start, ())))]
        visited.add(start)
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt in nodes and nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(edges.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    # 2-й проход по обратному графу
    visited2: set = set()
    scc_big = 0
    for start in reversed(order):
        if start in visited2:
            continue
        comp: list = []
        stack = [start]
        visited2.add(start)
        while stack:
            node = stack.pop()
            comp.append(node)
            for nxt in radj.get(node, ()):
                if nxt in nodes and nxt not in visited2:
                    visited2.add(nxt)
                    stack.append(nxt)
        if len(comp) >= 2:
            scc_big += 1
    return scc_big


def coupling_metrics(reports: Dict[str, dict]) -> dict:
    """reports: {path: file_report} (из analyze_python/analyze_js)."""
    known = {pkg_of_file(p) for p in reports}
    known.discard(".")

    pkg_classes: Counter = Counter()
    pkg_abstract: Counter = Counter()
    pkg_sloc: Counter = Counter()
    edges_out: Dict[str, set] = defaultdict(set)
    edge_count: Counter = Counter()

    for path, rep in reports.items():
        pkg = pkg_of_file(path)
        pkg_classes[pkg] += rep.get("classes", 0)
        pkg_abstract[pkg] += rep.get("abstract", 0)
        pkg_sloc[pkg] += rep.get("sloc", 0)
        dir_parts = path.replace("\\", "/").split("/")[:-1]
        for level, module in rep.get("imports", []):
            if rep.get("lang") == "py":
                target = _resolve_py(dir_parts, level, module, known)
            else:
                target = _resolve_js(dir_parts, module, known)
            if target and target != pkg:
                edges_out[pkg].add(target)
                edge_count[(pkg, target)] += 1

    edges_in: Dict[str, set] = defaultdict(set)
    for src, targets in edges_out.items():
        for t in targets:
            edges_in[t].add(src)

    packages = []
    i_vals, d_vals = [], []
    for pkg in sorted(known):
        ce, ca = len(edges_out.get(pkg, ())), len(edges_in.get(pkg, ()))
        instability = ce / (ca + ce) if (ca + ce) else 0.0
        classes = pkg_classes.get(pkg, 0)
        abstractness = (pkg_abstract.get(pkg, 0) / classes) if classes else 0.0
        dist = abs(abstractness + instability - 1.0)
        i_vals.append(instability)
        d_vals.append(dist)
        packages.append({
            "pkg": pkg,
            "sloc": pkg_sloc.get(pkg, 0),
            "ca": ca,
            "ce": ce,
            "I": round(instability, 3),
            "A": round(abstractness, 3),
            "D": round(dist, 3),
            "classes": classes,
        })

    connected = {p for p in known if edges_out.get(p) or edges_in.get(p)}
    simple_edges = {u: set(vs) for u, vs in edges_out.items()}
    cycles = _scc_count(connected, simple_edges)
    violations = sum(1 for p in packages if p["ca"] >= 3 and p["I"] > 0.5)

    has_deps = bool(edge_count)
    return {
        "packages": packages,
        # если между пакетами нет ни одной зависимости — метрика vacuous,
        # считаем её нейтральной (не наказываем изолированные проекты)
        "mean_I": round(sum(i_vals) / len(i_vals), 3) if i_vals else 0.0,
        "mean_D": round(sum(d_vals) / len(d_vals), 3) if (d_vals and has_deps) else 0.0,
        "cycles": cycles,
        "violations": violations,
        "pkg_count": len(known),
        "has_deps": has_deps,
        "edges": [[u, v, n] for (u, v), n in edge_count.most_common(60)],
    }
