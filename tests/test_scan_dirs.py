"""目录浏览回归：外接盘、系统目录、失效链接及键盘导航。

不访问真实 /Volumes，也不依赖 FFmpeg、终端或用户素材。
"""
from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import tui
from pycut.core import scan


class _Screen:
    """仅实现目录选择器使用的 curses 接口。"""

    def __init__(self, keys):
        self._keys = iter(keys)

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        pass

    def addstr(self, _y, _x, _s, _attrs=0):
        pass

    def refresh(self):
        pass

    def getch(self):
        try:
            return next(self._keys)
        except StopIteration as exc:
            raise AssertionError("目录选择器未按预期返回") from exc


class ScanDirsTests(unittest.TestCase):
    def test_volumes_is_browsable_but_system_and_hidden_dirs_are_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("Volumes", "System", "Library", "private",
                         "node_modules", ".Spotlight-V100", ".hidden"):
                (root / name).mkdir()
            disk = root / "Volumes" / "视频盘"
            disk.mkdir()

            visible = scan.list_dirs(root)
            self.assertEqual(visible, [root / "Volumes"])
            self.assertEqual(scan.list_dirs(root / "Volumes"), [disk])
            self.assertFalse(scan._is_system_dir(root / "Volumes"))
            self.assertTrue(scan._is_system_dir(root / "System"))

    def test_symlink_to_parent_is_not_a_browsable_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            volume = root / "Volumes"
            volume.mkdir()
            (volume / "素材").mkdir()
            link = volume / "loop"
            try:
                link.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"测试环境不支持符号链接: {exc}")
            self.assertEqual(scan.list_dirs(volume), [volume / "素材"])

    def test_unmounted_or_unreadable_directory_is_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(scan.list_dirs(root / "not-mounted"), [])
            with mock.patch.object(scan.os, "scandir",
                                   side_effect=PermissionError("denied")):
                self.assertEqual(scan.list_dirs(root), [])

    def test_one_unreadable_entry_does_not_hide_other_directories(self):
        class Entry:
            def __init__(self, path, *, broken=False):
                self.path = str(path)
                self.broken = broken

            def is_dir(self, follow_symlinks=True):
                if follow_symlinks:
                    raise AssertionError("浏览器不应跟随符号链接")
                if self.broken:
                    raise PermissionError("offline volume")
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("A", "B"):
                (root / name).mkdir()
            entries = [Entry(root / "B"), Entry(root / "offline", broken=True),
                       Entry(root / "A")]
            with mock.patch.object(scan.os, "scandir", return_value=entries):
                self.assertEqual(scan.list_dirs(root), [root / "A", root / "B"])

    def test_browser_enters_volumes_and_selects_the_mounted_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            disk = root / "Volumes" / "视频盘"
            disk.mkdir(parents=True)
            keys = [10, curses.KEY_DOWN, curses.KEY_DOWN, 10, 10]
            with mock.patch.object(tui, "_attr", return_value=0):
                chosen = tui._pick_dir(_Screen(keys), "选择外接盘", start=root)
            self.assertEqual(chosen, str(disk))

    def test_browser_can_go_back_home_root_or_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            leaf = root / "Videos"
            leaf.mkdir()
            with mock.patch.object(tui, "_attr", return_value=0):
                self.assertEqual(tui._pick_dir(_Screen([ord("h"), 10]),
                                               "返回", start=leaf), str(root))
                self.assertEqual(tui._pick_dir(_Screen([ord("~"), 10]),
                                               "主目录", start=leaf), str(Path.home()))
                self.assertEqual(tui._pick_dir(_Screen([ord("/"), 10]),
                                               "根目录", start=leaf), "/")
                self.assertIsNone(tui._pick_dir(_Screen([27]),
                                                "取消", start=leaf))


if __name__ == "__main__":
    unittest.main()
