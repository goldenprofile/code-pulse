"""Гонка «двойной скан» (атомарный try_begin) и набор файлов ворктри/истории."""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from pulse import scanner
from pulse.store import Store
from server import App, make_handler


class TryBeginTest(unittest.TestCase):
    def test_sequential_cas(self):
        st = scanner.Status()
        self.assertTrue(st.try_begin("scan"))
        self.assertFalse(st.try_begin("scan"))      # фаза занята
        self.assertFalse(st.try_begin("backfill"))  # любая занятая фаза блокирует
        st.end()
        self.assertTrue(st.try_begin("backfill"))

    def test_exactly_one_winner(self):
        st = scanner.Status()
        n = 16
        barrier = threading.Barrier(n)
        results = []

        def worker():
            barrier.wait()
            results.append(st.try_begin("scan"))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # check-and-set целиком под замком ⇒ победитель ровно один
        # при любом чередовании потоков, без sleep-гонок на удачу
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), n - 1)

