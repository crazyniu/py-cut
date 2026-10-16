"""Shared sibling output directories and flat filenames."""
from datetime import datetime
from pathlib import Path
import os
import re
import unicodedata

BUCKETS = ("有人脸", "无人脸")


def source_root(src: Path) -> Path:
    for parent in src.parents:
        if parent.name in BUCKETS:
            return parent.parent
    return src.parent


def sibling(root: Path, kind: str) -> Path:
    root = root.expanduser().resolve()
    return root.parent / kind


def slice_output_root(source: Path, explicit: str = "") -> Path:
    """CLI/TUI共用：显式输出 > 配置画中画目录 > 源目录同级切片。"""
    from .. import config
    if explicit:
        return Path(explicit).expanduser()
    return config.PIP_DIR if os.environ.get("PYCUT_PIP_DIR", "").strip() else sibling(source, "切片")


def montage_output(root: Path) -> Path:
    return sibling(root, "混剪") / datetime.now().strftime("%m%d")


def flat_output(src: Path, root: Path) -> Path:
    bucket = next((p.name for p in src.parents if p.name in BUCKETS), "")
    name = src.stem if re.search(r"_[1-9][0-9]*$", src.stem) else src.stem + "_1"
    return root / bucket / (name + ".mp4")


def validate_targets(pairs: list[tuple[Path, Path]]) -> None:
    seen = {}
    inputs = {src.resolve() for src, _ in pairs}
    for src, dst in pairs:
        key = unicodedata.normalize("NFC", str(dst.resolve())).casefold()
        if dst.resolve() in inputs:
            raise ValueError(f"输出目录不能覆盖输入视频: {dst}")
        if key in seen and seen[key].resolve() != src.resolve():
            raise ValueError(f"输出文件同名冲突: {seen[key]} / {src} -> {dst}")
        seen[key] = src


def matching_root(src: Path, roots) -> Path:
    """嵌套根优先归属最长路径；每个文件只输出一次。"""
    from ..settings import directories
    parents = [root for root in directories(roots) if src.resolve().is_relative_to(root)]
    return max(parents, key=lambda p: len(p.parts)) if parents else source_root(src)
