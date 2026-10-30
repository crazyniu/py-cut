"""Offline fake-screen coverage of wizard CLI mapping, navigation, cancellation, and task loops."""

from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import settings, cli, config, tui, tui_runner


class FakeScreen:
    """Deterministic keyboard and canvas-boundary assertions for wizard tests."""

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
        for name, value in [('PIP_DIRS', []), ('CLIP_DIRS', [Path('/tmp/clips')])]:
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_native_directory_picker_unicode_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "婚礼素材"
            target.mkdir()
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(target)) as chooser:
                self.assertEqual(
                    tui._pick_dir(FakeScreen(), "选择目录", start=root),
                    str(target),
                )
            chooser.assert_called_once_with("选择目录", root)
            with mock.patch.object(tui, "choose_directory", return_value=None):
                self.assertIsNone(
                    tui._pick_dir(FakeScreen(), "选择目录", start=root)
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
                 mock.patch.object(config, "CLIP_DIRS", [Path("/tmp/videos")]), \
                 mock.patch.object(tui, "list_videos_recursive",
                            return_value=[Path("/tmp/videos/clip.mp4"),
                                          Path("/tmp/videos/not-supported.mov")]), \
                 mock.patch.object(tui, "_confirm", return_value=True):
                result = wizard(FakeScreen(), 100)
                self.assertEqual(result, [[command, "--src-dir", settings.encoded_directories([Path("/tmp/videos")])]])
                args = cli.build_parser().parse_args(result[0])
                self.assertEqual(args.command, command)
                self.assertEqual(settings.directories(args.src_dir), [Path("/tmp/videos").resolve()])

    def test_no_video_short_circuits_before_confirm(self):
        for wizard in (tui._wizard_cover, tui._wizard_dedup):
            with self.subTest(wizard=wizard.__name__), \
                 mock.patch.object(tui, "list_videos_recursive", return_value=[]), \
                 mock.patch.object(tui, "_flash") as flash, \
                 mock.patch.object(tui, "_confirm") as confirm:
                self.assertIsNone(wizard(FakeScreen(), 100))
                flash.assert_called_once()
                confirm.assert_not_called()

    def test_montage_creates_one_command_per_selected_source(self):
        first = Path("/tmp/video/第一支.mp4")
        second = Path("/tmp/video/second.mp4")
        selection = ([first, second], {str(first): 12, str(second): 2})
        with mock.patch.dict("os.environ", {"PYCUT_PIP_DIR": "/tmp/pip"}), \
             mock.patch("pycut.settings.problems", return_value=[]), \
             mock.patch.object(config, "CLIP_DIRS", [Path("/tmp/clips")]), \
             mock.patch.object(config, "PIP_DIRS", [Path("/tmp/pip")]), \
             mock.patch("pycut.core.montage_clips._collect_files", side_effect=[[first] * 4, [second] * 3]), \
             mock.patch.object(tui, "list_videos_recursive", return_value=[first, second]), \
             mock.patch.object(tui, "_multi_select", return_value=selection), \
             mock.patch.object(tui, "_set_fission_counts", return_value=True), \
             mock.patch.object(tui, "_confirm", return_value=True):
            commands = tui._wizard_montage(FakeScreen(), 100)
        self.assertEqual(len(commands), 2)
        for argv, source, count in zip(commands, (first, second), (12, 2)):
            args = cli.build_parser().parse_args(argv)
            self.assertEqual(args.command, "montage")
            self.assertEqual(args.source, str(source))
            self.assertEqual(settings.directories(args.clip_dir), [Path("/tmp/clips").resolve()])
            self.assertEqual(settings.directories(args.dedup_dir), [Path("/tmp/pip").resolve()])
            self.assertEqual(args.fission, count)

    def test_montage_defaults_to_online_stickers_without_extra_picker(self):
        with tempfile.TemporaryDirectory() as temp:
            sticker_root = Path(temp) / "stickers"
            sticker_root.mkdir()
            (sticker_root / "logo.png").write_bytes(b"placeholder")
            video = Path("/tmp/video/one.mp4")
            selection = ([video], {str(video): 1})
            with mock.patch.object(tui, "_pick_dir", side_effect=[
                     "/tmp/video", "/tmp/clips", "/tmp/pip"]) as picker, \
                 mock.patch("pycut.core.montage_clips._collect_files", return_value=[Path("candidate.mp4")] * 2), \
                 mock.patch.object(tui, "list_videos_recursive", return_value=[video]), \
                 mock.patch.object(tui, "_multi_select", return_value=selection), \
                 mock.patch.object(tui, "_set_fission_counts", return_value=True), \
             mock.patch.object(tui, "_confirm", return_value=True):
                commands = tui._wizard_montage(FakeScreen(), 100)
        self.assertEqual(len(commands), 1)
        args = cli.build_parser().parse_args(commands[0])
        self.assertEqual(args.sticker_dir, "")
        self.assertFalse(args.no_sticker)
        picker.assert_not_called()

    def test_montage_cancel_before_confirm_never_builds_jobs(self):
        with mock.patch.object(tui, "list_videos_recursive", return_value=[Path("reference.mp4")]), \
             mock.patch("pycut.core.montage_clips._collect_files", return_value=[Path("candidate.mp4")] * 4), \
             mock.patch.object(tui, "_confirm") as confirm, \
             mock.patch.object(tui, "_multi_select", return_value=None) as multi:
            self.assertIsNone(tui._wizard_montage(FakeScreen(), 100))
            confirm.assert_not_called()
            multi.assert_called_once()

    def test_only_processing_features_are_accessible_from_main_menu(self):
        self.assertEqual(
            [key for key, _, _ in tui.FEATURES],
            ["cover", "dedup", "montage", "fission"],
        )
        # The former search shortcut "5" must not dispatch an unavailable item.
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui, "_run_feature_wizard") as dispatch:
            result = tui._loop(FakeScreen([ord("5"), 27, ord("q")]))
        self.assertEqual(result, ("quit", None))
        dispatch.assert_not_called()

    def test_saved_menu_focus_outside_current_features_is_clamped(self):
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui._loop, "_feat_sel", 99), \
             mock.patch.object(tui, "_run_feature_wizard",
                               return_value=[["fission"]]) as dispatch:
            result = tui._loop(FakeScreen([10]))
        self.assertEqual(result, ("run", [["fission"]]))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], "fission")

    def test_narrow_screen_allows_cancel_without_out_of_bounds(self):
        tiny = (12, 40)
        self.assertFalse(tui._confirm(FakeScreen([27], size=tiny), []))
        self.assertIsNone(tui._multi_select(
            FakeScreen([27], size=tiny), "混剪", [Path("/tmp/a.mp4")]
        ))
        with mock.patch.object(tui.curses, "curs_set"):
            self.assertEqual(tui._loop(FakeScreen([27, ord("q")], size=tiny))[0], "quit")
        with mock.patch.object(tui, "choose_directory", return_value=None):
            self.assertIsNone(
                tui._pick_dir(FakeScreen([27], size=tiny),
                              "路径", start=Path("/"))
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
