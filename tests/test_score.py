"""Сводный скор: веса подындексов, нормировка 0–100, граничные случаи.

Веса из докстринга pulse/score.py: MI 25%, архитектура 20%, когнитивная 20%,
дублирование 15%, тесты 10%, TODO 10%. Золотые числа посчитаны вручную.
"""
import unittest

from pulse.score import quality_subscores


class ScoreTest(unittest.TestCase):
    def test_empty_totals_is_neutral_not_zero(self):
        res = quality_subscores({}, None, 0.0)
        self.assertEqual(res["mi"], 0.0)       # mi_avg неизвестен → 0
        self.assertEqual(res["cog"], 100.0)    # нет функций — не штрафуем
        self.assertEqual(res["arch"], 100.0)   # нет зацепления — нейтрально
        self.assertEqual(res["dup"], 100.0)
        self.assertEqual(res["test"], 0.0)     # нет тестов — честный 0
        self.assertEqual(res["todo"], 100.0)
        # 0.2*100 + 0.2*100 + 0.15*100 + 0.1*0 + 0.1*100
        self.assertEqual(res["score"], 65.0)

    def test_perfect_inputs_golden(self):
        totals = {"mi_avg": 100, "cog_per_func": 0, "test_ratio": 0,
                  "todo_per_kloc": 0}
        res = quality_subscores(totals, {}, 0.0)
        # 0.25*100 + 0.2*100 + 0.2*100 + 0.15*100 + 0.1*0(нет тестов) + 0.1*100
        self.assertEqual(res["score"], 90.0)

    def test_weights_golden_scenario(self):
        totals = {"mi_avg": 80, "cog_per_func": 4, "test_ratio": 0.5,
                  "todo_per_kloc": 10}
        coupling = {"mean_D": 0, "cycles": 1, "violations": 0}
        res = quality_subscores(totals, coupling, 0.04)
        self.assertEqual(res["mi"], 80.0)
        self.assertEqual(res["cog"], 100.0)    # cog_per_func == 4 → без штрафа
        self.assertEqual(res["arch"], 92.0)    # 100 - 1 цикл * 8
        self.assertEqual(res["dup"], 80.0)     # 100 - 0.04 * 500
        self.assertEqual(res["test"], 100.0)   # 0.5 * 200
        self.assertEqual(res["todo"], 50.0)    # 100 - 10 * 5
        # 0.25*80 + 0.2*100 + 0.2*92 + 0.15*80 + 0.1*100 + 0.1*50
        self.assertEqual(res["score"], 85.4)

    def test_extreme_inputs_clamp_both_ends(self):
        totals = {"mi_avg": 0, "cog_per_func": 100, "test_ratio": 2,
                  "todo_per_kloc": 1000}
        coupling = {"mean_D": 2, "cycles": 100, "violations": 100}
        res = quality_subscores(totals, coupling, 1.0)
        self.assertEqual(res["mi"], 0.0)
        self.assertEqual(res["cog"], 0.0)      # 100 - 96*8 → зажато в 0
        self.assertEqual(res["arch"], 0.0)     # 100 - 180 - 800 - 400 → 0
        self.assertEqual(res["dup"], 0.0)      # 100 - 500 → 0
        self.assertEqual(res["test"], 100.0)   # 2*200 = 400 → зажато в 100
        self.assertEqual(res["todo"], 0.0)
        self.assertEqual(res["score"], 10.0)   # единственный живой вклад — тесты

    def test_test_ratio_boundary_at_half(self):
        self.assertEqual(quality_subscores({"test_ratio": 0.5}, {}, 0.0)["test"], 100.0)
        self.assertEqual(quality_subscores({"test_ratio": 0.25}, {}, 0.0)["test"], 50.0)

    def test_dup_ratio_boundary(self):
        self.assertEqual(quality_subscores({}, {}, 0.2)["dup"], 0.0)
        self.assertEqual(quality_subscores({}, {}, 0.1)["dup"], 50.0)

    def test_legacy_cog_per_kloc_fallback(self):
        # старые снапшоты без cog_per_func: cog_f = cog_per_kloc / 7
        res = quality_subscores({"cog_per_kloc": 70}, {}, 0.0)
        self.assertEqual(res["cog"], 52.0)     # 100 - (10 - 4) * 8
        # 0.2*52 + 0.2*100 + 0.15*100 + 0.1*0 + 0.1*100
        self.assertEqual(res["score"], 55.4)

    def test_coupling_none_equals_empty(self):
        totals = {"mi_avg": 90, "cog_per_func": 2, "test_ratio": 0.4,
                  "todo_per_kloc": 1}
        self.assertEqual(quality_subscores(totals, None, 0.0),
                         quality_subscores(totals, {}, 0.0))


if __name__ == "__main__":
    unittest.main()
