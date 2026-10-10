"""Regression: redaction must precede the 4096-character log display cap."""
from __future__ import annotations

from collections import deque
import io
import unittest
from unittest import mock

from pycut import tui_runner


class FailingStream:
    def __init__(self, error: str):
        self.error = error
        self.closed = False

    def __iter__(self):
        raise OSError(self.error)
        yield ""  # make this a generator; error occurs in the reader thread

    def close(self):
        self.closed = True


class LongLogSecretTests(unittest.TestCase):
    KEY = "sk-private-test-token"

    @classmethod
    def boundary_line(cls, message_prefix_len: int = 0) -> str:
        # Old early truncation cuts off the last key character, breaking redaction.
        prefix = "x" * (
            tui_runner._MAX_LOG_CHARS - len(cls.KEY) + 1 - message_prefix_len
        )
        # Extra output keeps the final (redacted) line longer than the display cap.
        return prefix + cls.KEY + "z" * 200

    def assert_masked(self, lines: deque[str]) -> None:
        displayed = "\n".join(lines)
        self.assertNotIn(self.KEY, displayed)
        self.assertNotIn(self.KEY[:-1], displayed)
        self.assertIn("********", displayed)
        self.assertIn("已截断", displayed)
        for line in lines:
            self.assertLessEqual(len(line), tui_runner._MAX_LOG_CHARS + len("… [日志行已截断]"))

    def test_child_stdout_redacts_before_truncation(self):
        child = mock.Mock()
        child.stdout = io.StringIO(self.boundary_line() + "\n")
        child.poll.return_value = 0
        screen = mock.Mock()
        screen.getch.return_value = -1
        logs = deque(maxlen=20)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=child), \
             mock.patch.object(tui_runner, "_draw"):
            result = tui_runner._run_one(
                screen, ["slice", "--llm-key", self.KEY], 1, 1, logs
            )
        self.assertEqual(result, (0, False))
        self.assert_masked(logs)

    def test_launch_exception_redacts_before_truncation(self):
        logs = deque(maxlen=10)
        with mock.patch.object(
            tui_runner.subprocess, "Popen", side_effect=OSError(self.boundary_line(len("[无法启动] OSError: ")))
        ):
            result = tui_runner._run_one(
                mock.Mock(), ["slice", "--api-key=" + self.KEY], 1, 1, logs
            )
        self.assertEqual(result, (2, False))
        self.assert_masked(logs)

    def test_reader_exception_redacts_before_truncation(self):
        stream = FailingStream(
            self.boundary_line(len("[日志读取失败] OSError: "))
        )
        child = mock.Mock(stdout=stream)
        child.poll.return_value = 0
        screen = mock.Mock()
        screen.getch.return_value = -1
        logs = deque(maxlen=10)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=child), \
             mock.patch.object(tui_runner, "_draw"):
            result = tui_runner._run_one(
                screen, ["slice", "--llm-key", self.KEY], 1, 1, logs
            )
        self.assertEqual(result, (2, False))
        self.assertTrue(stream.closed)
        self.assert_masked(logs)

    def test_osc_split_secret_and_default_sanitize_cap(self):
        injected = self.KEY[:3] + "\x1b]0;window title\x07" + self.KEY[3:]
        self.assertEqual(
            tui_runner._safe_log_line(injected, ["slice", "--llm-key", self.KEY]),
            "********",
        )
        capped = tui_runner._sanitize("x" * 6000)
        self.assertEqual(len(capped), tui_runner._MAX_LOG_CHARS + len("… [日志行已截断]"))
        self.assertIn("已截断", capped)

    def test_short_secret_still_hides_entire_line(self):
        self.assertEqual(
            tui_runner._safe_log_line("failed to launch", ["slice", "--llm-key", "a"]),
            "[日志已隐藏：包含敏感参数]",
        )


if __name__ == "__main__":
    unittest.main()
