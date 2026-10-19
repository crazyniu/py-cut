"""Local directory navigation for the Web settings service.

Browser folder APIs expose names or relative paths, so the local backend returns
absolute paths. List directories only and hide dot-prefixed names."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def _describe_error(exc: OSError) -> str:
    """Map filesystem access failures to user-facing messages."""
    if isinstance(exc, (NotADirectoryError, FileNotFoundError)):
        return "目录不存在"
    if isinstance(exc, PermissionError):
        return "无权访问该目录"
    return "无法访问该目录"


def resolve_path(value: str, project_root: Path) -> Path:
    """Expand home/environment variables; resolve relative paths against the project."""
    expanded = Path(os.path.expandvars(value or "")).expanduser()
    path = expanded if expanded.is_absolute() else project_root / expanded
    return path.resolve()


def roots(home: Path | None = None) -> list[dict]:
    """Return available home, user-folder, mounted-drive, and filesystem roots."""
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
    """Return {path, parent, dirs, roots, error} for immediate subdirectories.

    An empty path starts at home. Report inaccessible paths in error rather than raising."""
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
