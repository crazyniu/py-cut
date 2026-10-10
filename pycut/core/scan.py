"""目录与视频扫描工具：为 TUI 引导式向导提供「列目录 / 数视频 / 列视频 / 查视频」能力。

设计动机：
    TUI 重构成「引导式向导」后，每个功能的第一步都是「让用户选一个目录」，
    第二步是「递归统计里面有多少视频」，混剪还要「非递归列出爆款视频目录下
    的视频供多选」，另外用户需要「查询视频素材」。这些是跨命令的通用能力，
    收敛到本模块，命令层与 TUI 共用，避免各处重复写 os.walk / glob。

关于递归与深度：
    * 切片 / 字幕擦除 / 去重 / 混剪片段库 —— **递归**，但**最多下探 3 层**
      （用户反馈：素材目录太深会扫到无关内容，限制层级更可控）。
    * 混剪爆款视频 —— **不递归**：只列出用户指定目录**正下方**的视频文件，
      便于逐个勾选。

目录显示与处理范围彼此独立：
    * 目录选择器列出操作系统返回的全部子目录，包括 `.` 开头的隐藏目录、
      System/Library 等系统目录及指向目录的符号链接；不主动筛掉任何目录名称。
    * 视频处理的递归统计/素材收集仍按既有 `_is_system_dir` 规则剪枝，
      保留已标定的深度、视频范围与处理逻辑。
    * 操作系统因权限限制不可读取的目录内容无法由 TUI 强行列出。

macOS 陷阱（沿用 slice_pipeline 的经验）：
    `find -iname` 会静默丢文件，必须用 os.walk / os.scandir 遍历。
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


def list_dirs(root: Path) -> list[Path]:
    """列出 root 的所有一级子目录，包含隐藏、系统和目录符号链接。

    仅服务 TUI 目录浏览；视频处理的递归剪枝由其他函数独立负责。
    无法访问或已经失效的目录，仍遵循操作系统的权限/错误结果。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        with os.scandir(str(root)) as entries:
            for entry in entries:
                try:
                    # 单层列举不会递归进入链接；目录链接也应像普通目录显示。
                    if entry.is_dir(follow_symlinks=True):
                        out.append(Path(entry.path))
                except OSError:
                    # 已卸载的卷、失效的链接或权限错误不影响其他目录。
                    continue
    except OSError:
        pass
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
