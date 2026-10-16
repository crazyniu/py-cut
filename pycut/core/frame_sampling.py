"""Bounded FFmpeg multi-input/multi-output sampling; no model or extra dependency."""
from __future__ import annotations

import math
import subprocess
import tempfile
import time
from pathlib import Path

from .. import config
from .runtime import STOP


class FrameExtractionError(RuntimeError):
    """Sampling failed; callers must not classify the video as subtitle-free."""


def grab_frames(clip: Path, times: list[float], folder: Path, *,
                width: int = 480, timeout: float = 30,
                ffmpeg: str | None = None) -> list[Path]:
    """Extract the same seek samples as individual FFmpeg calls in bounded batches.

    Multiple independent input seeks avoid decoding an entire long video just to
    select a few distant frames. All outputs share a process (up to eight seeks),
    with one decoder/encoder thread per sample. The entire request has one deadline.
    """
    if width < 2 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("抽帧尺寸或超时无效")
    if any(not math.isfinite(t) or t < 0 for t in times):
        raise ValueError("抽帧时间必须是有效非负秒数")
    deadline = time.monotonic() + timeout
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    outputs = []
    for start in range(0, len(times), 8):
        if STOP.flag:
            raise FrameExtractionError("用户中断字幕检测")
        if time.monotonic() >= deadline:
            raise TimeoutError("采样帧提取超时")
        group = times[start:start + 8]
        paths = [folder / f"f{start + i}.jpg" for i in range(len(group))]
        cmd = [ffmpeg or config.FFMPEG, "-y", "-loglevel", "error", "-nostdin"]
        for t in group:
            cmd += ["-ss", f"{t:.3f}", "-threads", "1", "-i", str(clip)]
        for i, path in enumerate(paths):
            cmd += ["-map", f"{i}:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
                    "-filter_threads", "1", "-vf", f"scale={width}:-2",
                    "-q:v", "4", "-threads", "1", "-fps_mode", "passthrough", str(path)]
        proc = None
        try:
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=errors)
                while proc.poll() is None:
                    if STOP.flag:
                        raise FrameExtractionError("用户中断字幕检测")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("采样帧提取超时")
                    try:
                        proc.wait(timeout=min(.1, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                if proc.returncode or any(not p.is_file() or not p.stat().st_size for p in paths):
                    errors.seek(0)
                    detail = errors.read().decode("utf-8", "replace")[-2000:]
                    raise FrameExtractionError(f"采样帧提取不完整：{detail.strip()}")
        except TimeoutError:
            raise
        except OSError as exc:
            raise FrameExtractionError(f"无法抽取采样帧：{exc}") from exc
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
        if STOP.flag:
            raise FrameExtractionError("用户中断字幕检测")
        outputs.extend(paths)
    return outputs
