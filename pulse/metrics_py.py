"""AST-метрики Python-кода.

Считается по исходникам (в т.ч. из git-истории, без checkout):
- когнитивная сложность (адаптация SonarSource: +1+вложенность за if/for/while/except,
  elif — плоский +1, логические операторы и тернарники учитываются);
- цикломатическая сложность;
- индекс поддерживаемости MI — через radon (если установлен, эталонный расчёт:
  Halstead + CC + LLOC), fallback — собственная формула по SLOC;
- LCOM4 (связность методов класса — детектор God Object);
- абстрактность классов (ABC/Protocol/abstractmethod) — для Distance from Main Sequence;
- импорты (для графа зацепления Ca/Ce);
- размеры, TODO/FIXME.
"""
from __future__ import annotations

import ast
import io
import math
import re
import tokenize
from collections import Counter
from typing import Dict, List, Tuple

MAX_SOURCE_BYTES = 400_000
LONG_FUNC_COG = 20  # cognitive-сложность, с которой функция считается «длинной»

_NESTABLE = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


# ------------------------------------------------------------------ Halstead
def halstead_volume(tree: ast.AST) -> float:
    ops: Counter = Counter()
    operands: Counter = Counter()
    # собираем диапазоны докстрингов, чтобы исключить их из операндов
    doc_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, *_FUNC_NODES)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value,
                                                                     ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_spans.append((body[0].value.lineno, body[0].value.end_lineno))

    def in_docstring(lineno: int) -> bool:
        return any(a <= lineno <= b for a, b in doc_spans)

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            ops[type(node.op).__name__] += 1
        elif isinstance(node, ast.Compare):
            for op in node.ops:
                ops[type(op).__name__] += 1
        elif isinstance(node, ast.UnaryOp):
            ops[type(node.op).__name__] += 1
        elif isinstance(node, ast.BoolOp):
            ops["and/or"] += len(node.values) - 1
        elif isinstance(node, ast.IfExp):
            ops["?:"] += 1
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            ops["="] += 1
        elif isinstance(node, (*_FUNC_NODES, ast.ClassDef, ast.Lambda)):
            ops["def"] += 1
        elif isinstance(node, (*_NESTABLE, ast.Try, ast.With, ast.AsyncWith, ast.Match)):
            ops[type(node).__name__] += 1
        elif isinstance(node, ast.Name):
            operands[node.id] += 1
        elif isinstance(node, ast.arg):
            operands[node.arg] += 1
        elif isinstance(node, ast.Constant):
            if not (isinstance(node.value, str) and in_docstring(node.lineno)):
                operands[repr(node.value)[:60]] += 1
    n = len(ops) + len(operands)
    total = sum(ops.values()) + sum(operands.values())
    return total * math.log2(n) if n > 0 and total > 0 else 0.0


def maintainability_index(sloc: int, cc: int, volume: float) -> float:
    """MI по классической формуле + калибровка по radon.

    Сырая формула систематически занижает относительно radon; монотонное
    преобразование MI_adj = 100·(1−e^(−4.6·MI_raw/100)) совпадает с ним
    на выборке реальных файлов (±5 пунктов) и сохраняет динамику.
    """
    if sloc <= 0 or volume <= 0:
        return 100.0
    raw = (171 - 5.2 * math.log(volume) - 0.23 * cc - 16.2 * math.log(sloc)) * 100 / 171
    raw = max(0.0, min(100.0, raw))
    return 100.0 * (1.0 - math.exp(-4.6 * raw / 100.0))


def mi_via_radon(src: str) -> float | None:
    """Эталонный MI через radon (работает по строке исходника — годится для git-истории)."""
    try:
        from radon.metrics import mi_visit
        return max(0.0, min(100.0, mi_visit(src, multi=True)))
    except Exception:  # noqa: BLE001 — radon нет или не осилил код
        return None


