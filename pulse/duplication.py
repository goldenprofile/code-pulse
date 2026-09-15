"""Оценка дублирования кода: скользящее окно нормализованных строк (по умолчанию 6).

Первое вхождение блока не считается дубликатом; метрика — доля строк,
попавших хотя бы в один повторившийся блок. Приближение, но стабильное
между снапшотами.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict

WINDOW = 6


def _normalize_lines(src: str, is_python: bool) -> list[str]:
    out = []
    for raw in src.splitlines():
        s = raw.strip().lower()
        if not s:
            continue
        if is_python and (s.startswith("#") or s.startswith('"""') or s.startswith("'''")):
            continue
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        out.append(s)
    return out


def duplication_ratio(files: Dict[str, str]) -> float:
    counts: Counter = Counter()
    normalized: Dict[str, list[str]] = {}
    for path, src in files.items():
        lines = _normalize_lines(src, path.endswith(".py"))
        normalized[path] = lines
        for i in range(len(lines) - WINDOW + 1):
            counts[hash("\n".join(lines[i:i + WINDOW]))] += 1

    dup = 0
    total = 0
    for lines in normalized.values():
        total += len(lines)
        flags = [False] * len(lines)
        for i in range(len(lines) - WINDOW + 1):
            if counts[hash("\n".join(lines[i:i + WINDOW]))] >= 2:
                for j in range(i, i + WINDOW):
                    flags[j] = True
        dup += sum(flags)
    return round(dup / total, 4) if total else 0.0
