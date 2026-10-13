"""Existing TUI workflow navigation and folder-picker regression tests.

All tests use fake curses + temporary folders; no user media, ffmpeg or API key.
"""
from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import config, tui, tui_runner


class Screen:
    def __init__(self, keys, size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.printed = []

    def getmaxyx(self):
        return self.size

    def getch(self):
        if not self.keys:
            raise AssertionError("unexpected key read: navigation got stuck")
        return self.keys.pop(0)

    def addstr(self, y, x, s, attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w, (y, x)
        assert x + tui._disp_width(s) < w, (x, s, w)
        self.printed.append((y, x, s))

    def refresh(self):
        pass

    def erase(self):
        pass

    def keypad(self, enabled):
        pass

    def timeout(self, millis):
        pass


class NavigationTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_home_has_brand_and_local_settings_address(self):
        screen = Screen([ord("q")])
        with mock.patch.object(tui.curses, "curs_set"):
            self.assertEqual(tui._loop(screen), ("quit", None))
        drawn = "\n".join(s for _, _, s in screen.printed)
        self.assertIn("\n".join(tui._LOGO), drawn)
        self.assertIn("设置 http://127.0.0.1:", drawn)

    def test_home_instructions_are_only_at_bottom_and_never_repeated(self):
        for size in ((14, 60), (24, 80), (32, 100)):
            screen = Screen([ord("q")], size=size)
            with mock.patch.object(tui.curses, "curs_set"):
                self.assertEqual(tui._loop(screen), ("quit", None))
            hints = [(y, text) for y, _, text in screen.printed if "↑↓" in text]
            self.assertEqual(len(hints), 1)
            self.assertEqual(hints[0][0], size[0] - 2)
            self.assertFalse(any("选择现有处理流程" in text or "本地视频工具" in text
                                 for _, _, text in screen.printed))

    def test_home_back_keys_do_not_exit_or_run_jobs(self):
        screen = Screen([27, curses.KEY_LEFT, curses.KEY_BACKSPACE, ord("2")])
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui, "_run_feature_wizard",
                               return_value=[["cover"]]) as dispatch:
            self.assertEqual(tui._loop(screen), ("run", [["cover"]]))
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[1], "cover")
        self.assertEqual(screen.keys, [])

    def test_home_back_on_tiny_terminal_requires_q_to_exit(self):
        screen = Screen([27, curses.KEY_LEFT, ord("q")], size=(12, 40))
        with mock.patch.object(tui.curses, "curs_set"):
            self.assertEqual(tui._loop(screen), ("quit", None))
        self.assertEqual(screen.keys, [])

    def test_native_picker_defaults_to_current_home(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tui.Path, "home", return_value=Path(tmp)), \
             mock.patch.object(tui, "choose_directory",
                               return_value=tmp) as chooser:
            self.assertEqual(
                tui._pick_dir(Screen([]), "选择目录"), tmp
            )
        chooser.assert_called_once_with("选择目录", Path(tmp))

    def test_native_picker_passes_prior_folder_when_backtracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "中文素材"
            child.mkdir()
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(child)) as chooser:
                self.assertEqual(
                    tui._pick_dir(Screen([]), "素材目录", start=root),
                    str(child),
                )
            chooser.assert_called_once_with("素材目录", root)

    def test_native_picker_cancel_returns_to_previous_wizard(self):
        with mock.patch.object(tui, "choose_directory", return_value=None):
            self.assertIsNone(tui._pick_dir(
                Screen([]), "目录", start=Path("/")
            ))

    def test_review_escape_returns_to_main_without_directory_picker(self):
        with mock.patch.object(tui, "_pick_dir") as picker, \
             mock.patch("pycut.core.scan.list_videos_recursive", return_value=[Path("/tmp/video.mp4")]), \
             mock.patch.object(tui, "_confirm", return_value=False):
            self.assertIsNone(tui._wizard_cover(Screen([]), 100))
        picker.assert_not_called()

    def test_review_esc_then_picker_esc_goes_to_main(self):
        with mock.patch.object(tui, "_pick_dir",
                               side_effect=["/tmp/one", None]), \
             mock.patch("pycut.core.scan.list_videos_recursive",
                        return_value=[Path("/tmp/video.mp4")]), \
             mock.patch.object(tui, "_confirm", return_value=False) as confirm:
            self.assertIsNone(tui._wizard_cover(Screen([]), 100))
            confirm.assert_called_once()

    def test_empty_configured_directory_does_not_start_processing(self):
        with mock.patch.object(tui, "_pick_dir") as picker, \
             mock.patch.object(tui, "count_videos_recursive", return_value=0), \
             mock.patch.object(tui, "_flash") as flash, \
             mock.patch.object(tui, "_confirm") as confirm:
            self.assertIsNone(tui._wizard_dedup(Screen([]), 100))
        picker.assert_not_called()
        flash.assert_called_once()
        confirm.assert_not_called()

    def test_montage_back_from_final_review_preserves_fission(self):
        video = Path("/tmp/sources/婚礼.mp4")
        selection = ([video], {str(video): 12})
        with mock.patch.object(tui, "_pick_dir",
                               side_effect=["/tmp/sources", "/tmp/clips", "/tmp/pip"]), \
             mock.patch("pycut.core.montage_clips._collect_files", return_value=[Path("candidate.mp4")] * 2), \
             mock.patch.object(tui, "list_videos_flat", return_value=[video]), \
             mock.patch.object(tui, "_multi_select",
                               return_value=selection) as multiselect, \
             mock.patch.object(tui, "_count_sticker_images", return_value=1), \
             mock.patch.object(tui, "_set_fission_counts", return_value=True), \
             mock.patch.object(tui, "_confirm", side_effect=[False, True]):
            commands = tui._wizard_montage(Screen([]), 100)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][-2:], ["--fission", "12"])
        self.assertEqual(multiselect.call_count, 1)
        self.assertIsNone(multiselect.call_args_list[0].kwargs["initial"])
        self.assertEqual(selection[1][str(video)], 12)

    def test_montage_reads_configured_folders_without_picker(self):
        video = Path("/tmp/source/test.mp4")
        with mock.patch.object(config, "MONTAGE_CLIP_DIR", Path("/tmp/clips")), \
             mock.patch.object(config, "MONTAGE_DEDUP_DIR", Path("/tmp/pip")), \
             mock.patch.object(tui, "_pick_dir") as picker, \
             mock.patch("pycut.core.montage_clips._collect_files", return_value=[Path("candidate.mp4")] * 1), \
             mock.patch.object(tui, "list_videos_flat", return_value=[video]), \
             mock.patch.object(tui, "_multi_select", return_value=([video], {str(video):3})), \
             mock.patch.object(tui, "_set_fission_counts", return_value=True), \
             mock.patch.object(tui, "_confirm", return_value=True):
            commands = tui._wizard_montage(Screen([]), 100)
        picker.assert_not_called()
        self.assertIn("/tmp/clips", commands[0])
        self.assertIn("/tmp/pip", commands[0])
        self.assertIn("--intro-dir", commands[0])

    def test_multi_select_preserves_existing_selection_when_backtracking(self):
        first = Path("/tmp/a.mp4")
        second = Path("/tmp/b.mp4")
        selected = ([second], {str(second): 14})
        result = tui._multi_select(Screen([10]), "已选视频",
                                   [first, second], initial=selected)
        self.assertEqual(result, selected)

    def test_result_page_esc_returns_menu_but_q_exits(self):
        for key, expected_quit in ((27, False), (ord("q"), True)):
            with self.subTest(key=key), \
                 mock.patch.object(tui_runner, "_run_one",
                                   return_value=(0, False)), \
                 mock.patch.object(tui_runner, "_draw"):
                self.assertEqual(
                    tui_runner.run_jobs(Screen([key]), [["slice"]]),
                    (0, expected_quit),
                )


if __name__ == "__main__":
    unittest.main()
