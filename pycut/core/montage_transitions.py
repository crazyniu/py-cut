"""Independent brightness fades for normalized silent segments.

No overlap or duration changes. This module constructs filters/commands only."""
from __future__ import annotations

import math
from pathlib import Path

from .. import config
from .montage_clips import CRF_DEFAULT, _enc_args


def fade_duration(segment_duration: float, requested: float) -> float:
    """Clamp each end fade to half the segment duration."""
    duration = float(segment_duration)
    seconds = float(requested)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("片段时长必须为有效正数")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("转场时长必须为有效正数")
    return min(seconds, duration / 2.0)


def seam_fade_filters(duration: float, seconds: float, first: bool = False,
                      last: bool = False, *, start: float = 0.0) -> list[str]:
    """Offset fades onto the pre-output-seek timeline without changing the frame budget."""
    length = float(duration)
    fade = fade_duration(length, seconds)
    filters = []
    if not first:
        at = "0" if start == 0 else f"{start:.6f}"
        filters.append(f"fade=t=in:st={at}:d={fade:.6f}")
    if not last:
        filters.append(f"fade=t=out:st={start + length - fade:.6f}:d={fade:.6f}")
    return filters


def build_seam_fade_cmd(src: Path, out: Path, *,
                        duration: float, seconds: float = 0.25,
                        first: bool = False, last: bool = False,
                        fps: int | None = None,
                        crf: str = CRF_DEFAULT) -> list[str]:
    """Build silent nonoverlapping fades, skipping first fade-in and last fade-out."""
    from .ffmpeg import FFMPEG

    filters = seam_fade_filters(duration, seconds, first, last)
    if not filters:
        return []
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vf", ",".join(filters),
        *_enc_args(Path(out), fps or config.MONTAGE_FPS, str(crf)),
    ]
