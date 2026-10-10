"""Keyboard, contrast and Esc latency regression for the existing four-step TUI.

Fake curses only; no external media, subprocess, or network.
"""
from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import __version__, tui


class Screen:
    def __init__(self, keys=(), size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.drawn = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        pass

    def refresh(self):
        pass

    def keypad(self, value):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("unexpected key read or blocked navigation")
        return self.keys.pop(0)

    def addstr(self, y, x, s, attrs=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w
        assert x + tui._disp_width(s) < w, (y, x, s, w)
        self.drawn.append((y, x, s, attrs))


class KeymapTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_esc_delay_is_75ms_not_default_second(self):
        with mock.patch.dict(tui.os.environ, {}, clear=True), \
             mock.patch.object(tui.curses, "set_escdelay") as delay:
            tui._configure_esc_delay()
            delay.assert_called_once_with(75)

    def test_explicit_escdelay_setting_is_respected(self):
        with mock.patch.dict(tui.os.environ, {"ESCDELAY": "250"}), \
             mock.patch.object(tui.curses, "set_escdelay") as delay:
            tui._configure_esc_delay()
            delay.assert_not_called()

    def test_standard_header_and_footer_avoid_reverse_video_bars(self):
        screen = Screen()
        tui._frame(screen, "混剪 · 选择素材", "/Users/apple/Desktop")
        tui._footer(
            screen, "↑↓移动 · " + "很多按键 · " * 20,
            "↑↓移动  ←上级  →进入  Space选目录  Esc返回"
        )
        self.assertTrue(any(f"v{__version__}" in s for _, _, s, _ in screen.drawn))
        self.assertTrue(any("←上级" in s and "Esc返回" in s
                            for _, _, s, _ in screen.drawn))
        self.assertFalse(any(attr & curses.A_REVERSE
                             for _, _, _, attr in screen.drawn))

    def test_main_menu_right_enters_and_left_is_not_exit(self):
        screen = Screen([curses.KEY_LEFT, curses.KEY_DOWN, curses.KEY_RIGHT])
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui, "_run_feature_wizard",
                               return_value=[["cover"]]) as wizard:
            result = tui._loop(screen)
        self.assertEqual(result, ("run", [["cover"]]))
        self.assertEqual(wizard.call_args.args[1], "cover")

    def test_main_menu_space_does_not_launch_an_action(self):
        screen = Screen([ord(" "), ord("q")])
        with mock.patch.object(tui.curses, "curs_set"), \
             mock.patch.object(tui, "_run_feature_wizard") as wizard:
            self.assertEqual(tui._loop(screen), ("quit", None))
        wizard.assert_not_called()

    def test_picker_right_enters_child_and_right_confirms_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "素材"
            target.mkdir()
            # With an explicit start, "use current folder" has focus.
            screen = Screen([curses.KEY_UP, curses.KEY_RIGHT, curses.KEY_RIGHT])
            self.assertEqual(tui._pick_dir(screen, "目录", start=root),
                             str(target))

    def test_picker_left_reopens_parent_without_selecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "素材"
            child.mkdir()
            screen = Screen([curses.KEY_LEFT, ord(" ")])
            self.assertEqual(tui._pick_dir(screen, "目录", start=child),
                             str(root))

    def test_picker_down_does_not_wrap_away_from_current_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            screen = Screen([curses.KEY_DOWN, ord(" ")])
            self.assertEqual(tui._pick_dir(screen, "目录", start=root),
                             str(root))

    def test_video_space_multiselect_and_right_confirm(self):
        files = [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")]
        screen = Screen([ord(" "), curses.KEY_DOWN, ord(" "),
                         curses.KEY_RIGHT])
        chosen, counts = tui._multi_select(screen, "混剪 · 选择视频", files)
        self.assertEqual(chosen, files)
        self.assertEqual(counts, {str(files[0]): 1, str(files[1]): 1})
        self.assertTrue(any("Space" in s and "→" in s
                            for _, _, s, _ in screen.drawn))

    def test_video_left_or_esc_returns_without_starting_job(self):
        files = [Path("/tmp/a.mp4")]
        self.assertIsNone(tui._multi_select(
            Screen([ord(" "), curses.KEY_LEFT]), "视频", files
        ))
        self.assertIsNone(tui._multi_select(Screen([27]), "视频", files))

    def test_confirmation_left_is_back_and_right_only_focuses_action(self):
        opts = [("字幕", "擦除", True)]
        self.assertFalse(tui._confirm(Screen([curses.KEY_LEFT]),
                                      [("目录", "/tmp")], opts))
        self.assertFalse(tui._confirm(
            Screen([curses.KEY_RIGHT, 27]), [("目录", "/tmp")], opts
        ))

    def test_confirmation_right_then_enter_is_explicit_start(self):
        opts = [("字幕", "擦除", True)]
        flags = []
        self.assertTrue(tui._confirm(
            Screen([curses.KEY_RIGHT, 10]), [("目录", "/tmp")], opts, flags
        ))
        self.assertEqual(flags, [True])


if __name__ == "__main__":
    unittest.main()
