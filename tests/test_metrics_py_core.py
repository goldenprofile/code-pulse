"""Ядро python-метрик: когнитивная/цикломатическая сложность, MI, LCOM4, импорты, TODO.

Золотые значения посчитаны вручную по правилам из докстринга pulse/metrics_py.py:
- когнитивная (адаптация SonarSource): if/for/while/except дают +1+вложенность,
  elif — плоский +1, логические операторы (n-1), тернарник +1+вложенность,
  comprehension 1+число if, assert +1, match_case +1; вложенные функции отдельно;
- цикломатическая файла: cc = 1 + сумма cc функций + max(0, cc модуля - 1),
  где cc функции = 1 + число ветвлений;
- MI: raw = (171 - 5.2*ln(V) - 0.23*cc - 16.2*ln(SLOC))*100/171,
  MI = 100*(1 - e^(-4.6*raw/100)) (калибровка по radon).
"""
import ast
import unittest

from pulse.metrics_py import (
    _function_complexity,
    analyze_python,
    analyze_python_file,
    halstead_volume,
    maintainability_index,
)


def cog_cc(src: str) -> tuple[int, int]:
    rep = analyze_python_file("a.py", src)
    assert rep is not None  # в тестах источник всегда парсится
    return rep["cog"], rep["cc"]


