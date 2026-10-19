"""Compatibility directory navigation with temporary mounts/links and fake curses."""
from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import tui
from pycut.core import scan


class _Screen:
    """Minimal curses screen protocol for directory-picker tests."""

    def __init__(self, keys):
        self._keys = iter(keys)
        self.printed = []

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        pass

    def addstr(self, _y, _x, _s, _attrs=0):
        self.printed.append(_s)

    def refresh(self):
        pass

    def getch(self):
        try:
            return next(self._keys)
        except StopIteration as exc:
            raise AssertionError("目录选择器未按预期返回") from exc


class ScanDirsTests(unittest.TestCase):
    def test_browser_hides_only_dot_prefix_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("Volumes", "System", "Library", "private",
                         "node_modules", ".Spotlight-V100", ".hidden"):
                (root / name).mkdir()
            disk = root / "Volumes" / "视频盘"
            disk.mkdir()

            visible = scan.list_dirs(root)
            names = [p.name for p in visible]
            self.assertEqual(names, sorted(names, key=lambda name: (name.casefold(), name)))
            self.assertEqual(
                set(names),
                {"Volumes", "System", "Library", "private", "node_modules"},
            )
            self.assertEqual(scan.list_dirs(root / "Volumes"), [disk])
            # The processing pipeline can still filter system paths;
            # its predicate must not affect the TUI browser.
            self.assertFalse(scan._is_system_dir(root / "Volumes"))
            self.assertTrue(scan._is_system_dir(root / "System"))

    def test_symlink_to_directory_is_listed_but_broken_link_and_file_are_not(self):
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
            (volume / "notes.txt").write_text("not a folder", encoding="utf-8")
            (volume / "broken").symlink_to(volume / "missing", target_is_directory=True)
            (volume / ".secret-link").symlink_to(root, target_is_directory=True)
            self.assertEqual(scan.list_dirs(volume), [link, volume / "素材"])

    def test_unmounted_or_unreadable_directory_is_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(scan.list_dirs(root / "not-mounted"), [])
            with mock.patch.object(scan.os, "scandir",
                                   side_effect=PermissionError("denied")):
                self.assertEqual(scan.list_dirs(root), [])
                with self.assertRaises(PermissionError):
                    scan.list_dirs(root, raise_on_error=True)

    def test_one_unreadable_entry_does_not_hide_other_directories(self):
        class Entry:
            def __init__(self, path, *, broken=False):
                self.path = str(path)
                self.name = path.name
                self.broken = broken

            def is_dir(self, follow_symlinks=True):
                if not follow_symlinks:
                    raise AssertionError("浏览器需要显示目录符号链接")
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

    def test_native_picker_can_choose_a_system_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Library"
            folder.mkdir()
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(folder)) as choose, \
                 mock.patch.object(tui, "_attr", return_value=0):
                self.assertEqual(
                    tui._pick_dir(_Screen([]), "选择目录", start=root),
                    str(folder),
                )
            choose.assert_called_once_with("选择目录", root)

    def test_native_picker_accepts_a_mounted_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            disk = root / "Volumes" / "视频盘"
            disk.mkdir(parents=True)
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(disk)) as choose, \
                 mock.patch.object(tui, "_attr", return_value=0):
                self.assertEqual(
                    tui._pick_dir(_Screen([]), "选择外接盘", start=root),
                    str(disk),
                )
            choose.assert_called_once_with("选择外接盘", root)

    def test_canceling_native_dialog_does_not_select_a_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(tui, "choose_directory",
                                   return_value=None), \
                 mock.patch.object(tui, "_attr", return_value=0):
                self.assertIsNone(tui._pick_dir(
                    _Screen([]), "取消目录选择", start=root
                ))


if __name__ == "__main__":
    unittest.main()
