"""Markdown-отчёт по проекту: рендер .md и отдача через GET /api/report.

Вынесено из server.py (Handler._report): render_report — чистый рендер
без HTTP и БД, send_report — тонкая HTTP-обвязка над хендлером.
Формат отчёта и заголовков ответа менять нельзя: на него смотрит фронтенд.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from pulse import gitstats
from pulse.gitutil import commit_log_numstat


def fmt(v) -> str:
    return "—" if v is None else f"{v:g}"


def fmt_signed(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ("−" if v < 0 else "±")
    return f"{sign}{abs(v):g}"


def filename_slug(name: str) -> str:
    # slug защищает заголовок Content-Disposition от CRLF/кавычек/не-ASCII
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def resolve_hotspots(app, detail: dict, name: str, days: int) -> list:
    """Хотспоты за период: из кэша evo при стандартных 30 днях,
    иначе пересчёт по git-логу на лету."""
    if days == 30:
        return (detail.get("evo") or {}).get("hotspots") or []
    snaps = app.store.snapshots(name)
    files_map = (snaps[-1].get("metrics") or {}).get("files", {}) if snaps else {}
    path = Path(detail["path"])
    commits = commit_log_numstat(path, days) if (path / ".git").exists() else []
    return gitstats.hotspots(commits, files_map)


def render_report(name: str, detail: dict, days: int, hotspots: list,
                  alert_drop: float, delta30: float | None,
                  now: float | None = None) -> str:
    """Чистый рендер markdown-отчёта по detail из App.project_detail()."""
    series = detail.get("series") or []
    score = series[-1]["score"] if series else None
    subs = (series[-1].get("subs") or {}) if series else {}
    if now is None:
        now = time.time()
    delta7 = detail.get("delta7")
    live = detail.get("live") or {}
    coupling = detail.get("coupling") or {}
    evo = detail.get("evo") or {}

    lines = [
        f"# Code Pulse — {name}",
        "",
        f"- путь: `{detail['path']}`",
        f"- стек: {detail['stack'] or '—'}",
        f"- дата: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- период: {days} дней",
        "",
        "## Итог",
        "",
        f"- скор: {fmt(score)}",
        f"- изменение за 30 дней: {fmt_signed(delta30)}",
        f"- изменение за 7 дней: {fmt_signed(delta7)}"
        + (f" (порог алерта {alert_drop:g})" if detail.get("alert") else ""),
        "",
        "## Подындексы",
        "",
    ]
    for key, label in (("mi", "MI (ремонтопригодность)"),
                       ("cog", "Когнитивная сложность"),
                       ("dup", "Дублирование"),
                       ("arch", "Архитектура"),
                       ("test", "Тесты"),
                       ("todo", "TODO")):
        lines.append(f"- {label}: {fmt(subs.get(key))}")

    lines += ["", "## Хотспоты", ""]
    if hotspots:
        lines += ["| файл | cog | SLOC | коммитов | churn | score |",
                  "|---|---:|---:|---:|---:|---:|"]
        lines += [
            f"| {h.get('file', '')} | {h.get('cog', 0)} | {h.get('sloc', 0)} | "
            f"{h.get('commits', 0)} | {h.get('churn', 0)} | {h.get('score', 0)} |"
            for h in hotspots[:10]
        ]
    else:
        lines.append("Нет коммитов за период.")

    lines += ["", "## Пакеты (Ca / Ce / I / A / D)", ""]
    pkgs = coupling.get("packages") or []
    if pkgs:
        lines += ["| пакет | SLOC | Ca | Ce | I | A | D |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        lines += [
            f"| {p.get('pkg', '')} | {p.get('sloc', 0)} | {p.get('ca', 0)} | "
            f"{p.get('ce', 0)} | {p.get('I', 0):.2f} | {p.get('A', 0):.2f} | "
            f"{p.get('D', 0):.2f} |"
            for p in pkgs
        ]
        lines += ["",
                  (f"Циклов зависимостей: {coupling.get('cycles', 0)} · "
                   f"нарушений (I>0.5 при Ca≥3): {coupling.get('violations', 0)}")]
    else:
        lines.append("Нет данных о пакетах.")

    age = (evo.get("age") or {})
    buckets = age.get("buckets") or {}
    lines += ["", "## Возраст кода", ""]
    if buckets:
        stale = buckets.get("stale", 0)
        lines += [
            f"- < 7 дней: {buckets.get('d7', 0)} SLOC",
            f"- < 30 дней: {buckets.get('d30', 0)} SLOC",
            f"- < 90 дней: {buckets.get('d90', 0)} SLOC",
            (f"- застой (≥ 90 дней): {stale} SLOC"
             f" ({round((age.get('stale_share') or 0) * 100)}%)"),
        ]
    else:
        lines.append("Нет данных.")

    lines += ["", "## Незакоммиченные файлы", ""]
    dirty = live.get("dirty_files") or []
    if dirty:
        lines += [f"- `{f}`" for f in dirty]
    elif live.get("no_git"):
        lines.append("Git-репозитория нет — состояние файлов не отслеживается.")
    else:
        lines.append("Рабочее дерево чистое.")

    return "\n".join(lines) + "\n"


def send_report(handler, app, qs: dict) -> None:
    """GET /api/report?name=&days= — скачиваемый markdown-отчёт по проекту."""
    name = qs.get("name", [""])[0]
    try:
        days = int(qs.get("days", ["30"])[0])
    except ValueError:
        days = 30
    days = max(1, min(365, days))
    detail = app.project_detail(name)
    if detail is None:
        handler._json({"error": "not found"}, 404)
        return
    # всё из БД уже вычитано в detail (копии, лок не держится);
    # ниже — форматирование и, при days != 30, gitstats без кэша
    series = detail.get("series") or []
    now = time.time()
    delta30 = app._delta(series, now, 30)
    hotspots = resolve_hotspots(app, detail, name, days)
    text = render_report(name, detail, days, hotspots, app.alert_drop, delta30, now)
    slug = filename_slug(name)
    data = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/markdown; charset=utf-8")
    handler.send_header("Content-Disposition",
                        f'attachment; filename="code-pulse-{slug}-{days}d.md"')
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
