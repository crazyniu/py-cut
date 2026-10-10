"""Native folder-picker integration tests; no real GUI, media, or mounted disk.

The chooser itself comes from macOS Standard Additions or the open-source
Tk file dialog. We verify delegation and validation rather than reinventing
its directory-tree navigation in curses.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from pycut import folder_picker, tui


class FakeScreen:
    def __init__(self, size=(24, 100)):
        self.size = size
        self.drawn: list[str] = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        pass

    def refresh(self):
        pass

    def addstr(self, y, x, content, attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w
        assert x + tui._disp_width(content) < w
        self.drawn.append(content)

    def getch(self):
        raise AssertionError("native picker must not read keyboard navigation")


class NativeFolderPickerTests(unittest.TestCase):
    def setUp(self):
        color = mock.patch.object(tui, "_attr", return_value=0)
        color.start()
        self.addCleanup(color.stop)

    def test_macos_delegates_to_system_dialog(self):
        with mock.patch.object(folder_picker.sys, "platform", "darwin"), \
             mock.patch.object(folder_picker, "_macos_choose_folder",
                               return_value="/Volumes/My Disk") as apple, \
             mock.patch.object(folder_picker, "_tk_choose_folder") as tk:
            self.assertEqual(
                folder_picker.choose_directory("选择视频目录", Path("/Users/demo")),
                "/Volumes/My Disk",
            )
        apple.assert_called_once_with("选择视频目录", Path("/Users/demo"))
        tk.assert_not_called()

    def test_other_desktop_uses_opensource_tk_dialog(self):
        with mock.patch.object(folder_picker.sys, "platform", "linux"), \
             mock.patch.object(folder_picker, "_tk_choose_folder",
                               return_value="/tmp") as tk, \
             mock.patch.object(folder_picker, "_macos_choose_folder") as apple:
            self.assertEqual(
                folder_picker.choose_directory("选择目录", Path("/home/demo")),
                "/tmp",
            )
        tk.assert_called_once()
        apple.assert_not_called()

    def test_macos_passes_title_and_path_as_argv_never_shell(self):
        title = '目录 "特殊" · 中文'
        directory = Path("/Users/apple")
        returned = "/Volumes/视频 磁盘/剪辑/\n"
        with mock.patch.object(folder_picker.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, returned, "")) as run:
            actual = folder_picker._macos_choose_folder(title, directory)
        self.assertEqual(actual, "/Volumes/视频 磁盘/剪辑/")
        args, options = run.call_args
        self.assertEqual(args[0][0:2], ["osascript", "-e"])
        self.assertEqual(args[0][-2:], [title, str(directory)])
        self.assertIn("choose folder", args[0][2])
        self.assertIn("invisibles false", args[0][2])
        self.assertNotIn(title, args[0][2])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertFalse(options["check"])

    def test_macos_dialog_cancel_is_none(self):
        with mock.patch.object(folder_picker.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, "\n", "")):
            self.assertIsNone(folder_picker._macos_choose_folder(
                "目录", Path("/Users/demo")
            ))

    def test_macos_error_minus_128_is_cancel(self):
        with mock.patch.object(folder_picker.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "User canceled. (-128)")):
            self.assertIsNone(folder_picker._macos_choose_folder(
                "目录", Path("/Users/demo")
            ))

    def test_macos_other_dialog_error_propagates(self):
        with mock.patch.object(folder_picker.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "automation denied")):
            with self.assertRaisesRegex(OSError, "automation denied"):
                folder_picker._macos_choose_folder("目录", Path("/Users/demo"))

    def test_tk_native_dialog_requires_existing_folder(self):
        window = mock.Mock()
        picker = mock.Mock(return_value="/tmp/素材")
        fake_tk = types.ModuleType("tkinter")
        fake_tk.Tk = mock.Mock(return_value=window)
        fake_tk.TclError = type("TclError", (Exception,), {})
        fake_dialog = types.ModuleType("tkinter.filedialog")
        fake_dialog.askdirectory = picker
        fake_tk.filedialog = fake_dialog
        with mock.patch.dict("sys.modules", {
            "tkinter": fake_tk, "tkinter.filedialog": fake_dialog
        }):
            result = folder_picker._tk_choose_folder("目录", Path("/tmp"))
        self.assertEqual(result, "/tmp/素材")
        picker.assert_called_once_with(
            parent=window, initialdir="/tmp", title="目录", mustexist=True
        )
        window.withdraw.assert_called_once()
        window.destroy.assert_called_once()

    def test_tk_cancel_returns_none_and_destroys_window(self):
        window = mock.Mock()
        fake_tk = types.ModuleType("tkinter")
        fake_tk.Tk = mock.Mock(return_value=window)
        fake_tk.TclError = type("TclError", (Exception,), {})
        fake_dialog = types.ModuleType("tkinter.filedialog")
        fake_dialog.askdirectory = mock.Mock(return_value="")
        fake_tk.filedialog = fake_dialog
        with mock.patch.dict("sys.modules", {
            "tkinter": fake_tk, "tkinter.filedialog": fake_dialog
        }):
            self.assertIsNone(
                folder_picker._tk_choose_folder("目录", Path("/tmp"))
            )
        window.destroy.assert_called_once()

    def test_picker_defaults_to_actual_current_user_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "Users" / "apple"
            home.mkdir(parents=True)
            (home / "Desktop").mkdir()
            with mock.patch.object(tui.Path, "home", return_value=home), \
                 mock.patch.object(tui, "choose_directory",
                                   return_value=str(home)) as chooser:
                self.assertEqual(
                    tui._pick_dir(FakeScreen(), "选择源目录"), str(home)
                )
            chooser.assert_called_once_with("选择源目录", home)

    def test_picker_remembers_previous_selection_as_initial_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            child = Path(temp) / "视频 空格"
            child.mkdir()
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(child)) as chooser:
                self.assertEqual(
                    tui._pick_dir(FakeScreen(), "下一步", start=child), str(child)
                )
            chooser.assert_called_once_with("下一步", child)

    def test_nonexistent_start_falls_back_to_current_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            missing = home / "disconnected-drive"
            with mock.patch.object(tui.Path, "home", return_value=home), \
                 mock.patch.object(tui, "choose_directory",
                                   return_value=None) as chooser:
                self.assertIsNone(
                    tui._pick_dir(FakeScreen(), "目录", start=missing)
                )
            chooser.assert_called_once_with("目录", home)

    def test_native_picker_can_choose_unicode_disk_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            disk = Path(temp) / "Volumes" / "外接硬盘 01" / "源视频"
            disk.mkdir(parents=True)
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(disk)):
                self.assertEqual(tui._pick_dir(FakeScreen(), "选目录"), str(disk))

    def test_cancel_returns_none_without_launching_work(self):
        with mock.patch.object(tui, "choose_directory",
                               return_value=None):
            self.assertIsNone(tui._pick_dir(FakeScreen(), "目录"))

    def test_invalid_returned_file_or_disconnected_disk_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video.mp4"
            video.touch()
            for bad in (str(video), str(root / "gone"), "relative/path"):
                with self.subTest(path=bad), \
                     mock.patch.object(tui, "choose_directory",
                                       return_value=bad), \
                     mock.patch.object(tui, "_flash") as flash:
                    self.assertIsNone(tui._pick_dir(FakeScreen(), "目录"))
                    flash.assert_called_once()

    def test_os_picker_error_is_visible_and_goes_back_safely(self):
        with mock.patch.object(tui, "choose_directory",
                               side_effect=OSError("GUI unavailable")), \
             mock.patch.object(tui, "_flash") as flash:
            self.assertIsNone(tui._pick_dir(FakeScreen(), "目录"))
            self.assertIn("GUI unavailable", flash.call_args.args[1])

    def test_no_custom_directory_tree_or_shortcut_processing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(root)) as chooser, \
                 mock.patch("pycut.core.scan.list_dirs",
                            side_effect=AssertionError("should not scan")):
                self.assertEqual(tui._pick_dir(FakeScreen(), "目录"), str(root))
            chooser.assert_called_once()

    def test_native_panel_preserves_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "disk"
            target.mkdir()
            link = root / "shortcut"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("OS does not permit directory symlinks")
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(link)):
                self.assertEqual(tui._pick_dir(FakeScreen(), "目录"), str(link))


if __name__ == "__main__":
    unittest.main()
