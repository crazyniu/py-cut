"""本机 Web 配置页的目录浏览后端，纯标准库，返回真实绝对路径。

浏览器出于安全拿不到本地绝对路径：`<input type="file" webkitdirectory>`
只暴露相对根目录的 `webkitRelativePath`，File System Access API 的
`showDirectoryPicker` 也只返回不带路径的 `name`。因此由运行 pycut 的本机
服务自己枚举文件系统，前端只做导航并把返回的绝对路径回填到目录输入框。

与终端选择器（pycut/folder_picker.py）一致：隐藏点开头的目录，只列目录不列文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def _describe_error(exc: OSError) -> str:
    """把常见目录访问失败映射成清晰的用户提示。"""
    if isinstance(exc, (NotADirectoryError, FileNotFoundError)):
        return "目录不存在"
    if isinstance(exc, PermissionError):
        return "无权访问该目录"
    return "无法访问该目录"


def resolve_path(value: str, project_root: Path) -> Path:
    """展开 ~ 与环境变量；相对路径按项目根解析（与 settings.directory 一致）。"""
    expanded = Path(os.path.expandvars(value or "")).expanduser()
    path = expanded if expanded.is_absolute() else project_root / expanded
    return path.resolve()


def roots(home: Path | None = None) -> list[dict]:
    """候选起始位置：主目录/桌面/文档/下载/磁盘与文件系统根。"""
    h = home or Path.home()
    out: list[dict] = []

    def add(name: str, path: Path) -> None:
        try:
            if path.is_dir():
                out.append({"name": name, "path": str(path)})
        except OSError:
            pass

    add("主目录", h)
    add("桌面", h / "Desktop")
    add("文档", h / "Documents")
    add("下载", h / "Downloads")
    if sys.platform == "win32":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            add(f"{letter}: 盘", Path(f"{letter}:\\"))
    else:
        add("磁盘", Path("/Volumes") if sys.platform == "darwin" else Path("/mnt"))
        add("根目录", Path("/"))
    return out


def list_dir(path: str | None, project_root: Path | None = None) -> dict:
    """枚举 path 下的直接子目录，返回 {path, parent, dirs, roots, error}。

    path 为空时从主目录开始；不可访问或不存在时返回 error 字符串而不是抛异常。
    """
    root = project_root or config.PROJECT_ROOT
    target: Path
    if not path or not path.strip():
        target = Path.home()
    else:
        try:
            target = resolve_path(path, root)
        except (OSError, ValueError):
            return {"path": path, "parent": None, "dirs": [], "roots": roots(), "error": "目录不存在"}

    try:
        names: list[str] = []
        for entry in os.scandir(target):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    names.append(entry.name)
            except OSError:
                continue
    except OSError as exc:
        return {"path": str(target), "parent": None, "dirs": [], "roots": roots(), "error": _describe_error(exc)}

    names.sort(key=lambda n: (n.casefold(), n))
    dirs = [{"name": n, "path": str(target / n)} for n in names]
    parent = None if target.parent == target else str(target.parent)
    return {"path": str(target), "parent": parent, "dirs": dirs, "roots": roots(), "error": None}
