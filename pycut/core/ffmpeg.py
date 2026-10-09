"""ffmpeg / ffprobe 封装：视频探测、启动自检、probe_scalar 单字段查询。

硬件编码走 h264_videotoolbox（仅支持 -b:v，不支持 CRF）；
ffmpeg 9.0 起 -vsync 已移除，统一用 -fps_mode cfr。
"""
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
    rotation: int          # 0/90/180/270，仅用于提示与抽帧方向兜底
    bitrate: str


def probe_video(path: Path) -> VideoInfo | None:
    """探测视频基础信息；失败返回 None。"""
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
    dur = _f(vs.get("duration"))
    if dur <= 0:
        dur = _f((data.get("format") or {}).get("duration"))
    if dur <= 0:
        return None

    # 旋转信息：stream_tags.rotate 或 side_data rotation
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
    """ffprobe 取单个字段（default=nw=1:nk=1 格式），失败返回空串。"""
    rc, out, _ = run_cmd(
        [FFPROBE, "-v", "error", "-show_entries", entry,
         "-of", "default=nw=1:nk=1", str(path)],
        timeout=30,
    )
    return out.strip() if rc == 0 else ""


def self_check(require_src: bool = True) -> Any:
    """启动自检：cv2 / ffmpeg / ffprobe /（可选）源目录 / 磁盘空间。失败直接 sys.exit。"""
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
