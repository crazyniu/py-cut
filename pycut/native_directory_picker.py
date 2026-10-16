"""Open an OS folder dialog in a bounded child process; never upload files."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

_TIMEOUT = 120
_LOCK = threading.Lock()
_MAC_SCRIPT = '''on run argv
    try
        set folderChoice to choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))
        return POSIX path of folderChoice
    on error number -128
        return ""
    end try
end run'''
_WINDOWS_SCRIPT = '''[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:PYCUT_PICKER_TITLE
$dialog.SelectedPath = $env:PYCUT_PICKER_START
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.SelectedPath)
    }
} finally { $dialog.Dispose() }'''
_TK_SCRIPT = '''import os, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
try:
    selected = filedialog.askdirectory(parent=root, title=os.environ['PYCUT_PICKER_TITLE'], initialdir=os.environ['PYCUT_PICKER_START'], mustexist=True)
    print(selected or '')
finally:
    root.destroy()
'''


class PickerError(RuntimeError):
    """A usable explanation for the local settings UI."""


def choose_directory(initial: str, project_root: Path, title: str) -> str | None:
    """Return a real absolute directory, None on cancel, or a clear GUI error."""
    if not _LOCK.acquire(blocking=False):
        raise PickerError('已有文件夹选择窗口打开，请先完成选择或取消')
    try:
        start = Path(os.path.expandvars(initial)).expanduser() if initial else Path.home()
        if not start.is_absolute():
            start = project_root / start
        if not start.is_dir():
            start = Path.home()
        env = {**os.environ, 'PYCUT_PICKER_TITLE': title, 'PYCUT_PICKER_START': str(start.resolve())}
        zenity = False
        if sys.platform == 'darwin':
            command = ['osascript', '-e', _MAC_SCRIPT, title, str(start.resolve())]
        elif sys.platform == 'win32':
            encoded = base64.b64encode(_WINDOWS_SCRIPT.encode('utf-16le')).decode('ascii')
            command = ['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', encoded]
        elif shutil.which('zenity'):
            zenity = True
            command = ['zenity', '--file-selection', '--directory', '--title='+title, '--filename='+str(start.resolve())+os.sep]
        else:
            if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
                raise PickerError('当前环境没有图形桌面，无法打开系统窗口；请手动输入目录路径')
            command = [sys.executable, '-c', _TK_SCRIPT]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise PickerError('文件夹选择等待超时，请重新选择或手动输入路径') from exc
        except OSError as exc:
            raise PickerError('无法启动系统文件夹窗口，请手动输入目录路径') from exc
        if zenity and result.returncode == 1 and not result.stderr.strip():
            return None
        if result.returncode != 0:
            raise PickerError('系统文件夹窗口不可用，请确认图形桌面已启动，或手动输入路径')
        value = result.stdout.rstrip('\r\n')
        if not value:
            return None
        selected = Path(value)
        if not selected.is_absolute() or not selected.is_dir():
            raise PickerError('选择的文件夹不存在或无法访问，请重新选择或手动输入路径')
        return str(selected.resolve())
    finally:
        _LOCK.release()
