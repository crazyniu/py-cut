"""Cross-platform, pure-terminal folder selection using textual-fspicker.

Filesystem browsing and keyboard navigation belong to the mature, MIT-licensed
Textual SelectDirectory / DirectoryNavigation widgets, not to pycut.
Only thin workflow integration, shortcuts, and path validation live here.
"""
from __future__ import annotations

import curses
import os
from pathlib import Path
import sys

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static
from textual_fspicker import SelectDirectory
from textual_fspicker.parts import DirectoryNavigation


def _mounted_drives_directory(home: Path) -> Path:
    """Pick a sensible mount browser location; Windows has native drive list."""
    if sys.platform == "darwin":
        return Path("/Volumes")
    if sys.platform == "win32":
        return Path(home.anchor)
    roots = (
        Path("/run/media") / home.name,
        Path("/media") / home.name,
        Path("/run/media"),
        Path("/media"),
        Path("/mnt"),
    )
    return next((root for root in roots if root.is_dir()), Path("/mnt"))


class PyCutSelectDirectory(SelectDirectory):
    """Add convenient locations and a paste field to the existing picker.

    The actual directory tree, focus, scrolling, hidden-dot filtering and
    Windows drive selection are implemented by textual-fspicker.
    """

    DEFAULT_CSS = """
    #pycut-picker-help {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    #pycut-picker-path {
        margin: 0 1 1 1;
    }
    """
    BINDINGS = [
        *SelectDirectory.BINDINGS,
        Binding("1", "jump_home", "主目录", show=False),
        Binding("2", "jump_desktop", "桌面", show=False),
        Binding("3", "jump_documents", "文档", show=False),
        Binding("4", "jump_downloads", "下载", show=False),
        Binding("5", "jump_mounts", "挂载磁盘", show=False),
        Binding("p", "focus_path", "输入路径", show=False),
        Binding("left", "go_up", "返回上级", show=False),
    ]

    def _header_area(self) -> ComposeResult:
        yield Static(
            "1 主目录  2 桌面  3 文档  4 下载  5 磁盘  P 输入路径"
            "   ↑↓ 选择  Enter 打开  Backspace/← 上级  Esc 返回",
            id="pycut-picker-help",
        )
        yield Input(
            placeholder="可粘贴绝对路径、~/目录或相对路径，Enter 跳转",
            id="pycut-picker-path",
        )

    def _navigation(self) -> DirectoryNavigation:
        return self.query_one(DirectoryNavigation)

    def _jump(self, destination: Path) -> None:
        try:
            if not destination.is_dir():
                self._set_error(f"目录不存在或无法访问：{destination}")
                return
        except OSError:
            self._set_error(f"目录不存在或无法访问：{destination}")
            return
        self._navigation().location = destination
        self._navigation().focus()
        self._set_error()

    def action_jump_home(self) -> None:
        self._jump(Path.home())

    def action_jump_desktop(self) -> None:
        self._jump(Path.home() / "Desktop")

    def action_jump_documents(self) -> None:
        self._jump(Path.home() / "Documents")

    def action_jump_downloads(self) -> None:
        self._jump(Path.home() / "Downloads")

    def action_jump_mounts(self) -> None:
        self._jump(_mounted_drives_directory(Path.home()))

    def action_focus_path(self) -> None:
        self.query_one("#pycut-picker-path", Input).focus()

    def action_go_up(self) -> None:
        self._navigation().action_navigate_up()
        self._navigation().focus()

    @on(Input.Submitted, "#pycut-picker-path")
    def _path_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            self._set_error("请输入目录路径")
            return
        expanded = Path(os.path.expandvars(raw)).expanduser()
        target = (expanded if expanded.is_absolute()
                  else self._navigation().location / expanded)
        try:
            if not target.is_dir():
                self._set_error("目录不存在或不是文件夹")
                return
        except OSError:
            self._set_error("目录不存在或无权访问")
            return
        self._jump(target)
        event.input.value = ""


class _PickerApp(App[Path | None]):
    """Run the reusable SelectDirectory modal as a standalone terminal view."""

    def __init__(self, title: str, location: Path) -> None:
        super().__init__()
        self._picker_title = title
        self._location = location

    @work
    async def on_mount(self) -> None:
        chosen = await self.push_screen_wait(
            PyCutSelectDirectory(
                location=self._location,
                title=self._picker_title,
                select_button="选择当前目录",
                cancel_button="返回",
            )
        )
        self.exit(chosen)


def choose_directory(title: str, initial_dir: Path) -> str | None:
    """Temporarily suspend curses, run a pure TUI picker, and restore curses.

    No GUI/Tk/AppleScript, shell subprocesses, or custom directory listing.
    A finally block must restore terminal input mode for Esc and arrow keys.
    """
    curses.def_prog_mode()
    try:
        curses.endwin()
        chosen = _PickerApp(title, initial_dir).run()
    finally:
        curses.reset_prog_mode()
    return str(chosen) if chosen is not None else None