class CognitiveCyclomaticTest(unittest.TestCase):
    def test_empty_file(self):
        rep = analyze_python_file("a.py", "")
        self.assertEqual((rep["loc"], rep["sloc"]), (0, 0))
        self.assertEqual((rep["cog"], rep["cc"]), (0, 1))
        self.assertEqual((rep["funcs"], rep["todos"], rep["mi"]), (0, 0, 100.0))
        self.assertEqual(rep["imports"], [])

    def test_file_without_functions(self):
        rep = analyze_python_file("a.py", "x = 1\ny = x + 1\n")
        self.assertEqual((rep["cog"], rep["cc"]), (0, 1))
        self.assertEqual(rep["funcs"], 0)

    def test_flat_ifs(self):
        # два if на нулевой вложенности: +1 каждый
        src = ("def f(x):\n"
               "    if x > 0:\n"
               "        y = 1\n"
               "    if x > 1:\n"
               "        y = 2\n"
               "    return y\n")
        self.assertEqual(cog_cc(src), (2, 4))

    def test_logical_operators_count_as_branches(self):
        # a and b and c — один BoolOp из 3 значений: +2
        self.assertEqual(cog_cc("def g(a, b, c):\n    return a and b and c\n"), (2, 4))

    def test_nested_if_counts_per_docstring(self):
        # внешний if на вложенности 0: +1; внутренний if на вложенности 1: +1+1=2
        # → cog 3; cc = ветвления + 2 = 4
        self.assertEqual(
            cog_cc("def f():\n    if a:\n        if b:\n            pass\n"), (3, 4))

    def test_deep_nesting_scales_with_depth(self):
        # if(+1) → for(+2) → while(+3) → if(+4); cc = 1 + 4 ветвления
        src = ("def h(x, ys):\n"
               "    if x:\n"
               "        for y in ys:\n"
               "            while x:\n"
               "                if y:\n"
               "                    break\n")
        self.assertEqual(cog_cc(src), (10, 6))

    def test_elif_chain_is_flat(self):
        # первый if +1, каждый elif — плоский +1 (без надбавки за вложенность)
        src = ("def k(x):\n"
               "    if x == 1:\n"
               "        return 'a'\n"
               "    elif x == 2:\n"
               "        return 'b'\n"
               "    elif x == 3:\n"
               "        return 'c'\n"
               "    else:\n"
               "        return 'd'\n")
        self.assertEqual(cog_cc(src), (3, 5))

    def test_branch_inside_for_counts_with_nesting(self):
        # for +1, вложенный в его тело if — +1+вложенность(1) = 2
        src = ("def f(ys, c):\n"
               "    for y in ys:\n"
               "        if c:\n"
               "            break\n")
        self.assertEqual(cog_cc(src), (3, 4))

    def test_except_handler_counts_nesting(self):
        # if на вложенности 0: +1, deepest 1; except на вложенности 1: +1+1=2,
        # deepest 2 — except, как if/циклы, поднимает max_nesting
        src = ("def f():\n"
               "    if a:\n"
               "        try:\n"
               "            pass\n"
               "        except E:\n"
               "            pass\n")
        self.assertEqual(_function_complexity(ast.parse(src).body[0]), (3, 3, 2))
        # глубина except учитывается в следующем ветвлении: if в его теле — +1+2=3
        src2 = ("def f():\n"
                "    if a:\n"
                "        try:\n"
                "            pass\n"
                "        except E:\n"
                "            if b:\n"
                "                pass\n")
        self.assertEqual(_function_complexity(ast.parse(src2).body[0]), (6, 4, 3))

    def test_ternary(self):
        self.assertEqual(cog_cc("def t(a, b, c):\n    return a if c else b\n"), (1, 3))

    def test_comprehension_with_if(self):
        # comprehension: 1 + число if'ов = 2
        self.assertEqual(cog_cc("def comp(ys):\n    return [y for y in ys if y]\n"),
                         (2, 4))

    def test_each_except_handler_counts(self):
        src = ("def r(x):\n"
               "    try:\n"
               "        return 1 / x\n"
               "    except ZeroDivisionError:\n"
               "        return 0\n"
               "    except TypeError:\n"
               "        return 2\n")
        self.assertEqual(cog_cc(src), (2, 4))

    def test_assert_counts(self):
        self.assertEqual(cog_cc("def chk(x):\n    assert x > 0\n    return x\n"), (1, 3))

    def test_match_cases_count(self):
        src = ("def m(v):\n"
               "    match v:\n"
               "        case 1:\n"
               "            return 'a'\n"
               "        case _:\n"
               "            return 'b'\n")
        self.assertEqual(cog_cc(src), (2, 4))

    def test_nested_functions_analyzed_separately(self):
        # inner не входит в outer: 1 + 1, funcs == 2
        src = ("def outer(x):\n"
               "    if x:\n"
               "        return 1\n"
               "\n"
               "    def inner(y):\n"
               "        if y:\n"
               "            return 2\n"
               "        return 0\n"
               "\n"
               "    return 0\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual((rep["cog"], rep["cc"], rep["funcs"]), (2, 5, 2))

    def test_module_level_branching(self):
        # ветвление вне функций: mod_cc = 2 → cc = 1 + (2 - 1) = 2
        self.assertEqual(cog_cc("FLAG = True\nif FLAG:\n    run()\n"), (1, 2))


class Lcom4Test(unittest.TestCase):
    def test_cohesive_class_is_one_component(self):
        src = ("class Point:\n"
               "    def __init__(self):\n"
               "        self.x = 0\n"
               "        self.y = 0\n"
               "\n"
               "    def set_x(self):\n"
               "        self.x = 1\n"
               "\n"
               "    def get_x(self):\n"
               "        return self.x\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual((rep["lcom4_max"], rep["lcom4_bad"], rep["classes"]), (1, 0, 1))

    def test_god_object_five_disjoint_methods(self):
        src = ("class God:\n"
               "    def a(self):\n"
               "        self.xa = 1\n"
               "\n"
               "    def b(self):\n"
               "        self.xb = 2\n"
               "\n"
               "    def c(self):\n"
               "        self.xc = 3\n"
               "\n"
               "    def d(self):\n"
               "        self.xd = 4\n"
               "\n"
               "    def e(self):\n"
               "        self.xe = 5\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual((rep["lcom4_max"], rep["lcom4_bad"]), (5, 1))

    def test_methods_connected_via_self_calls(self):
        # one вызывает two (общие и через _v); other обособлен → 2 компоненты
        src = ("class Chain:\n"
               "    def one(self):\n"
               "        return self.two() + self._v\n"
               "\n"
               "    def two(self):\n"
               "        return self._v\n"
               "\n"
               "    def other(self):\n"
               "        return self._w\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual((rep["lcom4_max"], rep["lcom4_bad"]), (2, 0))

    def test_static_and_class_methods_skipped(self):
        src = ("class Utils:\n"
               "    def a(self):\n"
               "        return self._v\n"
               "\n"
               "    @staticmethod\n"
               "    def s():\n"
               "        return 1\n"
               "\n"
               "    @classmethod\n"
               "    def c(cls):\n"
               "        return 2\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual(rep["lcom4_max"], 1)

    def test_only_static_methods_means_zero(self):
        src = ("class OnlyStatic:\n"
               "    @staticmethod\n"
               "    def s():\n"
               "        return 1\n")
        self.assertEqual(analyze_python_file("a.py", src)["lcom4_max"], 0)


class AbstractAndImportsTest(unittest.TestCase):
    def test_abstract_classes_detected(self):
        src = ("from abc import ABC, abstractmethod\n"
               "\n"
               "class Base(ABC):\n"
               "    @abstractmethod\n"
               "    def do(self):\n"
               "        ...\n"
               "\n"
               "class Proto(Protocol):\n"
               "    def handle(self):\n"
               "        ...\n"
               "\n"
               "class Plain:\n"
               "    def work(self):\n"
               "        ...\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual((rep["classes"], rep["abstract"]), (3, 2))

    def test_imports_extraction_with_levels(self):
        src = ("import os\n"
               "import a.b.c\n"
               "from pkg.mod import thing\n"
               "from . import sibling\n"
               "from ..other import fn\n")
        rep = analyze_python_file("a.py", src)
        self.assertEqual(rep["imports"], [
            (0, "os"), (0, "a.b.c"), (0, "pkg.mod"), (1, ""), (2, "other"),
        ])

    def test_todo_counts_only_comments(self):
        src = ("# TODO: refactor this\n"
               "x = 1  # fixme later\n"
               "Y = 'TODO in string'\n"
               "todo_list = []\n"
               "# plain comment\n")
        self.assertEqual(analyze_python_file("a.py", src)["todos"], 2)


class RobustnessTest(unittest.TestCase):
    def test_syntax_error_returns_none(self):
        self.assertIsNone(analyze_python_file("a.py", "def broken(:\n"))

    def test_oversize_source_returns_none(self):
        self.assertIsNone(analyze_python_file("a.py", "x = 1\n" * 70_000))


class HalsteadAndMITest(unittest.TestCase):
    def test_halstead_volume_golden(self):
        # x = 1: операции {=}, операнды {x, 1} → V = 3 * log2(3) ≈ 4.7549
        v = halstead_volume(ast.parse("x = 1\n"))
        self.assertAlmostEqual(v, 4.7549, delta=0.001)

    def test_halstead_excludes_docstring(self):
        with_doc = halstead_volume(ast.parse('"""Doc text here."""\nx = 1\n'))
        without = halstead_volume(ast.parse("x = 1\n"))
        self.assertEqual(with_doc, without)

    def test_mi_guards_return_perfect(self):
        self.assertEqual(maintainability_index(0, 10, 500.0), 100.0)
        self.assertEqual(maintainability_index(10, 1, 0.0), 100.0)

    def test_mi_golden_value(self):
        # вручную: raw = (171 - 5.2*ln100 - 0.23 - 16.2*ln10)*100/171 ≈ 64.05,
        # MI = 100*(1 - e^(-4.6*0.6405)) ≈ 94.75
        self.assertAlmostEqual(maintainability_index(10, 1, 100.0), 94.75, delta=0.06)

    def test_mi_decreases_with_cc(self):
        good = maintainability_index(10, 1, 100.0)
        bad = maintainability_index(10, 2, 100.0)
        self.assertLess(bad, good)
        self.assertLess(good, 100.0)

    def test_file_mi_golden(self):
        # x = 1: SLOC=1, cc=1, V≈4.7549 → raw ≈ 95.12 → MI ≈ 98.74
        self.assertAlmostEqual(analyze_python_file("a.py", "x = 1\n")["mi"],
                               98.74, delta=0.06)


class TotalsTest(unittest.TestCase):
    def test_empty_project_totals(self):
        t = analyze_python({})["totals"]
        self.assertEqual(t["files"], 0)
        self.assertEqual(t["sloc"], 0)
        self.assertEqual(t["mi_avg"], 100.0)
        self.assertEqual(t["cog_per_kloc"], 0.0)
        self.assertEqual(t["cc_per_func"], 0.0)
        self.assertEqual(t["lcom4_max"], 0)
        self.assertEqual(t["god_files"], 0)

    def test_test_file_detection_and_test_sloc(self):
        res = analyze_python({
            "app.py": "y = 2\n",
            "tests/test_app.py": "x = 1\n",
            "pkg/mod_test.py": "z = 3\n",
        })
        self.assertTrue(res["files"]["tests/test_app.py"]["is_test"])
        self.assertTrue(res["files"]["pkg/mod_test.py"]["is_test"])
        self.assertFalse(res["files"]["app.py"]["is_test"])
        self.assertEqual(res["totals"]["test_sloc"], 2)
        self.assertEqual(res["totals"]["sloc"], 3)


if __name__ == "__main__":
    unittest.main()
