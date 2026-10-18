"""退出码契约回归：错误输出不能误报成功，失败不能运行依赖任务。

所有测试只使用临时的非视频文本文件及 mock，不执行 FFmpeg/LLM。
"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, tui, tui_runner
from pycut.commands import montage_cmd


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "source.mp4"
        self.source.write_bytes(b"dummy-input")

    def _run_montage(self, step, fission, responses):
        # 以真实 CLI parser 创建 args，直接测试命令入口的返回契约。
        argv = ["montage", "--source", str(self.source), "--no-sub",
                "--step", step, "--fission", str(fission)]
        args = cli.build_parser().parse_args(argv)
        with mock.patch.object(montage_cmd, "install_signal_handlers"), \
             mock.patch.object(montage_cmd, "reset_stop"), \
             mock.patch.object(montage_cmd, "self_check"), \
             mock.patch.object(montage_cmd, "_resolve_srt", return_value=""), \
             mock.patch("pycut.commands.montage_pipeline.run_pipeline",
                        side_effect=responses) as pipeline, \
             mock.patch.object(montage_cmd, "log"):
            return montage_cmd.run(args), pipeline.call_count

    @staticmethod
    def _success_result(final="output.mp4"):
        return {
            "error": "", "final": final, "source_duration": 12.0,
            "segments": 3, "clips_used": 4, "final_duration": 12.0,
            "pip_count": 0, "clips_reused": 0,
        }

    def test_montage_error_short_circuits_before_fission(self):
        rc, calls = self._run_montage("all", 5, [{"error": "decode failed"}])
        self.assertEqual((rc, calls), (2, 1))

    def test_montage_missing_required_final_is_nonzero(self):
        rc, calls = self._run_montage("all", 5, [self._success_result(final="")])
        self.assertEqual((rc, calls), (2, 1))

    def test_step_a_may_succeed_without_final_video(self):
        # --step a 仅提取基准，不会生成视频，空 final 属于正常契约。
        rc, calls = self._run_montage("a", 1, [self._success_result(final="")])
        self.assertEqual((rc, calls), (0, 1))

    def test_second_fission_failure_makes_command_nonzero(self):
        first = self._success_result()
        rc, calls = self._run_montage("all", 2, [first, {"error": "encode failed"}])
        self.assertEqual((rc, calls), (2, 2))

    def test_cli_failure_stops_chained_dedup(self):
        screen = mock.Mock()
        screen.getch.return_value = 10
        commands = [["cover", "--all"], ["dedup", "--all"]]
        for argv in commands:
            cli.build_parser().parse_args(argv)
        with mock.patch.object(tui_runner, "_run_one", return_value=(2, False)) as run, \
             mock.patch.object(tui_runner, "_draw"):
            result = tui_runner.run_jobs(screen, commands)
        self.assertEqual(result, (2, False))
        run.assert_called_once()

    def test_tui_startup_failure_is_nonzero_and_normal_quit_zero(self):
        with mock.patch.object(tui.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui.sys, "stdout", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui.sys, "stderr", io.StringIO()) as stderr, \
             mock.patch.object(tui.curses, "wrapper", side_effect=OSError("bad tty")):
            self.assertEqual(tui.run(), 2)
            self.assertIn("bad tty", stderr.getvalue())

        with mock.patch.object(tui.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui.sys, "stdout", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui.curses, "wrapper",
                               side_effect=lambda fn: fn(mock.Mock())), \
             mock.patch.object(tui, "_init_colors"), \
             mock.patch.object(tui, "_loop", return_value=("quit", None)):
            self.assertEqual(tui.run(), 0)


if __name__ == "__main__":
    unittest.main()
