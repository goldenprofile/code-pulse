"""Архитектурное зацепление: Ca/Ce, нестабильность I, абстрактность A, дистанция D, SCC.

Все золотые значения получены вручную из определений модуля:
I = Ce/(Ca+Ce), A = абстрактные/все классы, D = |A + I - 1|;
циклы считаются как компоненты сильной связности размера ≥2 (Косарайю).
Фикстуры — синтетические py-исходники через analyze_python (реальные отчёты файлов).
"""
import unittest

from pulse.coupling import coupling_metrics, pkg_of_dir, pkg_of_file
from pulse.metrics_py import analyze_python


def coupled(files: dict) -> dict:
    return coupling_metrics(analyze_python(files)["files"])


def by_pkg(res: dict) -> dict:
    return {p["pkg"]: p for p in res["packages"]}


class PkgOfTest(unittest.TestCase):
    def test_pkg_of_dir_rules(self):
        self.assertEqual(pkg_of_dir(["src", "mylib", "sub"]), "mylib")  # src-layout
        self.assertEqual(pkg_of_dir(["mylib", "sub"]), "mylib")
        self.assertEqual(pkg_of_dir(["."]), ".")
        # эвристика src-layout срезает первое имя из SRC_ROOTS в любой позиции
        self.assertEqual(pkg_of_dir(["lib", "inner"]), "inner")
        self.assertEqual(pkg_of_dir(["app", "inner"]), "inner")
        self.assertEqual(pkg_of_dir(["."]), ".")
        # одиночный "src" без вложенных пакетов — сам пакет
        self.assertEqual(pkg_of_dir(["src"]), "src")

    def test_pkg_of_file_rules(self):
        self.assertEqual(pkg_of_file("app.py"), ".")
        self.assertEqual(pkg_of_file("a/b/c.py"), "a")
        self.assertEqual(pkg_of_file("src/x/y.py"), "x")


class CouplingMetricsTest(unittest.TestCase):
    def test_no_imports_is_vacuous_and_neutral(self):
        res = coupled({"a/m.py": "x = 1\n", "b/n.py": "y = 2\n"})
        self.assertFalse(res["has_deps"])
        self.assertEqual(res["mean_D"], 0.0)  # нет рёбер — не наказываем
        self.assertEqual(res["cycles"], 0)
        self.assertEqual(res["pkg_count"], 2)
        for p in res["packages"]:
            self.assertEqual((p["ca"], p["ce"], p["I"]), (0, 0, 0.0))

    def test_afferent_efferent_golden(self):
        res = coupled({
            "pkga/core.py": "import pkgb.other\n",
            "pkgb/other.py": "x = 1\n",
        })
        pkgs = by_pkg(res)
        self.assertEqual(pkgs["pkga"]["ce"], 1)
        self.assertEqual(pkgs["pkga"]["ca"], 0)
        self.assertEqual(pkgs["pkga"]["I"], 1.0)   # зависит от всех — максимально нестабилен
        self.assertEqual(pkgs["pkga"]["D"], 0.0)   # |0 + 1 - 1|
        self.assertEqual(pkgs["pkgb"]["ce"], 0)
        self.assertEqual(pkgs["pkgb"]["ca"], 1)
        self.assertEqual(pkgs["pkgb"]["I"], 0.0)   # от него зависят — стабилен
        self.assertEqual(pkgs["pkgb"]["D"], 1.0)   # |0 + 0 - 1|
        self.assertAlmostEqual(res["mean_I"], 0.5)
        self.assertEqual(res["mean_D"], 0.5)
        self.assertTrue(res["has_deps"])
        self.assertEqual(res["cycles"], 0)

    def test_self_package_import_not_an_edge(self):
        res = coupled({
            "a/m.py": "from . import x\nimport a.y\n",
            "a/y.py": "z = 1\n",
        })
        self.assertFalse(res["has_deps"])
        self.assertEqual(res["cycles"], 0)
        self.assertEqual((by_pkg(res)["a"]["ca"], by_pkg(res)["a"]["ce"]), (0, 0))

    def test_relative_import_across_packages(self):
        # from ...p2.api в p1/sub/m.py поднимается на 2 уровня вверх → пакет p2
        res = coupled({
            "p1/sub/m.py": "from ...p2.api import f\n",
            "p2/api.py": "x = 1\n",
        })
        self.assertTrue(res["has_deps"])
        pkgs = by_pkg(res)
        self.assertEqual(pkgs["p1"]["ce"], 1)
        self.assertEqual(pkgs["p1"]["I"], 1.0)
        self.assertEqual(pkgs["p2"]["ca"], 1)

    def test_root_level_file_counts_as_afferent(self):
        # main.py в корне: пакет "." выброшен из known, но ребро в pkgb засчитано
        res = coupled({
            "main.py": "import pkgb.other\n",
            "pkgb/other.py": "x = 1\n",
        })
        self.assertEqual(res["pkg_count"], 1)
        self.assertEqual(by_pkg(res)["pkgb"]["ca"], 1)
        self.assertEqual(by_pkg(res)["pkgb"]["I"], 0.0)
        self.assertEqual(res["cycles"], 0)

    def test_fan_in_counts_edges_and_instability(self):
        res = coupled({
            "core/api.py": "import utilx.a\n",
            "webapp/main.py": "import core.api\n",
            "webapp/util.py": "import core.api\nimport utilx.a\n",
            "cli/run.py": "import core.api\n",
            "utilx/a.py": "x = 1\n",
        })
        pkgs = by_pkg(res)
        # core: ca=2 (webapp, cli), ce=1 (utilx) → I = 1/3
        self.assertEqual((pkgs["core"]["ca"], pkgs["core"]["ce"]), (2, 1))
        self.assertEqual(pkgs["core"]["I"], 0.333)
        self.assertEqual((pkgs["webapp"]["ce"], pkgs["webapp"]["I"]), (2, 1.0))
        self.assertEqual((pkgs["utilx"]["ca"], pkgs["utilx"]["I"]), (2, 0.0))
        # mean_I = (1/3 + 1 + 0 + 1) / 4 = 7/12
        self.assertAlmostEqual(res["mean_I"], 0.583)
        # два файла webapp импортируют core → вес ребра 2, первое в списке
        self.assertEqual(res["edges"][0], ["webapp", "core", 2])

    def test_package_named_lib_is_addressable(self):
        res = coupled({"lib/core.py": "x = 1\n", "u1/x.py": "import lib.core\n"})
        self.assertEqual(by_pkg(res)["lib"]["ca"], 1)
        self.assertEqual(by_pkg(res)["u1"]["ce"], 1)

    def test_abstractness_and_distance(self):
        res = coupled({
            "abs/api.py": ("from abc import ABC\n"
                           "\n"
                           "class A(ABC):\n"
                           "    pass\n"
                           "\n"
                           "class B:\n"
                           "    pass\n"),
            "imp/user.py": "import abs.api\n",
        })
        pkgs = by_pkg(res)
        # abs: A = 1/2, I = 0 → D = |0.5 - 1| = 0.5
        self.assertEqual(pkgs["abs"]["A"], 0.5)
        self.assertEqual(pkgs["abs"]["I"], 0.0)
        self.assertEqual(pkgs["abs"]["D"], 0.5)
        # imp: A = 0, I = 1 → D = 0 (на main sequence, но вырожденно конкретен)
        self.assertEqual(pkgs["imp"]["A"], 0.0)
        self.assertEqual(pkgs["imp"]["D"], 0.0)
        self.assertEqual(res["mean_D"], 0.25)


