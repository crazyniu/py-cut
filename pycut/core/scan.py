"""视频处理素材范围统计和兼容目录列举接口。

视频处理使用已有递归深度及系统目录剪枝规则，保持 CLI 与 TUI 的
处理结果一致。目录选择 UI 已委托给 pycut/folder_picker.py 的开源 Textual
终端目录选择组件，不再自行调用 list_dirs() 构造文件系统浏览器。

list_dirs() 仅作为原有兼容接口保留，显示非点前缀目录，
与真正用于处理素材的递归扫描规则相互独立。
"""
from __future__ import annotations

import os
from pathlib import Path

# 视频扩展名（统一小写匹配）
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")

# 递归扫描的默认最大深度（相对 root 下探层数）
DEFAULT_MAX_DEPTH = 3

# 视频处理递归剪枝所用系统目录黑名单（不用于 TUI 目录列表）
SYSTEM_DIR_NAMES = {
    "system", "library", "private", "usr", "bin", "sbin",
    "opt", "etc", "tmp", "var", "dev", "cores", "net", "home",
    "proc", "sys", "lost+found",
}

# 额外隐藏的常见非素材目录（无论层级，只要名字命中就跳过）
SKIP_DIR_NAMES = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", ".DS_Store",
}


def _is_video(name: str) -> bool:
    """按扩展名判视频（大小写不敏感）。"""
    return name.lower().endswith(VIDEO_EXTS)


def _is_hidden_dir(name: str) -> bool:
    """隐藏目录：`.` 开头（.git / .Trash / .Spotlight-V100 等）。"""
    return name.startswith(".")


def _is_system_dir(path: Path) -> bool:
    """系统级目录：命中黑名单（按末段名，大小写不敏感）。"""
    name = path.name.lower()
    if name in SYSTEM_DIR_NAMES or name in SKIP_DIR_NAMES:
        return True
    return _is_hidden_dir(path.name)


def list_dirs(root: Path, *, raise_on_error: bool = False) -> list[Path]:
    """列出 root 下名称不以 `.` 开头的一级子目录。

    兼容旧目录列举调用，包括 Library/System 等系统目录和目录链接。
    此接口不再供 TUI 选文件夹使用；真正的目录选择委托 Textual 终端组件。
    默认遇到不可读目录返回空列表，raise_on_error=True 可转交错误。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in os.scandir(str(root)):
            try:
                # 仅隐藏点开头的文件夹（包括同名目录软链接），不限制其他名称。
                if entry.name.startswith("."):
                    continue
                # 单层列举不会递归进入链接；有效目录链接保持可见。
                if entry.is_dir(follow_symlinks=True):
                    out.append(Path(entry.path))
            except OSError:
                # 已卸载的卷、失效的链接或权限错误不影响其他目录。
                continue
    except OSError:
        if raise_on_error:
            raise
    out.sort(key=lambda p: (p.name.casefold(), p.name))
    return out


def count_videos_recursive(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> int:
    """递归统计目录下视频数量（不返回路径，只数数，快），最多下探 max_depth 层。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        return 0
    n = 0
    base = str(root)
    for dirpath, dirnames, filenames in os.walk(base):
        # 剪枝：跳过系统目录与隐藏目录
        dirnames[:] = [
            d for d in dirnames if not _is_system_dir(Path(dirpath) / d)
        ]
        # 深度限制：超过 max_depth 不再往下钻
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        n += sum(1 for fn in filenames if _is_video(fn))
    return n


def list_videos_recursive(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """递归列出目录下所有视频（按「父目录 + 文件名」排序，稳定），最多下探 max_depth 层。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    files: list[Path] = []
    base = str(root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if not _is_system_dir(Path(dirpath) / d)
        ]
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        for fn in filenames:
            if _is_video(fn):
                files.append(Path(dirpath) / fn)
    files.sort(key=lambda p: (str(p.parent), p.name.lower()))
    return files


def list_videos_flat(root: Path) -> list[Path]:
    """**非递归**列出目录正下方的视频（混剪「爆款视频目录」用）。

    只扫一层，不 desc 子目录——这样 TUI 里能清楚区分「这个视频」与「这个子目录」，
    多选时不会误把子目录里的也拉进来。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    files: list[Path] = []
    try:
        for entry in os.scandir(str(root)):
            if entry.is_file() and _is_video(entry.name):
                files.append(Path(entry.path))
    except OSError:
        pass
    files.sort(key=lambda p: p.name.lower())
    return files


def search_videos(root: Path, keyword: str, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """按关键词**递归搜索**视频素材（文件名包含 keyword，大小写不敏感）。

    供「查询视频素材」用：用户输入关键词，返回所有文件名匹配的视频路径，
    同样遵守系统目录过滤与 max_depth 限制。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    kw = keyword.strip().casefold()
    if not kw:
        return []
    files = list_videos_recursive(root, max_depth=max_depth)
    return [f for f in files if kw in f.name.casefold()]
