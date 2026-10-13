"""TUI runtime stdin ownership and compact controls regression tests."""
from __future__ import annotations

from collections import deque
import io
import os
import subprocess
import unittest
from unittest import mock

from pycut import tui, tui_runner


class RuntimeIsolationTests(unittest.TestCase):
    def test_child_never_inherits_curses_keyboard_input(self):
        child = mock.Mock()
        child.stdout = io.StringIO("started\n")
        child.poll.return_value = 0
        screen = mock.Mock()
        screen.getch.return_value = -1
        logs = deque(maxlen=10)
        with mock.patch.object(tui_runner.subprocess, "Popen", return_value=child) as popen, \
             mock.patch.object(tui_runner, "_draw"):
            self.assertEqual(tui_runner._run_one(
                screen, ["--version"], 1, 1, logs), (0, False))
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["start_new_session"] if os.name != "nt"
                        else not kwargs["start_new_session"])
        self.assertEqual(list(logs), ["started"])

    def test_run_state_uses_shared_footer_in_compact_terminal(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (20, 60)
        with mock.patch.object(tui, "_small_screen", return_value=False), \
             mock.patch.object(tui, "_frame", return_value=9), \
             mock.patch.object(tui, "_safe_add"), \
             mock.patch.object(tui, "_footer") as footer:
            tui_runner._draw(screen, ["slice"], 1, 2,
                             deque(["hello"]), 0, "running")
        self.assertIn("X中断", footer.call_args.args[2])
        self.assertIn("Ctrl+C", footer.call_args.args[1])

    def test_result_state_keeps_enter_and_quit_visible(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (20, 60)
        with mock.patch.object(tui, "_small_screen", return_value=False), \
             mock.patch.object(tui, "_frame", return_value=9), \
             mock.patch.object(tui, "_safe_add"), \
             mock.patch.object(tui, "_footer") as footer:
            tui_runner._draw(screen, ["slice"], 1, 1,
                             deque(["finished"]), 0, "success")
        self.assertIn("Enter主菜单", footer.call_args.args[2])
        self.assertIn("Q退出", footer.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
