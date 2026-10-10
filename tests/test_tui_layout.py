"""Responsive TUI rendering and editing regression tests (no terminal/media needed)."""
from __future__ import annotations

import curses
from pathlib import Path
import unittest
from unittest import mock

from pycut import tui


class Screen:
    def __init__(self, keys=(), size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.printed = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        pass

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def move(self, y, x):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w, (y, x, self.size)

    def clrtoeol(self):
        pass

    def addstr(self, y, x, text, _attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w, (y, x, self.size)
        assert x + tui._disp_width(text) < w, (x, text, self.size)
        self.printed.append((y, x, text))

    def getch(self):
        return self.keys.pop(0) if self.keys else 27

    def get_wch(self):
        return self.getch()


class LayoutTests(unittest.TestCase):
    def setUp(self):
        attr = mock.patch.object(tui, "_attr", return_value=0)
        attr.start()
        self.addCleanup(attr.stop)

    def test_unicode_ellipsize_respects_terminal_columns(self):
        s = "测试非常非常非常长的路径视频文件.mp4"
        for width in (1, 2, 4, 13, 27):
            for tail in (False, True):
                clipped = tui._ellipsize(s, width, tail=tail)
                self.assertLessEqual(tui._disp_width(clipped), width)
                if tui._disp_width(s) > width:
                    self.assertTrue(clipped.endswith("…") if not tail
                                    else clipped.startswith("…"))

    def test_three_screen_sizes_have_bounded_behavior(self):
        for size in ((24, 80), (20, 60)):
            with self.subTest(size=size):
                screen = Screen([27], size=size)
                self.assertFalse(tui._small_screen(screen))
                tui._frame(screen, "混剪 · 多选爆款视频", "字幕和长路径 /Users/测试素材")
                tui._footer(screen, "↑↓ · Enter · Esc 退出", "Enter选择 Esc退出")
                self.assertFalse(tui._confirm(Screen([27], size=size),
                                               [("视频", "视频.mp4")]))
        tiny = Screen([27], size=(12, 40))
        self.assertTrue(tui._small_screen(tiny))
        self.assertIsNone(tui._pick_dir(tiny, "路径", start=Path("/")))
        with mock.patch.object(tui.curses, "curs_set"):
            self.assertEqual(tui._loop(Screen([27], size=(12, 40)))[0], "quit")

    def test_confirmation_scrolls_all_selected_sources(self):
        lines = [("来源 " + str(i), "中文片段.mp4 × " + str(i)) for i in range(1, 26)]
        screen = Screen([curses.KEY_NPAGE, curses.KEY_NPAGE,
                         curses.KEY_NPAGE, 10], size=(20, 60))
        self.assertTrue(tui._confirm(screen, lines))
        self.assertTrue(any("来源 25" in text for _, _, text in screen.printed))
        self.assertTrue(any("PgUp/PgDn" in text for _, _, text in screen.printed))
        self.assertTrue(all(y < 18 for y, _, text in screen.printed
                            if "确认并开始处理" in text))

    def test_confirm_keeps_action_and_toggles_in_small_supported_screen(self):
        lines = [("来源", "非常长的中文文件名" * 10) for _ in range(20)]
        options = [("字幕", "擦除", True), ("去重", "检测", False)]
        outputs = []
        screen = Screen([curses.KEY_NPAGE, curses.KEY_DOWN,
                         curses.KEY_DOWN, 10], size=(20, 60))
        self.assertTrue(tui._confirm(screen, lines, options, outputs))
        self.assertEqual(outputs, [True, False])
        self.assertTrue(any("确认并开始处理" in text
                            for _, _, text in screen.printed))

    def test_grapheme_backspace_handles_accents_and_emoji(self):
        self.assertEqual(tui._erase_grapheme("caf\u00e9"), "caf")
        self.assertEqual(tui._erase_grapheme("e\u0301"), "")
        self.assertEqual(tui._erase_grapheme("婚礼a"), "婚礼")
        self.assertEqual(tui._erase_grapheme("👩\u200d💻"), "")
        self.assertEqual(tui._erase_grapheme("👍🏽"), "")
        screen = Screen(["婚", "礼", "e", "\u0301", "\b", "a", "\n"], size=(20, 60))
        self.assertEqual(tui._prompt_text(screen, 4, 2, 60, "输入关键词"), "婚礼a")

    def test_no_color_mode_and_light_palette(self):
        with mock.patch.object(tui.curses, "has_colors", return_value=False):
            tui._init_colors()
        self.assertFalse(tui._HAS_COLORS)
        self.assertEqual(tui._LIGHT_256["text"], 232)
        self.assertEqual(tui._LIGHT_8["text"], curses.COLOR_BLACK)
        with mock.patch.dict(tui.os.environ, {"COLORFGBG": "0;15"}), \
             mock.patch.object(tui.curses, "has_colors", return_value=True), \
             mock.patch.object(tui.curses, "use_default_colors"), \
             mock.patch.object(tui.curses, "COLORS", 256, create=True), \
             mock.patch.object(tui.curses, "init_pair") as pair:
            tui._init_colors()
        self.assertTrue(tui._LIGHT_BG)
        pair.assert_any_call(1, tui._LIGHT_256["accent"], -1)

    def test_long_video_label_and_search_row_do_not_overlap_footer(self):
        long_file = Path("/tmp/" + "超长素材" * 30 + ".mp4")
        screen = Screen([ord(" "), 10], size=(20, 60))
        chosen, counts = tui._multi_select(screen, "混剪", [long_file])
        self.assertEqual(chosen, [long_file])
        self.assertEqual(counts[str(long_file)], 1)
        self.assertFalse(any(y >= 18 and text.startswith("❯")
                             for y, _, text in screen.printed))


if __name__ == "__main__":
    unittest.main()
