"""TUI Unicode 输入框的文件系统与 fake curses 回归测试。

不访问真实素材，不依赖 FFmpeg、LLM 或交互终端。
"""

from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import tui
from pycut.core import scan


class Screen:
    def __init__(self, keys, size=(24, 100), sizes=()):
        self.keys = list(keys)
        self.size = size
        self.sizes = list(sizes)
        self.printed = []

    def getmaxyx(self):
        return self.size

    def move(self, y, x):
        assert 0 <= y < self.size[0]
        assert 0 <= x < self.size[1]

    def clrtoeol(self):
        pass

    def erase(self):
        pass

    def refresh(self):
        pass

    def addstr(self, y, x, value, attr=0):
        assert 0 <= y < self.size[0]
        assert 0 <= x < self.size[1]
        assert x + tui._disp_width(value) < self.size[1]
        self.printed.append(value)

    def get_wch(self):
        if not self.keys:
            raise AssertionError("input exhausted: editor did not finish")
        key = self.keys.pop(0)
        if key == curses.KEY_RESIZE and self.sizes:
            self.size = self.sizes.pop(0)
        return key

    def getch(self):
        key = self.get_wch()
        return ord(key) if isinstance(key, str) else key


class ScreenWithoutUnicode(Screen):
    get_wch = None

    def getch(self):
        if not self.keys:
            raise AssertionError("input exhausted")
        return self.keys.pop(0)


class UnicodeEditorTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_chinese_english_and_japanese_mix_and_backspace(self):
        keys = ["婚", "礼", "片", "段", "0", "1", "日", "本", "語", 127,
                "a", "B", "\n"]
        actual = tui._prompt_text(Screen(keys), 4, 2, 100, "输入关键词")
        self.assertEqual(actual, "婚礼片段01日本aB")

    def test_unicode_paste_keeps_full_keywords(self):
        phrase = "婚礼🎉片段01-Japan日本"
        result = tui._prompt_text(Screen([*phrase, 10]), 4, 2, 100, "查素材")
        self.assertEqual(result, phrase)

    def test_backspace_deletes_decomposed_combining_character(self):
        # 分解式 e + 组合重音应只占一个可见字素。
        result = tui._prompt_text(Screen(["e", "\u0301", 127, "婚", 10]),
                                  4, 2, 100, "关键词")
        self.assertEqual(result, "婚")

    def test_escape_from_keyword_box_does_not_search(self):
        self.assertIsNone(tui._prompt_text(Screen([27]), 4, 2, 100, "关键词"))
        self.assertIsNone(tui._prompt_text(Screen(["\x1b"]), 4, 2, 100,
                                            "关键词", default="已输入"))

    def test_very_long_keyword_is_clipped_only_for_display(self):
        phrase = "混合中文🎉" * 200
        screen = Screen([10], size=(24, 60))
        returned = tui._prompt_text(screen, 4, 2, 100, "搜索关键词", phrase)
        self.assertEqual(returned, phrase)
        self.assertTrue(any("关键词" in s for s in screen.printed))

    def test_resize_while_editing_keeps_input_and_cannot_move_outside_screen(self):
        # 初始输入框在第 20 行，收缩到高度 15 必须动态移动。
        screen = Screen([curses.KEY_RESIZE, "婚", ord("1"), 10],
                        size=(24, 100), sizes=[(15, 60)])
        result = tui._prompt_text(screen, 20, 2, 100, "中文搜索")
        self.assertEqual(result, "婚1")
        self.assertEqual(screen.size, (15, 60))

    def test_tiny_screen_can_cancel_or_resize_back(self):
        tiny = Screen([curses.KEY_RESIZE, 27], size=(24, 100),
                      sizes=[(12, 40)])
        self.assertIsNone(tui._prompt_text(tiny, 20, 2, 100, "关键词"))
        back = Screen([curses.KEY_RESIZE, curses.KEY_RESIZE, "日", 10],
                      size=(24, 100), sizes=[(12, 40), (24, 100)])
        self.assertEqual(tui._prompt_text(back, 20, 2, 100, "关键词"), "日")

    def test_getch_only_terminals_keep_ascii_fallback(self):
        screen = ScreenWithoutUnicode([ord("a"), ord("B"), 127, ord("c"), 10])
        self.assertEqual(tui._prompt_text(screen, 4, 2, 100, "关键词"), "ac")
        self.assertTrue(any("仅 ASCII" in x for x in screen.printed))

    def test_real_filename_search_matches_unicode_and_ignores_nonmatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "婚礼片段01.mp4"
            target.write_bytes(b"placeholder")
            (root / "other.mp4").write_bytes(b"placeholder")
            (root / "ignored.txt").write_text("婚礼片段")
            self.assertEqual(scan.search_videos(root, "婚礼片段01"), [target])
            self.assertEqual(scan.search_videos(root, "不存在"), [])
            self.assertEqual(scan.search_videos(root, "   "), [])


if __name__ == "__main__":
    unittest.main()
