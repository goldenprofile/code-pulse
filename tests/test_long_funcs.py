"""long_funcs считает длинные функции (cog >= 20), а не файлы."""
import unittest

from pulse.metrics_js import analyze_js, analyze_js_file
from pulse.metrics_py import LONG_FUNC_COG, analyze_python, analyze_python_file


def _py_long() -> str:
    lines = ["def big(x):", "    y = x"]
    for i in range(LONG_FUNC_COG):  # каждый top-level if даёт cog +1
        lines += [f"    if x > {i}:", f"        y += {i}"]
    lines.append("    return y")
    return "\n".join(lines) + "\n"


def _js_long(n: int = LONG_FUNC_COG - 1) -> str:
    # f_cc = 1 + число if/for/while/case/catch в теле → нужно n = 19 if'ов
    lines = ["function big(x) {", "  let y = x"]
    for i in range(n):
        lines += [f"  if (x > {i}) {{", f"    y += {i}", "  }"]
    lines += ["  return y", "}"]
    return "\n".join(lines) + "\n"


PY_SRC = "def small(x):\n    if x:\n        return 1\n    return 0\n\n" + _py_long()
JS_SRC = "function small(x) {\n  if (x) { return 1 }\n  return 0\n}\n\n" + _js_long()


class PyLongFuncsTest(unittest.TestCase):
    def test_counts_functions_not_files(self):
        rep = analyze_python_file("a.py", PY_SRC)
        self.assertEqual(rep["funcs"], 2)
        self.assertEqual(rep["long_funcs"], 1)

    def test_totals_aggregate_functions(self):
        totals = analyze_python({"a.py": PY_SRC})["totals"]
        self.assertEqual(totals["long_funcs"], 1)

    def test_two_long_funcs_in_one_file_count_twice(self):
        totals = analyze_python({"a.py": _py_long() + "\n\n" + _py_long()})["totals"]
        self.assertEqual(totals["long_funcs"], 2)


class JsLongFuncsTest(unittest.TestCase):
    def test_counts_functions_not_files(self):
        rep = analyze_js_file("a.js", JS_SRC)
        self.assertEqual(rep["funcs"], 2)
        self.assertEqual(rep["long_funcs"], 1)

    def test_totals_aggregate_functions(self):
        totals = analyze_js({"a.js": JS_SRC})["totals"]
        self.assertEqual(totals["long_funcs"], 1)

    def test_two_long_funcs_in_one_file_count_twice(self):
        src = _js_long() + "\n\n" + _js_long()
        totals = analyze_js({"a.js": src})["totals"]
        self.assertEqual(totals["long_funcs"], 2)


if __name__ == "__main__":
    unittest.main()
