"""FFmpeg subprocess wrapper must not consume TUI keystrokes or mask IO errors."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from pycut.core import utils


class SubprocessSafetyTests(unittest.TestCase):
    def test_commands_use_devnull_stdin_and_keep_output(self):
        process = SimpleNamespace(
            returncode=0, stdout="输出".encode("utf-8"),
            stderr="提示".encode("utf-8"))
        with mock.patch.object(utils.subprocess, "run",
                               return_value=process) as runner:
            self.assertEqual(
                utils.run_cmd(["ffprobe", "video.mp4"], 30),
                (0, "输出", "提示"))
        self.assertEqual(runner.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(runner.call_args.kwargs["timeout"], 30)
        self.assertNotIn("shell", runner.call_args.kwargs)

    def test_permission_errors_are_reported_without_uncaught_exception(self):
        with mock.patch.object(utils.subprocess, "run",
                               side_effect=PermissionError("denied")):
            code, out, err = utils.run_cmd(["ffmpeg"], 30)
        self.assertEqual(code, -1)
        self.assertEqual(out, "")
        self.assertIn("denied", err)

    def test_timeout_and_missing_command_still_report_failure(self):
        for error in (FileNotFoundError("missing"),
                      subprocess.TimeoutExpired(["ffprobe"], 3)):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(utils.subprocess, "run", side_effect=error):
                code, _out, err = utils.run_cmd(["ffprobe"], 3)
                self.assertEqual(code, -1)
                self.assertTrue(err)

    def test_timeout_preserves_diagnostics_emitted_before_stall(self):
        error = subprocess.TimeoutExpired(
            ["ffmpeg"], 3, output="已输出".encode(),
            stderr=b"[decoder] corrupt frame\n[filter] waiting for input\n")
        with mock.patch.object(utils.subprocess, "run", side_effect=error):
            code, out, err = utils.run_cmd(["ffmpeg"], 3)
        self.assertEqual((code, out), (-1, "已输出"))
        self.assertIn("timeout after 3s", err)
        self.assertIn("[decoder] corrupt frame", err)
        self.assertIn("[filter] waiting for input", err)

    def test_real_child_timeout_keeps_partial_output_and_error(self):
        code, out, err = utils.run_cmd([
            sys.executable, "-c",
            "import os,time; os.write(1,b'partial output'); "
            "os.write(2,b'decoder diagnostic'); time.sleep(30)"], .5)
        self.assertEqual((code, out), (-1, "partial output"))
        self.assertIn("timeout after 0.5s", err)
        self.assertIn("decoder diagnostic", err)


if __name__ == "__main__":
    unittest.main()
