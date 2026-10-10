"""Unicode 搜索贯穿输入框→真实文件名扫描的回归，不使用视频文件内容。"""

from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import tui
from pycut.core.scan import search_videos


class Screen:
    def __init__(self, keys, size=(24, 80)):
        self.keys = list(keys)
        self.size = size
        self.printed = []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, _attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w
        assert x + tui._disp_width(value) < w
        self.printed.append(str(value))

    def move(self, y, x):
        assert 0 <= y < self.size[0]

    def clrtoeol(self):
        pass

    def refresh(self):
        pass

    def erase(self):
        pass

    def get_wch(self):
        return self.keys.pop(0) if self.keys else "\x1b"

    def getch(self):
        k = self.get_wch()
        return ord(k) if isinstance(k, str) and len(k) == 1 else k


class UnicodeSearchTests(unittest.TestCase):
    def setUp(self):
        attr = mock.patch.object(tui, "_attr", return_value=0)
        attr.start()
        self.addCleanup(attr.stop)

    def test_chinese_keyword_from_real_input_matches_actual_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "分类").mkdir()
            match = root / "分类/婚礼片段01.MP4"
            match.write_bytes(b"dummy")
            (root / "分类/生日片段01.mp4").write_bytes(b"dummy")
            keyword = tui._prompt_text(
                Screen([*list("婚礼片段01"), "\n"]), 4, 2, 80, "关键词"
            )
            self.assertEqual(keyword, "婚礼片段01")
            self.assertEqual(search_videos(root, keyword), [match])
            self.assertEqual(search_videos(root, "不存在"), [])

    def test_japanese_ascii_emoji_and_unicode_backspace(self):
        screen = Screen(["日", "本", "語", curses.KEY_BACKSPACE,
                         "🍣", "a", "1", "\n"])
        self.assertEqual(
            tui._prompt_text(screen, 4, 2, 80, "关键词"), "日本🍣a1"
        )

    def test_unicode_paste_sequence_long_text_clips_display(self):
        # 真实终端的 bracketed paste 通常逐字符产生 get_wch 事件。
        phrase = "婚礼片段abc123" * 22
        screen = Screen([*phrase, "\n"], size=(20, 60))
        self.assertEqual(
            tui._prompt_text(screen, 4, 2, 60, "关键词"), phrase
        )
        self.assertTrue(screen.printed)

    def test_escape_does_not_start_search_and_ascii_fallback(self):
        self.assertIsNone(tui._prompt_text(Screen(["\x1b"]), 4, 2, 80, "关键词"))

        class AsciiOnly(Screen):
            get_wch = None

            def getch(self):
                k = self.keys.pop(0) if self.keys else 27
                return k

        screen = AsciiOnly([ord("a"), ord("b"), 10])
        # 如果不可用 get_wch，应退化到普通 getch 的 ASCII 输入。
        with mock.patch.object(screen, "get_wch", None, create=False):
            self.assertEqual(
                tui._prompt_text(screen, 4, 2, 80, "关键词"), "ab"
            )

    def test_search_tui_cancel_before_keyword_does_not_scan(self):
        with mock.patch.object(tui, "_pick_dir", return_value="/tmp/videos"), \
             mock.patch.object(tui, "_prompt_text", return_value=None), \
             mock.patch.object(tui, "search_videos") as search:
            self.assertIsNone(tui._wizard_search(mock.Mock(), 80))
            search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
