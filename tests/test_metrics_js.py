import unittest

from pulse.metrics_js import analyze_js_file


class FunctionCountTest(unittest.TestCase):
    def test_control_blocks_are_not_functions(self):
        src = (
            "function a(x) {\n"
            "  if (x) { return 1 } else if (x > 1) { x-- }\n"
            "  for (let i = 0; i < 3; i++) { x++ }\n"
            "  while (x) { x-- }\n"
            "  switch (x) { case 1: break }\n"
            "  try { x() } catch (e) { }\n"
            "}\n"
        )
        rep = analyze_js_file("a.js", src)
        self.assertEqual(rep["funcs"], 1)
        # база 1 + функция (1 + if, else if, for, while, case, catch)
        self.assertEqual(rep["cc"], 8)

    def test_methods_and_arrows_are_functions(self):
        src = (
            "class K {\n"
            "  m(y) { return y }\n"
            "  async n(z) { if (z) { return 1 } }\n"
            "}\n"
            "const f = (z) => { return z }\n"
        )
        self.assertEqual(analyze_js_file("k.js", src)["funcs"], 3)

    def test_expression_arrow_does_not_steal_next_body(self):
        src = (
            "const f = (z) => z\n"
            "function g(x) { if (x) { return 1 } }\n"
        )
        rep = analyze_js_file("f.js", src)
        self.assertEqual(rep["funcs"], 2)
        # if из g должен посчитаться один раз — только в g, не в f
        self.assertEqual(rep["cc"], 1 + 2)


if __name__ == "__main__":
    unittest.main()
