"""无需外部视频、ffmpeg、网络或 API 密钥的 TUI 端到端交互回归。

覆盖目录导航、确认开关、五项功能的 CLI 映射、取消/窄窗口与任务循环。
"""

from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, tui, tui_runner


class FakeScreen:
    """把键盘和画布边界变成可重复断言的最小 curses 协议。"""

    def __init__(self, keys=(), size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.printed = []
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, attr=0):
        assert 0 <= y < self.size[0], (y, self.size)
        assert 0 <= x < self.size[1], (x, self.size)
        assert x + tui._disp_width(value) < self.size[1], (x, value, self.size)
        self.printed.append((y, x, value))

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def get_wch(self):
        return self.getch()

    def erase(self):
        pass

    def move(self, y, x):
        pass

    def clrtoeol(self):
        pass

    def refresh(self):
        pass

    def keypad(self, enabled):
        pass

    def timeout(self, delay):
        self.timeouts.append(delay)


class TUIIntegrationTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_directory_browser_enters_and_selects_unicode_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "婚礼素材"
            target.mkdir()
            # 初始焦点位于第一个子目录，Enter 进入后再 Enter 选定。
            self.assertEqual(
                tui._pick_dir(FakeScreen([10, 10]), "选择目录", start=root),
                str(target),
            )
            # 在子目录按左方向键返回，然后选当前目录。
            self.assertEqual(
                tui._pick_dir(FakeScreen([curses.KEY_LEFT, 10]), "选择目录", start=target),
                str(root),
            )
            self.assertIsNone(
                tui._pick_dir(FakeScreen([27]), "选择目录", start=root)
            )

    def test_confirm_syncs_values_and_esc_never_runs(self):
        toggles = [("字幕", "提示", True), ("去重", "提示", False)]
        values = []
        scr = FakeScreen([ord(" "), curses.KEY_DOWN, ord(" "), curses.KEY_DOWN, 10])
        self.assertTrue(tui._confirm(scr, [("输入", "/tmp")], toggles, values))
        self.assertEqual(values, [False, True])
        cancelled = []
        self.assertFalse(tui._confirm(FakeScreen([27]), [], toggles, cancelled))
        self.assertEqual(cancelled, [True, False])

    def test_cover_and_dedup_create_parseable_commands(self):
        for wizard, command in ((tui._wizard_cover, "cover"),
                                (tui._wizard_dedup, "dedup")):
            with self.subTest(command=command), \
                 mock.patch.object(tui, "_pick_dir", return_value="/tmp/videos"), \
                 mock.patch.object(tui, "count_videos_recursive", return_value=3), \
                 mock.patch("pycut.core.scan.list_videos_recursive",
                            return_value=[Path("/tmp/videos/clip.mp4"),
                                          Path("/tmp/videos/not-supported.mov")]), \
                 mock.patch.object(tui, "_confirm", return_value=True):
                result = wizard(FakeScreen(), 100)
                self.assertEqual(result, [[command, "--src-dir", "/tmp/videos"]])
                args = cli.build_parser().parse_args(result[0])
                self.assertEqual(args.command, command)
                self.assertEqual(args.src_dir, "/tmp/videos")

    def test_no_video_short_circuits_before_confirm(self):
        for wizard in (tui._wizard_cover, tui._wizard_dedup):
            with self.subTest(wizard=wizard.__name__), \
                 mock.patch.object(tui, "_pick_dir", return_value="/tmp/empty"), \
                 mock.patch.object(tui, "count_videos_recursive", return_value=0), \
                 mock.patch.object(tui, "_flash") as flash, \
                 mock.patch.object(tui, "_confirm") as confirm:
                self.assertIsNone(wizard(FakeScreen(), 100))
                flash.assert_called_once()
                confirm.assert_not_called()

    def test_montage_creates_one_command_per_selected_source(self):
        first = Path("/tmp/video/第一支.mp4")
        second = Path("/tmp/video/second.mp4")
        selection = ([first, second], {str(first): 12, str(second): 2})
        with mock.patch.object(tui, "_pick_dir",
                               side_effect=["/tmp/clips", "/tmp/pip", "/tmp/video"]), \
             mock.patch.object(tui, "count_videos_recursive", side_effect=[4, 0]), \
             mock.patch.object(tui, "list_videos_flat", return_value=[first, second]), \
             mock.patch.object(tui, "_multi_select", return_value=selection), \
             mock.patch.object(tui, "_confirm", return_value=True):
            commands = tui._wizard_montage(FakeScreen(), 100)
        self.assertEqual(len(commands), 2)
        for argv, source, count in zip(commands, (first, second), (12, 2)):
            args = cli.build_parser().parse_args(argv)
            self.assertEqual(args.command, "montage")
            self.assertEqual(args.source, str(source))
            self.assertEqual(args.clip_dir, "/tmp/clips")
            self.assertEqual(args.dedup_dir, "/tmp/pip")
            self.assertEqual(args.fission, count)

    def test_montage_cancel_before_confirm_never_builds_jobs(self):
        with mock.patch.object(tui, "_pick_dir", side_effect=["/tmp/clips", None]), \
             mock.patch.object(tui, "count_videos_recursive", return_value=4), \
             mock.patch.object(tui, "_confirm") as confirm, \
             mock.patch.object(tui, "_multi_select") as multi:
            self.assertIsNone(tui._wizard_montage(FakeScreen(), 100))
            confirm.assert_not_called()
            multi.assert_not_called()

    def test_unicode_search_displays_results_and_returns_to_menu(self):
        result = Path("/tmp/视频素材/婚礼现场.mp4")
        scr = FakeScreen([curses.KEY_DOWN, 27])
        with mock.patch.object(tui, "_pick_dir", return_value="/tmp/视频素材"), \
             mock.patch.object(tui, "_prompt_text", return_value="婚礼"), \
             mock.patch.object(tui, "search_videos", return_value=[result]) as search:
            self.assertIsNone(tui._wizard_search(scr, 100))
            search.assert_called_once_with("/tmp/视频素材", "婚礼")
        self.assertTrue(any("婚礼现场" in s for _, _, s in scr.printed))

    def test_narrow_screen_allows_cancel_without_out_of_bounds(self):
        tiny = (12, 40)
        self.assertFalse(tui._confirm(FakeScreen([27], size=tiny), []))
        self.assertIsNone(tui._multi_select(
            FakeScreen([27], size=tiny), "混剪", [Path("/tmp/a.mp4")]
        ))
        with mock.patch.object(tui.curses, "curs_set"):
            self.assertEqual(tui._loop(FakeScreen([27], size=tiny))[0], "quit")
        self.assertIsNone(
            tui._pick_dir(FakeScreen([27], size=tiny), "路径", start=Path("/"))
        )

    def test_runner_start_failure_returns_nonzero_and_stays_in_tui(self):
        with mock.patch.object(tui_runner.subprocess, "Popen",
                               side_effect=OSError("spawn failed")):
            result = tui_runner.run_jobs(
                FakeScreen([10]), [["slice", "--no-sub"], ["dedup", "--all"]]
            )
        self.assertEqual(result, (2, False))

    def test_main_menu_reopens_after_successful_job(self):
        from pycut import tui_runner
        with mock.patch.object(tui.sys, "stdin", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui.sys, "stdout", mock.Mock(isatty=lambda: True)), \
             mock.patch.object(tui, "_init_colors"), \
             mock.patch.object(tui.curses, "wrapper",
                               side_effect=lambda fn: fn(FakeScreen())) as wrapper, \
             mock.patch.object(tui, "_loop",
                               side_effect=[("run", [["slice", "--no-sub"]]),
                                            ("quit", None)]) as loop, \
             mock.patch.object(tui_runner, "run_jobs", return_value=(0, False)) as run:
            self.assertEqual(tui.run(), 0)
            self.assertEqual(loop.call_count, 2)
            run.assert_called_once()
            wrapper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
