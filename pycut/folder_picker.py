"""Native folder-selection dialogs for the TUI.

This module does not enumerate directories or interpret typed paths.
macOS uses the system's choose-folder dialog (Standard Additions); other
desktop systems use the open-source Tk native directory picker in tkinter.
Both support standard system locations, mounted disks, and folder validation.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


_MACOS_CHOOSE_FOLDER = """on run argv
    set dialogTitle to item 1 of argv
    set startPath to item 2 of argv
    try
        set initialFolder to (POSIX file startPath) as alias
        set chosenFolder to choose folder with prompt dialogTitle ¬
            default location initialFolder invisibles false ¬
            multiple selections allowed false
        return POSIX path of chosenFolder
    on error number -128
        return ""
    end try
end run"""


def _macos_choose_folder(title: str, initial_dir: Path) -> str | None:
    """Show macOS' native folder dialog without handing the terminal to it."""
    # The title and initial path are positional arguments to osascript, not
    # interpolated into AppleScript source or executed by a shell.
    proc = subprocess.run(
        ["osascript", "-e", _MACOS_CHOOSE_FOLDER, title, str(initial_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode:
        if "-128" in proc.stderr:
            return None
        raise OSError("macOS 文件夹选择窗口无法打开：" + proc.stderr.strip())
    return proc.stdout.strip() or None


def _tk_choose_folder(title: str, initial_dir: Path) -> str | None:
    """Use the mature open-source Tk native folder dialog on other desktops."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise OSError("当前 Python 缺少 tkinter/Tk 图形文件夹选择组件") from exc
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        raise OSError("无法启动图形文件夹选择窗口：" + str(exc)) from exc
    try:
        window.withdraw()
        selected = filedialog.askdirectory(
            parent=window,
            initialdir=str(initial_dir),
            title=title,
            mustexist=True,
        )
    finally:
        window.destroy()
    return selected or None


def choose_directory(title: str, initial_dir: Path) -> str | None:
    """Return a native dialog's chosen directory or None for cancellation.

    Callers validate the selected directory immediately before processing,
    since removable disks may disappear while the dialog is open.
    """
    if sys.platform == "darwin":
        return _macos_choose_folder(title, initial_dir)
    return _tk_choose_folder(title, initial_dir)