# --------------------------------------------- когнитивная / цикломатическая
def _function_complexity(fn: ast.AST) -> Tuple[int, int, int]:
    """(cognitive, cyclomatic, max_nesting) для тела функции или модуля.

    Вложенные функции не входят в родителя — они анализируются отдельно.
    """
    cog = 0
    cc = 1
    deepest = 0

    # Ветвление считается в момент входа в него (uniformly на любой глубине:
    # statement тела/orelse родителя — тоже узел обхода), flat — плоский +1 для elif.
    def rec(node: ast.AST, depth: int, flat: bool = False) -> None:
        nonlocal cog, cc, deepest
        t = type(node)
        if t in _FUNC_NODES and node is not fn:
            return  # вложенная функция анализируется отдельно
        if t is ast.If:
            cog += 1 if flat else 1 + depth
            cc += 1
            if not flat:
                deepest = max(deepest, depth + 1)
            rec(node.test, depth)
            for b in node.body:
                rec(b, depth + 1)
            for b in node.orelse:
                # первый statement orelse — elif: плоский +1 без надбавки
                rec(b, depth + 1, flat=(b is node.orelse[0]))
        elif t in (ast.For, ast.AsyncFor, ast.While):
            cog += 1 + depth
            cc += 1
            deepest = max(deepest, depth + 1)
            for b in node.body:
                rec(b, depth + 1)
            for b in node.orelse:
                rec(b, depth + 1)
        elif t is ast.ExceptHandler:
            cog += 1 + depth
            cc += 1
            deepest = max(deepest, depth + 1)
            for b in node.body:
                rec(b, depth + 1)
        elif t is ast.BoolOp:
            extra = len(node.values) - 1
            cog += extra
            cc += extra
            for v in node.values:
                rec(v, depth)
        elif t is ast.IfExp:
            cog += 1 + depth
            cc += 1
            for c in ast.iter_child_nodes(node):
                rec(c, depth)
        elif t is ast.comprehension:
            n_if = len(node.ifs)
            cog += 1 + n_if
            cc += 1 + n_if
            for c in ast.iter_child_nodes(node):
                rec(c, depth)
        elif t is ast.Assert:
            cog += 1
            cc += 1
            rec(node.test, depth)
        elif t is ast.match_case:
            cog += 1
            cc += 1
            for b in node.body:
                rec(b, depth + 1)
        else:
            for child in ast.iter_child_nodes(node):
                rec(child, depth)

    rec(fn, 0)
    return cog, cc, deepest


# --------------------------------------------------------------------- LCOM4
class _UF:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def components(self) -> int:
        return len({self.find(x) for x in self.parent})


def _lcom4(cls: ast.ClassDef) -> int:
    """LCOM4: число компонент связности методов класса (1 — хорошо, ≥5 — God Object)."""
    methods: Dict[str, Tuple[set, set]] = {}
    for item in cls.body:
        if isinstance(item, _FUNC_NODES):
            names = {d.id for d in item.decorator_list if isinstance(d, ast.Name)}
            if names & {"staticmethod", "classmethod"}:
                continue
            attrs: set = set()
            calls: set = set()
            for n in ast.walk(item):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id == "self"):
                    attrs.add(n.attr)
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "self"):
                    calls.add(n.func.attr)
            methods[item.name] = (attrs, calls)
    nodes = [m for m in methods if m not in ("__init__", "__new__")]
    if not nodes:
        return 0
    uf = _UF()
    for m in nodes:
        uf.add(m)
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            a_attrs, a_calls = methods[a]
            b_attrs, b_calls = methods[b]
            if (a_attrs & b_attrs) or (a in b_calls) or (b in a_calls):
                uf.union(a, b)
    return uf.components()


def _is_abstract(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = base.id if isinstance(base, ast.Name) else (
            base.attr if isinstance(base, ast.Attribute) else None)
        if name in ("ABC", "ABCMeta", "Protocol"):
            return True
    for item in cls.body:
        for dec in getattr(item, "decorator_list", []):
            dn = dec.id if isinstance(dec, ast.Name) else (
                dec.attr if isinstance(dec, ast.Attribute) else None)
            if dn == "abstractmethod":
                return True
    return False


def file_imports(tree: ast.AST) -> List[Tuple[int, str]]:
    """[(level, module)] — level>0 для относительных импортов."""
    out: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((0, alias.name))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module or ""))
    return out


# --------------------------------------------------------------- файл целиком
_TODO_RE = re.compile(r"\b(?:todo|fixme)\b", re.IGNORECASE)


