"""Shared material enumeration and compatibility directory helpers.

Processing uses recursive root merging and system/hidden-directory pruning.
Legacy directory/search helpers remain separate from the Textual picker."""
from __future__ import annotations

import os
from pathlib import Path


VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")


DEFAULT_MAX_DEPTH = 3

# Processing exclusions do not control compatibility directory listings.
SYSTEM_DIR_NAMES = {
    "system", "library", "private", "usr", "bin", "sbin",
    "opt", "etc", "tmp", "var", "dev", "cores", "net", "home",
    "proc", "sys", "lost+found",
}


SKIP_DIR_NAMES = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", ".DS_Store",
}


def _is_video(name: str) -> bool:
    """Match supported video extensions case-insensitively."""
    return name.lower().endswith(VIDEO_EXTS)


def _is_hidden_dir(name: str) -> bool:
    """Return whether a directory name begins with a dot."""
    return name.startswith(".")


def _is_system_dir(path: Path) -> bool:
    """Match processing exclusions by case-insensitive basename or hidden prefix."""
    name = path.name.lower()
    if name in SYSTEM_DIR_NAMES or name in SKIP_DIR_NAMES:
        return True
    return _is_hidden_dir(path.name)


def list_dirs(root: Path, *, raise_on_error: bool = False) -> list[Path]:
    """Return immediate non-hidden directories, including valid directory symlinks.

    This compatibility helper does not drive the Textual picker. Unreadable roots
    return an empty list unless raise_on_error is enabled."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in os.scandir(str(root)):
            try:
                if entry.name.startswith("."):
                    continue
                # List valid directory links here; do not recurse through them.
                if entry.is_dir(follow_symlinks=True):
                    out.append(Path(entry.path))
            except OSError:
                # A broken link or inaccessible entry must not hide other directories.
                continue
    except OSError:
        if raise_on_error:
            raise
    out.sort(key=lambda p: (p.name.casefold(), p.name))
    return out


def list_videos_recursive(root, max_depth: int | None = DEFAULT_MAX_DEPTH,
                          *, raise_on_error: bool = False) -> list[Path]:
    """Merge roots and deduplicate real paths; None means unlimited depth.

    Do not recurse through directory symlinks. Propagate scan errors when requested."""
    from ..settings import directories
    files = {}
    def on_error(error):
        if raise_on_error:
            raise error
    for folder in directories(root):
        if not folder.is_dir():
            if raise_on_error:
                raise NotADirectoryError(f"素材目录不存在或无法访问: {folder}")
            continue
        for dirpath, dirnames, filenames in os.walk(folder, onerror=on_error):
            dirnames[:] = [d for d in dirnames if not _is_system_dir(Path(dirpath) / d)]
            rel = Path(dirpath).relative_to(folder)
            if max_depth is not None and len(rel.parts) > max_depth:
                dirnames[:] = []
                continue
            for name in filenames:
                path = Path(dirpath) / name
                if _is_video(name):
                    files.setdefault(path.resolve(), path)
    return sorted(files.values(), key=lambda p: (str(p.parent), p.name.lower()))


def search_videos(root: Path, keyword: str, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """Find videos by casefolded filename substring using the normal recursive scan limits."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    kw = keyword.strip().casefold()
    if not kw:
        return []
    files = list_videos_recursive(root, max_depth=max_depth)
    return [f for f in files if kw in f.name.casefold()]
