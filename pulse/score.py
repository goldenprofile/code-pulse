"""Сводный скор качества 0..100 и подындексы.

Веса: MI 25%, архитектура (D + циклы) 20%, когнитивная сложность 20%,
дублирование 15%, тесты 10%, TODO 10%.
"""
from __future__ import annotations


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def quality_subscores(totals: dict, coupling: dict | None, dup_ratio: float) -> dict:
    coupling = coupling or {}
    s_mi = _clamp(totals.get("mi_avg", 0))
    # когнитивная сложность: по СРЕДНЕЙ ФУНКЦИИ (Sonar: >15 — беда),
    # а не на KLOC — плотность функций в проектах слишком разная
    cog_f = totals.get("cog_per_func")
    if cog_f is None:
        cog_f = totals.get("cog_per_kloc", 0) / 7  # грубая оценка для старых снапшотов
    s_cog = _clamp(100 - max(0.0, cog_f - 4) * 8)
    s_dup = _clamp(100 - dup_ratio * 500)
    s_arch = _clamp(100 - coupling.get("mean_D", 0) * 90
                    - coupling.get("cycles", 0) * 8
                    - coupling.get("violations", 0) * 4)
    s_test = _clamp(totals.get("test_ratio", 0) * 200)
    s_todo = _clamp(100 - totals.get("todo_per_kloc", 0) * 5)
    score = (0.25 * s_mi + 0.20 * s_cog + 0.20 * s_arch
             + 0.15 * s_dup + 0.10 * s_test + 0.10 * s_todo)
    return {
        "score": round(score, 1),
        "mi": round(s_mi, 1),
        "cog": round(s_cog, 1),
        "dup": round(s_dup, 1),
        "arch": round(s_arch, 1),
        "test": round(s_test, 1),
        "todo": round(s_todo, 1),
    }
