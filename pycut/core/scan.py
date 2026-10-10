"""目录与视频扫描工具：为 TUI 引导式向导提供「数视频 / 列视频」能力。

设计动机：
    TUI 重构成「引导式向导」后，每个功能的第一步都是「让用户选一个目录」，
    第二步是「递归统计里面有多少视频」，混剪还要「非递归列出爆款视频目录下
    的视频供多选」。这些是跨命令的通用能力，收敛到本模块，命令层与 TUI 共用，
    避免各处重复写 os.walk / glob。

关于递归的取舍：
    * 切片 / 字幕擦除 / 去重 / 混剪片段库 —— **递归**：素材常按平台分目录
      （抖音/快手）、切片桶下还有「视频名/001.mp4」两层，必须 os.walk 到底。
    * 混剪爆款视频 —— **不递归**：只列出用户指定目录**正下方**的视频文件，
      便于逐个勾选。

macOS 陷阱（沿用 slice_pipeline 的经验）：
    `find -iname` 会静默丢文件，必须用 os.walk / os.scandir 遍历。
"""
from __future__ import annotations

import os
from pathlib import Path

# 视频扩展名（统一小写匹配）
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")


def _is_video(name: str) -> bool:
    """按扩展名判视频（大小写不敏感）。"""
    return name.lower().endswith(VIDEO_EXTS)


def count_videos_recursive(root: Path) -> int:
    """递归统计目录下视频数量（不返回路径，只数数，快）。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        return 0
    n = 0
    for _dirpath, _dirnames, filenames in os.walk(str(root)):
        n += sum(1 for fn in filenames if _is_video(fn))
    return n


def list_videos_recursive(root: Path) -> list[Path]:
    """递归列出目录下所有视频（按「父目录 + 文件名」排序，稳定）。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(str(root)):
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
