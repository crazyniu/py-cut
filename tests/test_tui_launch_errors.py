"""TUI subprocess launch errors must never expose command-line secrets."""
from __future__ import annotations

from collections import deque
import unittest
from unittest import mock

from pycut import tui_runner


class LaunchErrorTests(unittest.TestCase):
    def _run_error(self, argv, message):
        screen = mock.Mock()
        history = deque(maxlen=10)
        with mock.patch.object(tui_runner.subprocess, "Popen",
                               side_effect=OSError(message)):
            result = tui_runner._run_one(screen, argv, 1, 1, history)
        return result, list(history)

    def test_space_separated_secret_is_redacted(self):
        key = "sk-secret-test-123"
        result, lines = self._run_error(
            ["slice", "--llm-key", key],
            "failed to launch: --llm-key " + key)
        self.assertEqual(result, (2, False))
        self.assertNotIn(key, "".join(lines))
        self.assertIn("********", lines[0])

    def test_equals_secret_and_terminal_escape_are_sanitized(self):
        key = "sk-secret-test-456"
        result, lines = self._run_error(
            ["slice", "--api-key=" + key],
            "\x1b[31m--api-key=" + key + "\x1b[0m")
        self.assertEqual(result, (2, False))
        self.assertNotIn(key, lines[0])
        self.assertNotIn("\x1b", lines[0])

    def test_normal_os_error_preserves_diagnostic(self):
        result, lines = self._run_error(["slice"], "permission denied")
        self.assertEqual(result, (2, False))
        self.assertIn("OSError: permission denied", lines[0])


if __name__ == "__main__":
    unittest.main()
