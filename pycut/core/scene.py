"""Scene detection and bounded segment planning.

Use the calibrated FFmpeg scene score, not a differently scaled Python histogram.
Keep default scene-detection threading; explicit thread counts regressed this path."""
from __future__ import annotations

import re
import math

from .. import config
from .utils import run_cmd

PTS_RE = re.compile(r"\bpts_time:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)")


def detect_scenes(path, scene_th: float) -> list[float]:
    """Return scene-cut timestamps in seconds, excluding zero; propagate FFmpeg failure."""
    args = [
        config.FFMPEG, "-hide_banner", "-nostats",
        "-i", str(path),
        "-filter:v", f"select='gt(scene,{scene_th})',showinfo",
        "-f", "null", "-",
    ]
    rc, _out, err = run_cmd(args, timeout=1800)
    if rc != 0:
        # Decoder failure is not a valid no-scene-change result; propagate it.
        tail = err.strip().splitlines()
        reason = tail[-1] if tail else "没有错误输出"
        raise RuntimeError(f"场景检测失败 rc={rc}: {reason}")

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
        if t == t and t > 0:
            cuts.append(t)
    return cuts


def build_segments(cuts: list[float], duration: float,
                   min_seg: float, max_seg: float) -> list[tuple[float, float]]:
    """Deduplicate cuts, split long intervals, and merge short intervals without losing content."""
    if not (math.isfinite(duration) and duration > 0
            and math.isfinite(min_seg) and math.isfinite(max_seg)
            and 0 < min_seg <= max_seg and max_seg >= 2 * min_seg):
        raise ValueError("时长须为正数，分段范围须满足 0 < min_seg 且 max_seg >= 2 * min_seg")
    if duration < min_seg:
        return []

    deduped: list[float] = []
    for c in sorted(cuts):
        if c <= 0.05 or c >= duration - 0.05:
            continue
        if deduped and c - deduped[-1] < config.DEDUP:
            continue
        deduped.append(c)

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

    # Accumulate a short first interval until it can join a valid neighbour.
    segs: list[tuple[float, float]] = []
    start = 0.0
    for c in expanded:
        if c - start < min_seg:
            continue
        segs.append((start, c))
        start = c
    segs.append((start, duration))

    if len(segs) >= 2 and segs[-1][1] - segs[-1][0] < min_seg:
        # Keep the last interval end when merging a short tail; using its start drops footage.
        _tail_start, tail_end = segs.pop()
        prev_start, _prev_end = segs.pop()
        segs.append((prev_start, tail_end))

    # Merging a short tail can exceed max_seg, so rebalance again.
    bounded = []
    for start, end in segs:
        count = max(1, math.ceil((end - start) / max_seg))
        for i in range(count):
            bounded.append((round(start + (end - start) * i / count, 3),
                            round(start + (end - start) * (i + 1) / count, 3)))
    return bounded