class CyclesTest(unittest.TestCase):
    def test_two_package_cycle(self):
        res = coupled({
            "a/m.py": "import b.n\n",
            "b/n.py": "import a.m\n",
        })
        self.assertEqual(res["cycles"], 1)
        # оба: ca=ce=1 → I=0.5, D=|0+0.5-1|=0.5
        for p in res["packages"]:
            self.assertEqual((p["I"], p["D"]), (0.5, 0.5))
        self.assertEqual(res["mean_D"], 0.5)

    def test_three_package_cycle_is_one_scc(self):
        res = coupled({
            "a/m.py": "import b.n\n",
            "b/n.py": "import c.k\n",
            "c/k.py": "import a.m\n",
        })
        self.assertEqual(res["cycles"], 1)
        self.assertEqual(res["pkg_count"], 3)

    def test_two_disjoint_cycles(self):
        res = coupled({
            "a/m.py": "import b.n\n",
            "b/n.py": "import a.m\n",
            "c/m.py": "import d.n\n",
            "d/n.py": "import c.m\n",
        })
        self.assertEqual(res["cycles"], 2)
        self.assertEqual(res["pkg_count"], 4)


class ViolationsTest(unittest.TestCase):
    def test_violation_requires_popular_and_unstable(self):
        # corelib: ca=3 (u1..u3), ce=4 (p1..p4) → I = 4/7 ≈ 0.571 > 0.5 → нарушение.
        # u1..u3 нестабильны (I=1), но ca=0; p1..p4 популярны, но стабильны.
        res = coupled({
            "corelib/core.py": "import p1.a\nimport p2.a\nimport p3.a\nimport p4.a\n",
            "u1/x.py": "import corelib.core\n",
            "u2/x.py": "import corelib.core\n",
            "u3/x.py": "import corelib.core\n",
            "p1/a.py": "x = 1\n",
            "p2/a.py": "x = 1\n",
            "p3/a.py": "x = 1\n",
            "p4/a.py": "x = 1\n",
        })
        self.assertEqual(res["violations"], 1)
        corelib = by_pkg(res)["corelib"]
        self.assertEqual((corelib["ca"], corelib["ce"], corelib["I"]),
                         (3, 4, 0.571))

    def test_stable_popular_package_is_not_violation(self):
        # corelib без исходящих зависимостей: I = 0 → даже с ca=3 это нормально
        res = coupled({
            "corelib/core.py": "x = 1\n",
            "u1/x.py": "import corelib.core\n",
            "u2/x.py": "import corelib.core\n",
            "u3/x.py": "import corelib.core\n",
        })
        self.assertEqual(res["violations"], 0)
        self.assertEqual(by_pkg(res)["corelib"]["I"], 0.0)


if __name__ == "__main__":
    unittest.main()
