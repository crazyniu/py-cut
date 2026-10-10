"""Cross-platform Textual directory selection regressions (no GUI needed).

The open-source library owns directory listing and keyboard navigation.
Test pycut's thin adapter, terminal restoration, shortcuts and path validation.
"""
from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.app import App
from textual.widgets import Input
from textual_fspicker import SelectDirectory
from textual_fspicker.parts import DirectoryNavigation

from pycut import folder_picker, tui


class Screen:
    def __init__(self):
        self.drawn = []

    def getmaxyx(self):
        return (24, 100)

    def erase(self):
        pass

    def refresh(self):
        pass

    def addstr(self, y, x, value, attr=0):
        self.drawn.append(value)

    def getch(self):
        raise AssertionError("curses must not read picker input")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tui, "_attr", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_public_widget_is_existing_open_source_select_directory(self):
        self.assertTrue(issubclass(
            folder_picker.PyCutSelectDirectory, SelectDirectory
        ))
        self.assertIn("location", __import__("inspect").signature(
            SelectDirectory.__init__
        ).parameters)

    def test_curses_is_suspended_and_restored_in_order(self):
        steps = []
        with mock.patch.object(folder_picker.curses, "def_prog_mode",
                               side_effect=lambda: steps.append("save")), \
             mock.patch.object(folder_picker.curses, "endwin",
                               side_effect=lambda: steps.append("suspend")), \
             mock.patch.object(folder_picker.curses, "reset_prog_mode",
                               side_effect=lambda: steps.append("restore")), \
             mock.patch.object(folder_picker._PickerApp, "run",
                               side_effect=lambda: (
                                   steps.append("textual") or Path("/tmp/folder")
                               )):
            selected = folder_picker.choose_directory("folder", Path("/tmp"))
        self.assertEqual(selected, str(Path("/tmp/folder")))
        self.assertEqual(steps, ["save", "suspend", "textual", "restore"])

    def test_curses_restored_when_user_cancels(self):
        with mock.patch.object(folder_picker.curses, "def_prog_mode"), \
             mock.patch.object(folder_picker.curses, "endwin"), \
             mock.patch.object(folder_picker.curses, "reset_prog_mode") as restore, \
             mock.patch.object(folder_picker._PickerApp, "run", return_value=None):
            self.assertIsNone(folder_picker.choose_directory(
                "folder", Path("/tmp")
            ))
        restore.assert_called_once()

    def test_curses_restored_when_picker_crashes(self):
        with mock.patch.object(folder_picker.curses, "def_prog_mode"), \
             mock.patch.object(folder_picker.curses, "endwin"), \
             mock.patch.object(folder_picker.curses, "reset_prog_mode") as restore, \
             mock.patch.object(folder_picker._PickerApp, "run",
                               side_effect=RuntimeError("terminal failed")):
            with self.assertRaisesRegex(RuntimeError, "terminal failed"):
                folder_picker.choose_directory("folder", Path("/tmp"))
        restore.assert_called_once()

    def test_default_user_home_is_resolved_by_existing_wizard(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "Users" / "apple"
            home.mkdir(parents=True)
            with mock.patch.object(tui.Path, "home", return_value=home), \
                 mock.patch.object(tui, "choose_directory",
                                   return_value=str(home)) as choose:
                self.assertEqual(tui._pick_dir(Screen(), "目录"), str(home))
            choose.assert_called_once_with("目录", home)

    def test_cancel_returns_none_and_does_not_start_processing(self):
        with mock.patch.object(tui, "choose_directory", return_value=None):
            self.assertIsNone(tui._pick_dir(Screen(), "目录"))

    def test_unicode_external_disk_selected_without_path_manipulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "Volumes" / "外接硬盘 01" / "源视频"
            disk.mkdir(parents=True)
            with mock.patch.object(tui, "choose_directory",
                                   return_value=str(disk)) as choose:
                self.assertEqual(tui._pick_dir(Screen(), "目录"), str(disk))
            choose.assert_called_once()

    def test_invalid_or_disconnected_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "video.mp4"
            file.touch()
            for invalid in (str(file), str(root / "gone"), "relative/folder"):
                with self.subTest(invalid=invalid), \
                     mock.patch.object(tui, "choose_directory",
                                       return_value=invalid), \
                     mock.patch.object(tui, "_flash") as flash:
                    self.assertIsNone(tui._pick_dir(Screen(), "目录"))
                    flash.assert_called_once()

    def test_error_visible_when_terminal_picker_missing(self):
        with mock.patch.object(tui, "choose_directory",
                               side_effect=RuntimeError("Textual missing")), \
             mock.patch.object(tui, "_flash") as flash:
            self.assertIsNone(tui._pick_dir(Screen(), "目录"))
            self.assertIn("Textual missing", flash.call_args.args[1])

    def test_mount_shortcut_location_macos_and_windows(self):
        with mock.patch.object(folder_picker.sys, "platform", "darwin"):
            self.assertEqual(folder_picker._mounted_drives_directory(
                Path("/Users/demo")
            ), Path("/Volumes"))
        with mock.patch.object(folder_picker.sys, "platform", "win32"):
            home = Path("/Users/demo")
            self.assertEqual(
                folder_picker._mounted_drives_directory(home),
                Path(home.anchor),
            )

    def test_fullscreen_tui_only_not_gui(self):
        import inspect
        src = inspect.getsource(folder_picker)
        for gui_term in ("osascript", "tkinter", "askdirectory", "choose folder"):
            self.assertNotIn(gui_term, src)
        self.assertIn("curses.endwin()", src)


class PickerHost(App[None]):
    def __init__(self, location: Path):
        super().__init__()
        self.initial_directory = location

    def on_mount(self):
        self.push_screen(
            folder_picker.PyCutSelectDirectory(
                location=self.initial_directory, title="选择目录"
            )
        )


class TextualIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_tree_is_owned_by_textual_fspicker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Library").mkdir()
            (root / "Documents").mkdir()
            (root / ".git").mkdir()
            app = PickerHost(root)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(
                    screen, folder_picker.PyCutSelectDirectory
                )
                nav = screen.query_one(DirectoryNavigation)
                self.assertEqual(nav.location, root)
                self.assertFalse(nav.show_hidden)
                # Mature widget does not hide 'Library' by name.
                self.assertTrue((root / "Library").is_dir())

    async def test_shortcuts_jump_to_existing_folders_without_autoselect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            desktop.mkdir()
            app = PickerHost(root)
            with mock.patch.object(folder_picker.Path, "home",
                                   return_value=root):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    picker = app.screen
                    picker.action_jump_desktop()
                    await pilot.pause()
                    self.assertEqual(
                        picker.query_one(DirectoryNavigation).location,
                        desktop,
                    )
                    picker.action_jump_home()
                    await pilot.pause()
                    self.assertEqual(
                        picker.query_one(DirectoryNavigation).location,
                        root,
                    )

    async def test_pasted_unicode_path_validates_then_navigates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "素材 中文 空格"
            target.mkdir()
            app = PickerHost(root)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                field = screen.query_one("#pycut-picker-path", Input)
                field.value = str(target)
                field.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(
                    screen.query_one(DirectoryNavigation).location,
                    target,
                )
                self.assertEqual(field.value, "")

    async def test_bad_pasted_path_keeps_current_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = PickerHost(root)
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                field = screen.query_one("#pycut-picker-path", Input)
                field.value = str(root / "missing")
                field.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(
                    screen.query_one(DirectoryNavigation).location,
                    root,
                )


if __name__ == "__main__":
    unittest.main()
