"""Диспетчер analyze_sources: распределение по анализаторам, сводные totals."""
import unittest

from pulse.analyze import analyze_sources


class AnalyzeSourcesTest(unittest.TestCase):
    def test_empty_tree(self):
        res = analyze_sources({})
        self.assertEqual(res["totals"]["files"], 0)
        self.assertEqual(res["totals"]["sloc"], 0)
        self.assertEqual(res["totals"]["mi_avg"], 100.0)
        self.assertEqual(res["langs"], {})
        self.assertEqual(res["files"], {})
        self.assertEqual(res["dup_ratio"], 0.0)
        self.assertEqual(res["coupling"]["pkg_count"], 0)

    def test_mixed_languages_dispatched(self):
        res = analyze_sources({"app.py": "x = 1\n", "web/app.js": "const x = 1;\n"})
        self.assertEqual(res["langs"], {"py": 1, "js": 1})
        self.assertEqual(res["totals"]["sloc"], 2)
        self.assertEqual(res["totals"]["files"], 2)

    def test_files_map_strips_imports_but_coupling_sees_them(self):
        res = analyze_sources({"a/mod.py": "import b\nx = 1\n"})
        entry = res["files"]["a/mod.py"]
        self.assertNotIn("imports", entry)
        self.assertIn("cog", entry)
        # пакет a известен, b — нет: зацепления нет, но пакет посчитан
        self.assertEqual(res["coupling"]["pkg_count"], 1)
        self.assertFalse(res["coupling"]["has_deps"])

    def test_test_ratio_and_todo_per_kloc(self):
        res = analyze_sources({
            "app.py": "x = 1  # TODO fix\n",
            "tests/test_app.py": "y = 1\n",
        })
        self.assertEqual(res["totals"]["todos"], 1)
        self.assertEqual(res["totals"]["todo_per_kloc"], 500.0)  # 1 на 2 SLOC
        self.assertEqual(res["totals"]["test_ratio"], 0.5)

    def test_totals_aggregation(self):
        res = analyze_sources({
            "p1/a.py": "def f(x):\n    if x:\n        return 1\n    return 0\n",
            "p2/b.py": "x = 1\n",
        })
        t = res["totals"]
        self.assertEqual(t["funcs"], 1)
        self.assertEqual(t["cog"], 1)
        self.assertEqual(t["cc"], 4)  # 1 + f_cc(2) + cc(p2)=1
        self.assertEqual(t["cog_per_func"], 1.0)
        self.assertEqual(t["cc_per_func"], 4.0)


if __name__ == "__main__":
    unittest.main()
