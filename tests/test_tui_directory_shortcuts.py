"""Shortcuts, mounted-volume browsing, pasted paths and validation for TUI.

All tests use temporary folders and fake curses, never mount or modify user disks.
"""
from __future__ import annotations

import curses
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import tui


class Screen:
    def __init__(self, keys, size=(24, 100)):
        self.keys = list(keys)
        self.size = size
        self.printed = []

    def getmaxyx(self):
        return self.size

    def _next(self):
        if not self.keys:
            raise AssertionError("unexpected key read / stuck directory browser")
        return self.keys.pop(0)

    def getch(self):
        key = self._next()
        return ord(key) if isinstance(key, str) and len(key) == 1 else key

    def get_wch(self):
        return self._next()

    def erase(self):
        pass

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def move(self, y, x):
        assert 0 <= y < self.size[0]
        assert 0 <= x < self.size[1]

    def clrtoeol(self):
        pass

    def addstr(self, y, x, content, attr=0):
        assert 0 <= y < self.size[0]
        assert 0 <= x < self.size[1]
        assert x + tui._disp_width(content) < self.size[1]
        self.printed.append(content)


class FolderShortcutTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_home_is_dynamic_and_user_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "用户甲"
            home.mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home):
                screen = Screen([ord(" ")])
                self.assertEqual(tui._pick_dir(screen, "目录"), str(home))
            self.assertTrue(any(str(home) in text for text in screen.printed))

    def test_shortcuts_desktop_documents_downloads_jump_then_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "test-user"
            home.mkdir()
            for name in ("Desktop", "Documents", "Downloads"):
                (home / name).mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home):
                for number, name in (("2", "Desktop"), ("3", "Documents"),
                                     ("4", "Downloads")):
                    with self.subTest(number=number):
                        screen = Screen([ord(number), 10])
                        self.assertEqual(
                            tui._pick_dir(screen, "选目录"),
                            str(home / name),
                        )

    def test_shortcut_changes_location_without_auto_selecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Desktop").mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home):
                screen = Screen([ord("2"), 27])
                self.assertIsNone(tui._pick_dir(screen, "目录"))
            self.assertTrue(any(str(home / "Desktop") in s for s in screen.printed))

    def test_home_shortcut_returns_from_other_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home):
                self.assertEqual(
                    tui._pick_dir(Screen([ord("1"), 10]), "目录", start=desktop),
                    str(home),
                )

    def test_mount_shortcut_lists_drives_and_allows_choose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            home.mkdir()
            volumes = root / "Volumes"
            drive = volumes / "My USB Disk"
            drive.mkdir(parents=True)
            (volumes / ".Spotlight-V100").mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home), \
                 mock.patch.object(tui, "_mount_directory", return_value=volumes):
                screen = Screen([ord("5"), curses.KEY_UP, 10, 10])
                self.assertEqual(tui._pick_dir(screen, "选外接盘"), str(drive))
            output = "\n".join(screen.printed)
            self.assertIn("My USB Disk/", output)
            self.assertNotIn(".Spotlight-V100/", output)

    def test_mount_root_on_macos_is_volumes(self):
        with mock.patch.object(tui.sys, "platform", "darwin"):
            self.assertEqual(
                tui._mount_directory(Path("/Users/demo")), Path("/Volumes")
            )

    def test_missing_shortcut_directory_stays_at_current_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            screen = Screen([ord("2"), ord(" ")])
            with mock.patch.object(tui.Path, "home", return_value=home):
                self.assertEqual(tui._pick_dir(screen, "目录"), str(home))
            self.assertTrue(any("目录不存在" in s for s in screen.printed))

    def test_paste_unicode_absolute_path_through_unicode_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "混剪素材 空格 🎬"
            path.mkdir()
            screen = Screen([ord("p"), *str(path), 10, 10])
            self.assertEqual(tui._pick_dir(screen, "选目录", start=root), str(path))
            self.assertTrue(any("粘贴目录路径" in s for s in screen.printed))

    def test_paste_quoted_or_shell_escaped_folder_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "My USB Disk"
            folder.mkdir()
            for value in (f'"{folder}"', str(folder).replace(" ", r"\ ")):
                with self.subTest(value=value):
                    self.assertEqual(tui._resolve_directory_path(value, root), folder)

    def test_paste_relative_and_tilde_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Downloads" / "clips"
            target.mkdir(parents=True)
            self.assertEqual(
                tui._resolve_directory_path("Downloads/clips", root), target
            )
            with mock.patch.dict(os.environ, {"PYCUT_FOLDER_TEST": str(target)}):
                self.assertEqual(
                    tui._resolve_directory_path("$PYCUT_FOLDER_TEST", root),
                    target,
                )
            # Use the actual interpreter's home path expansion contract.
            with mock.patch.dict(os.environ, {"HOME": str(root)}):
                self.assertEqual(
                    tui._resolve_directory_path("~/Downloads/clips", root),
                    target,
                )

    def test_invalid_path_and_file_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.mp4").touch()
            for value, expected in (
                ("", "请输入"),
                ("missing", "不存在"),
                ("file.mp4", "文件夹"),
            ):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, expected):
                        tui._resolve_directory_path(value, root)

    def test_invalid_paste_retries_in_place_using_ctrl_u(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "Videos"
            valid.mkdir()
            wrong = str(root / "missing")
            screen = Screen([ord("P"), *wrong, 10, 21, *str(valid), 10, 10])
            self.assertEqual(tui._pick_dir(screen, "目录", start=root), str(valid))
            self.assertTrue(any("目录不存在" in s for s in screen.printed))

    def test_esc_cancels_paste_not_entire_picker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            screen = Screen([ord("P"), 27, ord(" ")])
            self.assertEqual(tui._pick_dir(screen, "目录", start=root), str(root))

    def test_short_terminal_shows_all_shortcut_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            screen = Screen([27], size=(14, 60))
            with mock.patch.object(tui.Path, "home", return_value=home):
                self.assertIsNone(tui._pick_dir(screen, "目录"))
            line = next(x for x in screen.printed if "1主目录" in x)
            for snippet in ("2桌面", "3文档", "4下载", "5磁盘", "P路径"):
                self.assertIn(snippet, line)


if __name__ == "__main__":
    unittest.main()
