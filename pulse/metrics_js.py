"""Приближённые метрики для JS/TS (без полноценного AST — регэксп + баланс скобок).

Точность ниже, чем у Python-анализатора, но метрики монотонны по «грязности» кода
и консистентны между снапшотами, чего достаточно для графа динамики.
"""
from __future__ import annotations

import re

from pulse.metrics_py import LONG_FUNC_COG

JS_EXTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
MAX_SOURCE_BYTES = 400_000

_FUNC_RE = re.compile(
    r"function\s*\w*\s*\([^)]*\)\s*\{"          # function f() {
    r"|(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"  # стрелочные
    # \b перед именем обязателен: без него lookahead обходится сдвигом на символ
    # («if (x) {» матчился как метод «f», «while» — как «hile»)
    r"|(?:async\s+)?\b(?!(?:if|for|while|switch|catch|function|else|do|return)\b)"
    r"(\w+)\s*\([^)]*\)\s*\{",                                             # методы классов
)
_SKIP_RE = re.compile(
    r"(?P<com>//[^\n]*|/\*.*?\*/)"    # комментарии: строчные и блочные
    r"|(?P<str>'(?:\\.|[^'\\])*'"     # string
    r"|\"(?:\\.|[^\"\\])*\""          # string
    r"|`(?:\\.|[^`\\])*`)",           # template string
    re.DOTALL,
)
_COMPLEXITY_RE = re.compile(r"\b(if|for|while|case|catch)\b|&&|\|\||\?\s")
_IMPORT_RE = re.compile(
    r"import\s+[^'\"]*from\s+['\"]([^'\"]+)['\"]"
    r"|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|export\s+[^'\"]*from\s+['\"]([^'\"]+)['\"]",
)
_TODO_RE = re.compile(r"\b(?:todo|fixme)\b", re.IGNORECASE)


def _strip_noise(src: str) -> str:
    return _SKIP_RE.sub(lambda m: " " * len(m.group(0)), src)


def _count_todos(src: str) -> int:
    """TODO/FIXME только в комментариях (// и /* */).

    Лево-направленный скан комментариев и литералов: // внутри кавычек
    (например в URL) комментарием не считается.
    """
    return sum(1 for m in _SKIP_RE.finditer(src)
               if m.group("com") and _TODO_RE.search(m.group("com")))


def analyze_js_file(path: str, src: str) -> dict | None:
    if len(src.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
        return None

    clean = _strip_noise(src)
    lines = src.splitlines()
    loc = len(lines)
    sloc = sum(1 for ln in lines if ln.strip())
    todos = _count_todos(src)

    funcs = 0
    long_funcs = 0
    cog = 0
    cc = 1
    for m in _FUNC_RE.finditer(clean):
        funcs += 1
        # баланс скобок: срезаем тело функции
        depth = 0
        started = False
        if m.group(0).endswith("=>"):
            # стрелочная: тело в скобках только если «{» сразу после «=>»,
            # иначе find() утащил бы тело следующей функции
            rest = clean[m.end():].lstrip()
            body_start = len(clean) - len(rest) if rest.startswith("{") else -1
        else:
            body_start = clean.find("{", m.start())
        if body_start == -1:
            continue
        i = body_start
        while i < len(clean):
            ch = clean[i]
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
            i += 1
            if started and depth <= 0:
                break
        body = clean[body_start:i]
        f_cc = 1 + len(_COMPLEXITY_RE.findall(body))
        cc += f_cc
        cog += f_cc  # приближение: без учёта вложенности
        if f_cc >= LONG_FUNC_COG:
            long_funcs += 1

    mi = max(0.0, min(100.0, 110 - 0.1 * sloc - 4 * (cc / max(funcs, 1))))

    norm = path.replace("\\", "/")
    parts = norm.split("/")
    fname = parts[-1]
    is_test = (".test." in fname or ".spec." in fname or fname.endswith(".d.ts")
               or "tests" in parts[:-1] or "__tests__" in parts[:-1])

    imports: list[tuple[int, str]] = []
    for m in _IMPORT_RE.finditer(src):
        spec = next((g for g in m.groups() if g), None)
        if spec:
            imports.append((0, spec))

    return {
        "path": norm,
        "lang": "js",
        "loc": loc,
        "sloc": sloc,
        "cog": cog,
        "cc": cc,
        "mi": round(mi, 2),
        "funcs": funcs,
        "long_funcs": long_funcs,
        "classes": 0,
        "abstract": 0,
        "lcom4_max": 0,
        "lcom4_bad": 0,
        "todos": todos,
        "is_test": is_test,
        "imports": imports,
    }


def analyze_js(files: dict[str, str]) -> dict:
    from pulse.metrics_py import _totals_from_reports
    reports: dict[str, dict] = {}
    for path, src in files.items():
        rep = analyze_js_file(path, src)
        if rep:
            reports[rep["path"]] = rep
    return {"files": reports, "totals": _totals_from_reports(reports)}
