"""Offline TUI subprocess-log safety and result-page regression; no real media/terminal."""

from __future__ import annotations

from collections import deque
import io
import unittest
from unittest import mock

from pycut import tui_runner


class RunnerTests(unittest.TestCase):
    def test_strip_csi_osc_and_cap_unbounded_log_lines(self):
        self.assertEqual(tui_runner._sanitize("A\x1b[31m红\x1b[0mB"), "A红B")
        self.assertEqual(tui_runner._sanitize("A\x1b[?25lB"), "AB")
        self.assertEqual(tui_runner._sanitize("a\x1b]0;terminal title\x07b"), "ab")
        huge = tui_runner._sanitize("中" * 6000)
        self.assertLess(len(huge), 4200)
        self.assertIn("已截断", huge)
        self.assertNotIn("\x1b", huge)

    def test_child_stdout_does_not_expose_explicit_api_key(self):
        key = "sk-private-token"
        argv = ["slice", "--llm-key", key]
        self.assertEqual(
            tui_runner._redact("error with sk-private-token", argv),
            "error with ********",
        )
        self.assertEqual(
            tui_runner._redact("error: sk-private-token",
                               ["slice", "--llm-key=" + key]),
            "error: ********",
        )
        self.assertNotIn(key, tui_runner._display_command(argv))

        fake = mock.Mock()
        fake.stdout = io.StringIO("error with sk-private-token\n")
        fake.poll.return_value = 0
        screen = mock.Mock()
        screen.getch.return_value = -1
        history = deque(maxlen=10)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=fake), \
             mock.patch.object(tui_runner, "_draw"):
            self.assertEqual(
                tui_runner._run_one(screen, argv, 1, 1, history), (0, False)
            )
        self.assertTrue(any("********" in line for line in history))
        self.assertFalse(any(key in line for line in history))

    def test_failure_summary_has_real_code_and_skipped_count(self):
        screen = mock.Mock()
        screen.getch.return_value = 10
        commands = [["slice"], ["dedup", "--all"], ["montage"]]
        with mock.patch.object(tui_runner, "_run_one",
                               side_effect=[(0, False), (2, False)]) as run, \
             mock.patch.object(tui_runner, "_draw") as draw:
            self.assertEqual(tui_runner.run_jobs(screen, commands), (2, False))
        self.assertEqual(run.call_count, 2)
        summary = list(draw.call_args.args[4])
        self.assertTrue(any("已运行 2/3" in line and "退出码 2" in line
                            for line in summary))
        self.assertTrue(any("跳过 1" in line for line in summary))
        self.assertEqual(draw.call_args.args[6], "error")

    def test_cancel_with_zero_code_is_still_a_cancelled_failure(self):
        screen = mock.Mock()
        screen.getch.return_value = 10
        with mock.patch.object(tui_runner, "_run_one", return_value=(0, True)) as run, \
             mock.patch.object(tui_runner, "_draw") as draw:
            self.assertEqual(tui_runner.run_jobs(screen, [["slice"], ["dedup"]]),
                             (130, False))
        run.assert_called_once()
        self.assertEqual(draw.call_args.args[6], "cancelled")

    def test_empty_job_list_never_enters_results_loop(self):
        with mock.patch.object(tui_runner, "_draw") as draw:
            self.assertEqual(tui_runner.run_jobs(None, []), (0, False))
        draw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
