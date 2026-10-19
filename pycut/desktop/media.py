"""On-demand, stat-and-tool-keyed desktop thumbnails and compatible previews.

These disposable UI files never replace originals or feed processing. FFmpeg
children are bounded by timeouts; failed/changed-source renders are not cached.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

from .. import config
from .store import Store, stamp

PROBE_ARGS = ["-v", "error", "-show_entries",
              "stream=codec_type,codec_name,pix_fmt,width,height,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
              "-of", "json"]


def tool_identity(executable: str) -> str:
    path = Path(shutil.which(executable) or executable).resolve()
    return json.dumps([str(path), stamp(path)])


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError((result.stderr.strip()[-1500:] or "媒体工具执行失败"))
    return result


DETAIL_ARGS = [*PROBE_ARGS]
DETAIL_ARGS[3] = DETAIL_ARGS[3].replace("height,duration", "height,duration,avg_frame_rate,r_frame_rate,sample_rate,channels")


def probe_metadata(path: Path, kind: str, *, details: bool = False) -> dict:
    """Probe display metadata only; caller owns validity and catalog persistence."""
    data = json.loads(_run([config.FFPROBE, *(DETAIL_ARGS if details else PROBE_ARGS), str(path)], 30).stdout)
    video = next((stream for stream in data.get("streams", [])
                  if stream.get("codec_type") == "video"), None)
    if video is None or int(video.get("width", 0)) <= 0 or int(video.get("height", 0)) <= 0:
        raise ValueError("无法读取视频或图片尺寸")
    width, height = int(video["width"]), int(video["height"])
    rotation = (video.get("tags") or {}).get("rotate", 0)
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    if int(float(rotation)) % 180:
        width, height = height, width
    duration = 0.0
    if kind == "video":
        try:
            duration = float(video.get("duration"))
        except (TypeError, ValueError):
            duration = float(data.get("format", {}).get("duration", 0))
        if duration <= 0:
            raise ValueError("无法读取视频时长")
    extra = {}
    if details:
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
        try:
            numerator, denominator = rate.split("/")
            fps = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            fps = None
        extra = {"fps": fps, "size": path.stat().st_size,
                 "audio": [{"codec": stream.get("codec_name"), "sample_rate": stream.get("sample_rate"),
                            "channels": stream.get("channels")} for stream in data.get("streams", [])
                           if stream.get("codec_type") == "audio"]}
    return {**extra, "duration": duration, "width": width, "height": height,
            "video_codec": video.get("codec_name"), "pixel_format": video.get("pix_fmt"),
            "audio_codecs": [stream.get("codec_name") for stream in data.get("streams", [])
                             if stream.get("codec_type") == "audio"]}


class MediaCache:
    """Serialize preview generation and cap disposable cache disk usage."""

    def __init__(self, store: Store, max_bytes: int = 2 * 1024 ** 3):
        self.store = store
        self.directory = store.data_dir / "cache" / "media"
        self.directory.mkdir(parents=True, exist_ok=True)
        if max_bytes <= 0:
            raise ValueError("缓存容量必须大于零")
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._prune(None)

    def _asset(self, asset_id: str) -> dict:
        return self.store.asset_detail(asset_id)

    def details_path(self, path: Path) -> dict:
        """Probe one requested file; reuse only matching stat/tool/query metadata."""
        path = Path(path).resolve()
        with self._lock:
            before = stamp(path)
            key = self._key(path, before, "details-v1:" + json.dumps(DETAIL_ARGS), config.FFPROBE)
            target = self.directory / f"{key}.json"
            try:
                cached = json.loads(target.read_text(encoding="utf-8"))
                value = cached["data"]
                if cached["key"] == key and value["width"] > 0 and value["height"] > 0 and isinstance(value["audio"], list):
                    return value
            except (OSError, ValueError, KeyError, TypeError):
                pass
            result = probe_metadata(path, "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else "video", details=True)
            if stamp(path) != before:
                raise ValueError("素材已变化，请刷新后重试")
            temporary = target.with_name(f".{key}-{uuid.uuid4().hex}.json")
            try:
                temporary.write_text(json.dumps({"key": key, "data": result}), encoding="utf-8")
                temporary.replace(target)
                self._prune(target)
            finally:
                temporary.unlink(missing_ok=True)
            return result

    def thumbnail(self, asset_id: str) -> str:
        asset = self._asset(asset_id)
        if asset["kind"] == "subtitle":
            raise ValueError("字幕文件没有缩略图")
        return self.thumbnail_path(Path(asset["path"]))

    def thumbnail_path(self, path: Path) -> str:
        path = Path(path).resolve()
        with self._lock:
            before = stamp(path)
            key = self._key(path, before, "thumbnail-v1", config.FFMPEG)
            target = self.directory / f"{key}.jpg"
            if target.is_file():
                target.touch()
                return str(target)
            temporary = target.with_name(f".{key}-{uuid.uuid4().hex}.jpg")
            try:
                _run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf",
                      "scale=w=360:h=640:force_original_aspect_ratio=decrease", "-q:v", "3",
                      str(temporary)], 30)
                if stamp(path) != before:
                    raise ValueError("素材已变化，请刷新后重试")
                if not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError("无法生成缩略图")
                temporary.replace(target)
                self._prune(target)
                return str(target)
            finally:
                temporary.unlink(missing_ok=True)

    def preview(self, asset_id: str) -> str:
        asset = self._asset(asset_id)
        if asset["kind"] == "subtitle":
            raise ValueError("字幕文件不能单独播放")
        return self.preview_path(Path(asset["path"]))

    def preview_path(self, path: Path) -> str:
        """Preview indexed assets or published output paths without altering sources."""
        path = Path(path).resolve()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            with path.open("rb"):
                return str(path)
        with self._lock:
            before = stamp(path)
            metadata = probe_metadata(path, "video")
            if (path.suffix.lower() in {".mp4", ".m4v"}
                    and metadata["video_codec"] == "h264" and metadata["pixel_format"] == "yuv420p"
                    and all(codec in {"aac", "mp3"} for codec in metadata["audio_codecs"])):
                return str(path)
            key = self._key(path, before, "preview-v1", config.FFMPEG)
            target = self.directory / f"{key}.mp4"
            if target.is_file():
                target.touch()
                return str(target)
            remaining = self.max_bytes - sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
            if remaining < min(int(metadata["duration"] * 150000), self.max_bytes):
                self._prune(None, reserve=min(int(metadata["duration"] * 150000), self.max_bytes))
            # This UI proxy is not a quality or audio-preservation processing path.
            temporary = target.with_name(f".{key}-{uuid.uuid4().hex}.mp4")
            try:
                _run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                      "-vf", "scale=w=720:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary)],
                     max(120, min(1800, int(metadata["duration"] * 3))))
                if stamp(path) != before:
                    raise ValueError("素材已变化，请刷新后重试")
                if not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError("无法生成预览")
                if temporary.stat().st_size > self.max_bytes:
                    raise ValueError("此素材预览超过缓存容量，请使用系统播放器")
                temporary.replace(target)
                self._prune(target)
                return str(target)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(path: Path, signature: str, purpose: str, executable: str) -> str:
        value = json.dumps([str(path), signature, tool_identity(executable), purpose])
        return hashlib.sha256(value.encode()).hexdigest()

    def _prune(self, keep: Path | None, reserve: int = 0) -> None:
        files = [(path, path.stat()) for path in self.directory.iterdir()
                 if path.is_file()]
        total = sum(info.st_size for _, info in files)
        for path, info in sorted(files, key=lambda pair: pair[1].st_mtime_ns):
            if total + reserve <= self.max_bytes:
                break
            if path != keep:
                path.unlink(missing_ok=True)
                total -= info.st_size
