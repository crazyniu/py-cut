"""ANSI controls must not split secrets before log redaction."""
from __future__ import annotations

from collections import deque
import io
import unittest
from unittest import mock

from pycut import tui_runner


class AnsiSecretTests(unittest.TestCase):
    def test_reader_strips_ansi_before_redacting_key(self):
        key = "sk-secret"
        child = mock.Mock()
        child.stdout = io.StringIO("token sk-\\x1b[31msecret\\x1b[0m leaked\\n".replace("\\x1b", "\x1b"))
        child.poll.return_value = 0
        screen = mock.Mock()
        screen.getch.return_value = -1
        logs = deque(maxlen=20)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=child), \
             mock.patch.object(tui_runner, "_draw"):
            self.assertEqual(tui_runner._run_one(screen, ["slice", "--llm-key", key], 1, 1, logs), (0, False))
        self.assertNotIn(key, "".join(logs))
        self.assertIn("********", "".join(logs))

    def test_launch_error_strips_ansi_before_redacting_key(self):
        key = "sk-secret"
        screen = mock.Mock()
        logs = deque(maxlen=10)
        message = "failed sk-\\x1b[31msecret\\x1b[0m".replace("\\x1b", "\x1b")
        with mock.patch.object(tui_runner.subprocess, "Popen", side_effect=OSError(message)):
            self.assertEqual(tui_runner._run_one(screen, ["slice", "--api-key=" + key], 1, 1, logs), (2, False))
        self.assertNotIn(key, "".join(logs))
        self.assertIn("********", "".join(logs))


if __name__ == "__main__":
    unittest.main()
