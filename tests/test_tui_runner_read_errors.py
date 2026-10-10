"""Regression tests for failures while streaming subprocess output."""
from __future__ import annotations

from collections import deque
import io
import unittest
from unittest import mock

from pycut import tui_runner


class BrokenStream:
    def __iter__(self):
        yield "first line\n"
        raise OSError("stream read failed")

    def close(self):
        self.closed = True


class ReaderFailureTests(unittest.TestCase):
    def _run(self, stream, exit_code):
        child = mock.Mock()
        child.stdout = stream
        child.poll.return_value = exit_code
        screen = mock.Mock()
        screen.getch.return_value = -1
        history = deque(maxlen=20)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=child), \
             mock.patch.object(tui_runner, "_draw"):
            result = tui_runner._run_one(screen, ["slice"], 1, 1, history)
        return result, list(history)

    def test_read_failure_overrides_successful_child_exit(self):
        stream = BrokenStream()
        result, lines = self._run(stream, 0)
        self.assertEqual(result, (2, False))
        self.assertTrue(stream.closed)
        self.assertIn("first line", lines)
        self.assertTrue(any("日志读取失败" in line and "OSError" in line for line in lines))

    def test_child_failure_code_is_preserved(self):
        result, lines = self._run(BrokenStream(), 7)
        self.assertEqual(result, (7, False))
        self.assertTrue(any("日志读取失败" in line for line in lines))

    def test_normal_output_remains_success(self):
        result, lines = self._run(io.StringIO("normal output\n"), 0)
        self.assertEqual(result, (0, False))
        self.assertEqual(lines, ["normal output"])


if __name__ == "__main__":
    unittest.main()
