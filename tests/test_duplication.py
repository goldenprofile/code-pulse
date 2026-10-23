"""Дублирование: первое вхождение блока — не дубликат (контракт из докстринга)."""
import unittest

from pulse.duplication import duplication_ratio


def _block(tag: str) -> str:
    return "\n".join(f"{tag}{i} = {i}" for i in range(6))


class DuplicationRatioTest(unittest.TestCase):
    def test_repeated_block_counts_second_copy_only(self):
        src = _block("a") + "\n" + _block("a")
        self.assertEqual(duplication_ratio({"f.py": src}), 0.5)

    def test_unique_code_is_zero(self):
        src = "\n".join(f"x{i} = {i}" for i in range(10))
        self.assertEqual(duplication_ratio({"f.py": src}), 0.0)

    def test_duplication_across_files(self):
        shared = _block("a")
        files = {"a.py": shared + "\nb = 1", "b.py": shared + "\nc = 2"}
        # повторённые 6 строк второй копии из 14 нормализованных
        self.assertEqual(duplication_ratio(files), round(6 / 14, 4))


if __name__ == "__main__":
    unittest.main()
