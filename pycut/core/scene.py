"""场景检测与分段。

必须用 ffmpeg 内置 scene 分数（select='gt(scene,TH)'）：阈值 0.30 是在本机标定的
（0.25→26 / 0.30→16 / 0.40→16 切点）。换成自算直方图差异会量纲不同、标定全废，
且 Python 逐帧解码比 ffmpeg 慢约 5 倍。不要加 -threads N（实测反而慢一倍）。
"""
from __future__ import annotations

import re

from .. import config
from .utils import run_cmd

PTS_RE = re.compile(r"\bpts_time:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)")


def detect_scenes(path, scene_th: float) -> list[float]:
    """返回切点（秒），不含 0.0。

    用 ffmpeg 内置 scene 分数：阈值 0.30 标定（见模块说明）。
    """
    args = [
        config.FFMPEG, "-hide_banner", "-nostats",
        "-i", str(path),
        "-filter:v", f"select='gt(scene,{scene_th})',showinfo",
        "-f", "null", "-",
    ]
    _rc, _out, err = run_cmd(args, timeout=1800)

    cuts: list[float] = []
    for line in err.splitlines():
        if "pts_time" not in line:
            continue
        m = PTS_RE.search(line)
        if not m:
            continue
        try:
            t = float(m.group(1))
        except ValueError:
            continue
        if t == t and t > 0:      # 排除 NaN
            cuts.append(t)
    return cuts


def build_segments(cuts: list[float], duration: float,
                   min_seg: float, max_seg: float) -> list[tuple[float, float]]:
    """切点 -> 段落。去重 / 合并过短段 / 切分超长段。"""
    # 1) 去重：间距 < DEDUP 的切点合并
    deduped: list[float] = []
    for c in sorted(cuts):
        if c <= 0.05 or c >= duration - 0.05:
            continue
        if deduped and c - deduped[-1] < config.DEDUP:
            continue
        deduped.append(c)

    # 2) 切分超长段
    expanded: list[float] = []
    prev = 0.0
    for c in deduped + [duration]:
        span = c - prev
        if span > max_seg:
            n = int(span // max_seg)
            step = span / (n + 1)
            for k in range(1, n + 1):
                expanded.append(prev + step * k)
        expanded.append(c)
        prev = c
    if expanded and expanded[-1] >= duration - 1e-6:
        expanded.pop()

    # 3) 合并过短段（并入相邻段，不删除内容）
    #    首段若过短也不能保留为碎片段 —— 让 start 持续累积，直到跨度 >= min_seg 才切出。
    segs: list[tuple[float, float]] = []
    start = 0.0
    for c in expanded:
        if c - start < min_seg:
            continue
        segs.append((start, c))
        start = c
    segs.append((start, duration))

    # 尾部过短 -> 并入上一段
    if len(segs) >= 2 and segs[-1][1] - segs[-1][0] < min_seg:
        a, _ = segs.pop()
        b0, _ = segs.pop()
        segs.append((b0, a))

    return [(round(s, 3), round(e, 3)) for s, e in segs if e - s > 0.01]