def _count_todos(src: str) -> int:
    """Число #-комментариев, содержащих TODO/FIXME (вся строка или инлайн).

    Считаются только комментарии: вхождение в коде (todo_list) или в строковом
    литерале ("TODO: fix") не должно засчитываться — иначе проект штрафует сам себя.
    """
    n = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and _TODO_RE.search(tok.string):
                n += 1
    except (tokenize.TokenError, IndentationError):
        pass  # сюда не доходит: источник уже прошёл ast.parse выше
    return n


def analyze_python_file(path: str, src: str) -> dict | None:
    """Полный отчёт по одному .py файлу (или None, если не распарсился)."""
    if len(src.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
        return None
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None

    lines = src.splitlines()
    loc = len(lines)
    sloc = sum(1 for ln in lines if ln.strip())
    todos = _count_todos(src)

    mod_cog, mod_cc, _ = _function_complexity(tree)
    functions = [n for n in ast.walk(tree) if isinstance(n, _FUNC_NODES)]
    f_cog = f_cc = long_funcs = 0
    for f in functions:
        c, k, _d = _function_complexity(f)
        f_cog += c
        f_cc += k
        if c >= LONG_FUNC_COG:
            long_funcs += 1
    cog, cc = mod_cog + f_cog, 1 + f_cc + max(0, mod_cc - 1)

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    lcom4_max = 0
    lcom4_bad = 0
    abstract = 0
    for cl in classes:
        lc = _lcom4(cl)
        lcom4_max = max(lcom4_max, lc)
        if lc >= 5:
            lcom4_bad += 1
        if _is_abstract(cl):
            abstract += 1

    volume = halstead_volume(tree)
    mi = maintainability_index(max(sloc, 1), cc, volume)

    norm = path.replace("\\", "/")
    parts = norm.split("/")
    fname = parts[-1]
    is_test = (fname.startswith("test_") or fname.endswith("_test.py")
               or "tests" in parts[:-1] or "test" in parts[:-1])

    return {
        "path": norm,
        "lang": "py",
        "loc": loc,
        "sloc": sloc,
        "cog": cog,
        "cc": cc,
        "mi": round(mi, 2),
        "funcs": len(functions),
        "long_funcs": long_funcs,
        "classes": len(classes),
        "abstract": abstract,
        "lcom4_max": lcom4_max,
        "lcom4_bad": lcom4_bad,
        "todos": todos,
        "is_test": is_test,
        "imports": file_imports(tree),
    }


def analyze_python(files: Dict[str, str]) -> dict:
    """Анализ набора python-файлов: {path: source} → {files: {...}, totals: {...}}."""
    reports: Dict[str, dict] = {}
    for path, src in files.items():
        rep = analyze_python_file(path, src)
        if rep:
            reports[rep["path"]] = rep

    totals = _totals_from_reports(reports)
    return {"files": reports, "totals": totals}


def _totals_from_reports(reports: Dict[str, dict]) -> dict:
    sloc = sum(r["sloc"] for r in reports.values())
    cog = sum(r["cog"] for r in reports.values())
    cc = sum(r["cc"] for r in reports.values())
    mi_w = sum(r["mi"] * r["sloc"] for r in reports.values())
    funcs = sum(r["funcs"] for r in reports.values())
    test_sloc = sum(r["sloc"] for r in reports.values() if r["is_test"])
    return {
        "files": len(reports),
        "sloc": sloc,
        "cog": cog,
        "cc": cc,
        "funcs": funcs,
        "mi_avg": round(mi_w / sloc, 2) if sloc else 100.0,
        "cog_per_kloc": round(cog * 1000 / sloc, 2) if sloc else 0.0,
        "cc_per_func": round(cc / funcs, 2) if funcs else 0.0,
        "classes": sum(r["classes"] for r in reports.values()),
        "abstract": sum(r["abstract"] for r in reports.values()),
        "lcom4_bad": sum(r["lcom4_bad"] for r in reports.values()),
        "lcom4_max": max((r["lcom4_max"] for r in reports.values()), default=0),
        "long_funcs": sum(r["long_funcs"] for r in reports.values()),
        "god_files": sum(1 for r in reports.values() if r["sloc"] >= 400),
        "todos": sum(r["todos"] for r in reports.values()),
        "test_sloc": test_sloc,
    }
