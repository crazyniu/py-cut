"""FFmpeg probing and startup checks.

VideoToolbox uses bitrate rather than CRF. FFmpeg 9 removed -vsync; use -fps_mode cfr."""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from .utils import fmt_bytes, log, run_cmd

FFMPEG = config.FFMPEG
FFPROBE = config.FFPROBE


@dataclass
class VideoInfo:
    path: Path
    stem: str
    duration: float
    width: int
    height: int
    has_audio: bool
    rotation: int          # Degrees; used for display and extraction-orientation fallback.
    bitrate: str


def probe_video(path: Path) -> VideoInfo | None:
    """Return basic video metadata, or None when probing fails."""
    args = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:stream_tags=rotate:stream_side_data=rotation",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    rc, out, _err = run_cmd(args, timeout=120)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    vs = streams[0]

    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    width = int(_f(vs.get("width")))
    height = int(_f(vs.get("height")))
    if width <= 0 or height <= 0:
        # A valid duration does not make a zero-dimension stream decodable.
        return None
    dur = _f(vs.get("duration"))
    if dur <= 0:
        dur = _f((data.get("format") or {}).get("duration"))
    if dur <= 0:
        return None

    rotation = 0
    v = (vs.get("tags") or {}).get("rotate")
    if v is not None:
        try:
            rotation = int(v) % 360
        except (TypeError, ValueError):
            rotation = 0
    if rotation == 0:
        for sd in vs.get("side_data_list") or []:
            v = sd.get("rotation")
            if v is not None:
                try:
                    rotation = int(round(float(v))) % 360
                    break
                except (TypeError, ValueError):
                    pass

    rc2, out2, _ = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        timeout=60,
    )
    has_audio = rc2 == 0 and out2.strip() != ""

    return VideoInfo(
        path=path,
        stem=path.stem,
        duration=dur,
        width=width,
        height=height,
        has_audio=has_audio,
        rotation=rotation,
        bitrate=config.BITRATE_LOW if height <= 720 else config.BITRATE_HIGH,
    )


def probe_scalar(path: Path, entry: str) -> str:
    """Return raw default=nw=1:nk=1 probe output, or an empty string on failure.

    Without stream selection the output can contain multiple lines."""
    rc, out, _ = run_cmd(
        [FFPROBE, "-v", "error", "-show_entries", entry,
         "-of", "default=nw=1:nk=1", str(path)],
        timeout=30,
    )
    return out.strip() if rc == 0 else ""


def self_check(require_src: bool = True) -> Any:
    """Check cv2, media tools, optional source directory, and free disk space; exit on failure."""
    try:
        import cv2  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[自检失败] 无法 import cv2: {e}\n"
                 f"请使用 {sys.executable} 运行（该解释器含 cv2 4.13.0）。")

    log(f"[自检] cv2 {cv2.__version__} @ {sys.executable}")

    for exe in (FFMPEG, FFPROBE):
        if not Path(exe).is_file() or not os_access(exe):
            sys.exit(f"[自检失败] ffmpeg/ffprobe 不可执行: {exe}")

    rc, out, err = run_cmd([FFPROBE, "-version"], timeout=30)
    if rc != 0:
        sys.exit(f"[自检失败] ffprobe 无法运行: {err.strip()}")
    log(f"[自检] ffmpeg OK: {out.splitlines()[0] if out else 'ok'}")

    if require_src and not config.SRC_DIR.is_dir():
        sys.exit(f"[自检失败] 源目录不存在: {config.SRC_DIR}")

    try:
        free = shutil.disk_usage(str(config.DESKTOP)).free
    except Exception:
        free = shutil.disk_usage("/").free
    if free < config.MIN_FREE_BYTES:
        sys.exit(f"[自检失败] 磁盘剩余空间不足: {fmt_bytes(free)} < "
                 f"{fmt_bytes(config.MIN_FREE_BYTES)}")
    log(f"[自检] 磁盘剩余 {fmt_bytes(free)}")

    return cv2


def os_access(exe: str) -> bool:
    import os  # noqa: PLC0415
    return os.access(exe, os.X_OK)
