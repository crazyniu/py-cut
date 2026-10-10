"""可选段间淡化效果：只处理已标准化的静音片段，不改变时长和时间槽。

每段首尾分别做独立亮度淡入/淡出；不会交叉重叠，不缩短时长。
此模块只构造 ffmpeg 参数，不执行外部命令，也不改动默认编码设置。
"""
from __future__ import annotations

import math
from pathlib import Path

from .. import config
from .montage_clips import CRF_DEFAULT, _enc_args, _probe_duration


def fade_duration(segment_duration: float, requested: float) -> float:
    """每端最多占片段时长的一半；不在两端制造重叠负时长。"""
    duration = float(segment_duration)
    seconds = float(requested)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("片段时长必须为有效正数")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("转场时长必须为有效正数")
    return min(seconds, duration / 2.0)


def build_seam_fade_cmd(src: Path, out: Path, *,
                        duration: float, seconds: float = 0.25,
                        first: bool = False, last: bool = False,
                        fps: int | None = None,
                        crf: str = CRF_DEFAULT) -> list[str]:
    """标准化片段的段间黑场淡化：第一个不淡入，最后一个不淡出。

    使用 fade 滤镜，而非 xfade 叠帧：不改变时间槽时长，保证后续字幕、
    画中画和基准原声音轨仍沿用原时间轴。仅适用于静音片段。
    """
    from .ffmpeg import FFMPEG

    length = float(duration)
    fade = fade_duration(length, seconds)
    filters = []
    if not first:
        filters.append(f"fade=t=in:st=0:d={fade:.6f}")
    if not last:
        filters.append(f"fade=t=out:st={length - fade:.6f}:d={fade:.6f}")
    if not filters:
        return []
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vf", ",".join(filters),
        *_enc_args(Path(out), fps or config.MONTAGE_FPS, str(crf)),
    ]


def probe_segment_duration(path: Path) -> float:
    """复用现有时长探测，无法识别的段不允许执行转场。"""
    duration = _probe_duration(Path(path))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"无法探测有效片段时长: {path}")
    return duration
