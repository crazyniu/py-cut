"""混剪裂变数量可见输入、取消恢复和窗口尺寸变化测试。"""

from __future__ import annotations

import curses
from pathlib import Path
import unittest
from unittest import mock

from pycut import cli, tui


class Screen:
    def __init__(self, keys, size=(24, 100), on_resize=None):
        self.keys = list(keys)
        self.size = size
        self.on_resize = on_resize
        self.printed = []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, value, attr=0):
        assert 0 <= y < self.size[0], (y, self.size)
        assert 0 <= x < self.size[1], (x, self.size)
        assert x + tui._disp_width(value) < self.size[1], (x, value, self.size)
        self.printed.append((y, x, value))

    def move(self, y, x):
        assert 0 <= y < self.size[0], (y, self.size)

    def clrtoeol(self):
        pass

    def refresh(self):
        pass

    def erase(self):
        pass

    def getch(self):
        key = self.keys.pop(0) if self.keys else 27
        if key == curses.KEY_RESIZE and self.on_resize is not None:
            self.size = self.on_resize
        return key


class FissionTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_two_sources_show_sum_and_keep_individual_quantities(self):
        first = Path("/tmp/甲.mp4")
        second = Path("/tmp/乙.mp4")
        screen = Screen([
            ord(" "), ord("e"), ord("1"), ord("2"), 10,
            curses.KEY_DOWN, ord(" "), ord("e"), ord("2"), 10, 10,
        ])
        chosen, fission = tui._multi_select(screen, "混剪", [first, second])
        self.assertEqual(chosen, [first, second])
        self.assertEqual(fission, {str(first): 12, str(second): 2})
        self.assertTrue(
            any("预计输出 14 支" in value for _, _, value in screen.printed)
        )
        for file in chosen:
            args = cli.build_parser().parse_args(
                ["montage", "--source", str(file), "--fission", str(fission[str(file)])]
            )
            self.assertEqual(args.fission, fission[str(file)])

    def test_escape_during_integer_edit_keeps_selection_and_default(self):
        first = Path("/tmp/甲.mp4")
        screen = Screen([ord(" "), ord("e"), 27, 10])
        chosen, values = tui._multi_select(screen, "混剪", [first])
        self.assertEqual(chosen, [first])
        self.assertEqual(values[str(first)], 1)

    def test_out_of_range_is_visible_and_user_must_correct(self):
        screen = Screen([ord("0"), 10, 127, ord("9"), ord("9"), 10])
        result = tui._prompt_int(screen, 8, 2, 100, "裂变", 1, 1, 99)
        self.assertEqual(result, 99)
        self.assertTrue(
            any("请输入 1～99" in value for _, _, value in screen.printed)
        )

    def test_editor_survives_window_resize_and_esc(self):
        # 从 100×24 收缩至 60×15。原先固定 row=20，此时已越界。
        screen = Screen([curses.KEY_RESIZE, ord("1"), ord("2"), 10],
                        on_resize=(15, 60))
        self.assertEqual(tui._prompt_int(screen, 20, 2, 100, "裂变", 1, 1, 99),
                         12)
        tiny = Screen([curses.KEY_RESIZE, 27], on_resize=(11, 40))
        self.assertIsNone(
            tui._prompt_int(tiny, 20, 2, 100, "裂变", 1, 1, 99)
        )

    def test_integer_editor_recovers_from_cursor_move_during_resize(self):
        screen = Screen([ord("2"), 10])
        original_move = screen.move
        attempts = [0]

        def flaky_move(y, x):
            attempts[0] += 1
            if attempts[0] == 1:
                raise curses.error("window resized")
            return original_move(y, x)

        screen.move = flaky_move
        self.assertEqual(
            tui._prompt_int(screen, 8, 2, 100, "裂变", 1, 1, 99), 2
        )
        self.assertGreaterEqual(attempts[0], 2)

    def test_integer_editor_can_exit_tiny_terminal_with_q(self):
        self.assertIsNone(tui._prompt_int(
            Screen([ord("q")], size=(10, 40)),
            8, 2, 100, "裂变", 1, 1, 99,
        ))

    def test_empty_file_list_skips_indexing(self):
        self.assertIsNone(tui._multi_select(Screen([]), "混剪", []))


if __name__ == "__main__":
    unittest.main()
