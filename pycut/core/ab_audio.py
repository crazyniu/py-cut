"""AB audio material selection, RMS measurement, and stereo phase transforms.

These are signal edits, not speaker-identity replacement or platform acceptance
guarantees. Opposite-phase output can cancel when downmixed to mono.
"""
from __future__ import annotations

import os
import random
import re
from pathlib import Path

from .dedup_presets import derive_seed
from .ffmpeg import FFMPEG
from .runtime import check_stop
from .utils import run_cmd

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aif", ".aiff"}
RMS_SILENCE = 0.000001


def list_sources(source: str) -> tuple[str, ...]:
    """Read one audio file or a recursive directory; surface missing/read errors."""
    root = Path(source).expanduser().resolve()
    if root.is_file():
        return (str(root),)
    if not root.is_dir():
        raise ValueError(f"AB 音频素材不存在: {root}")

    def fail(error):
        raise error

    paths = set()
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
        paths.update(str((Path(folder) / name).resolve()) for name in files
                     if Path(name).suffix.lower() in AUDIO_EXTENSIONS)
    if not paths:
        raise ValueError(f"AB 音频目录没有音频文件: {root}")
    return tuple(sorted(paths))


def select_source(sources: tuple[str, ...], seed: int) -> tuple[Path, float]:
    rng = random.Random(derive_seed(seed, "ab-audio"))
    return Path(rng.choice(sources)), rng.uniform(0.0411, 0.0501)


def measure_rms(path: Path, duration: float, timeout: int, *, loop: bool = False) -> float:
    """Measure decoded stereo RMS over exactly the portion used by the mix."""
    check_stop()
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "info"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-vn", "-af",
            f"aformat=sample_rates=48000:channel_layouts=stereo,atrim=duration={duration:.6f},"
            "astats=measure_perchannel=none:measure_overall=RMS_level",
            "-t", f"{duration:.6f}", "-f", "null", "-"]
    rc, _out, err = run_cmd(cmd, timeout=timeout)
    check_stop()
    levels = re.findall(r"RMS level dB:\s*(-inf|[-+0-9.eE]+)", err)
    if rc or not levels:
        raise ValueError(f"无法测量音量: {path.name}: {err[-300:]}")
    return 10 ** (float(levels[-1]) / 20)


def phase_filter(compatible: bool = False) -> str:
    if compatible:
        return "pan=stereo|FL=0.85*c0|FR=0.85*c1"
    return "aeval=((val(0)+val(1))*0.425)|(-((val(0)+val(1))*0.425)):c=stereo"


def too_quiet(output_rms: float, source_rms: float) -> bool:
    return source_rms > RMS_SILENCE and (
        output_rms <= RMS_SILENCE or output_rms < source_rms * 0.08)
