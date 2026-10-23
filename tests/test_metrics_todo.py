"""TODO/FIXME считаются только в комментариях, не в коде и не в литералах."""
import unittest

from pulse.metrics_js import analyze_js_file
from pulse.metrics_py import analyze_python_file


class PythonTodoTest(unittest.TestCase):
    def test_comment_lines_counted(self):
        src = "# TODO: x\ny = 1  # FIXME later\n"
        self.assertEqual(analyze_python_file("a.py", src)["todos"], 2)

    def test_identifiers_are_not_todos(self):
        src = "todo_list = []\nFIXME_CONST = 1\nmytodo = 2\n"
        self.assertEqual(analyze_python_file("a.py", src)["todos"], 0)

    def test_string_literals_are_not_todos(self):
        src = 's = "TODO: fix"\nt = "todo_list"\n'
        self.assertEqual(analyze_python_file("a.py", src)["todos"], 0)


class JsTodoTest(unittest.TestCase):
    def test_comments_counted(self):
        src = "// TODO: x\nlet y = 1; // FIXME later\n/* TODO block */\n"
        self.assertEqual(analyze_js_file("a.js", src)["todos"], 3)

    def test_identifiers_and_strings_are_not_todos(self):
        src = (
            "let todoList = [];\n"
            'const s = "TODO: fix";\n'
            'let u = "http://x/TODO/1";\n'
        )
        self.assertEqual(analyze_js_file("a.js", src)["todos"], 0)


if __name__ == "__main__":
    unittest.main()
