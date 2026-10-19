"""Prepend an optional intro after the body is complete; copy the body video stream."""
from __future__ import annotations

import json
import os
import random
import tempfile
from fractions import Fraction
from pathlib import Path

from .. import config
from .dedup_presets import derive_seed
from .montage_clips import _probe_one, concat_clips, fit_filter
from .scan import list_videos_recursive
from .runtime import check_stop
from .utils import log, run_cmd


def _video_signature(path: Path) -> dict | None:
    """Read video format, frame count, and audio presence; do not compare SPS/PPS bytes."""
    cmd = [
        config.FFPROBE, "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,sample_aspect_ratio,"
        "r_frame_rate,time_base,duration,nb_frames", "-of", "json", str(path),
    ]
    rc, output, _ = run_cmd(cmd, timeout=60)
    if rc:
        return None
    try:
        streams = json.loads(output)["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        result = dict(video)
        result["duration"] = float(video["duration"])
        result["nb_frames"] = int(video["nb_frames"])
        result["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
        if result["duration"] <= 0 or result["nb_frames"] <= 0:
            return None
        return result
    except (ValueError, TypeError, KeyError, StopIteration):
        return None


def _compatible(left: dict, right: dict) -> bool:
    """Allow profile/level differences handled by concat Annex B parameter sets."""
    fields = ("codec_name", "width", "height", "pix_fmt", "sample_aspect_ratio",
              "r_frame_rate", "time_base")
    return (left.get("codec_name") == "h264"
            and all(key in left and left[key] == right.get(key) for key in fields))


def _adapt_intro(intro: Path, body_info: dict, output: Path, crf: str) -> Path:
    """Remux or adapt only the intro; never re-encode the completed body video."""
    info = _video_signature(intro)
    if info is None:
        raise RuntimeError("无法读取已处理前贴的流参数")
    if _compatible(info, body_info):
        return intro
    try:
        time_base = Fraction(body_info["time_base"])
        fps = Fraction(body_info["r_frame_rate"])
        if (body_info.get("codec_name") != "h264" or time_base.numerator != 1
                or fps != config.MONTAGE_FPS or body_info.get("pix_fmt") != "yuv420p"):
            raise ValueError("正文不是标准 H.264/yuv420p/30fps")
        width, height = int(body_info["width"]), int(body_info["height"])
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise RuntimeError("正文流规格异常，拒绝用重新编码改变已完成正文") from exc
    spatial = ("codec_name", "width", "height", "pix_fmt", "sample_aspect_ratio", "r_frame_rate")
    copy = all(info.get(k) == body_info.get(k) for k in spatial)
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(intro),
           "-map", "0:v:0", "-an"]
    if copy:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-vf", fit_filter((width, height), config.MONTAGE_FPS),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
                "-pix_fmt", "yuv420p", "-fps_mode", "cfr"]
    cmd += ["-video_track_timescale", str(time_base.denominator),
            "-movflags", "+faststart", str(output)]
    rc, _, error = run_cmd(cmd, timeout=3600)
    adapted = _video_signature(output) if not rc else None
    if (adapted is None or not _compatible(adapted, body_info)
            or abs(adapted["duration"] - info["duration"]) > 1 / config.MONTAGE_FPS):
        raise RuntimeError(f"前贴流规格适配失败：{error.strip()[-500:]}")
    return output


def _video_copy_cmd(concat_file: Path, body: Path, intro_source: Path,
                    has_intro_audio: bool, duration: float,
                    keep_audio: bool, output: Path, *, body_duration: float) -> list[str]:
    """Copy video and join source audio; short audio must not shorten the picture."""
    cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-nostdin",
           "-f", "concat", "-safe", "0", "-auto_convert", "1", "-i", str(concat_file)]
    if keep_audio:
        cmd.extend(["-i", str(body)])
        if has_intro_audio:
            cmd.extend(["-i", str(intro_source)])
        else:
            cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        graph = (
            f"[2:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.6f}[a0];"
            f"[1:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,atrim=duration={body_duration:.6f}[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]"
        )
        cmd += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-map", "0:v:0", "-an", "-c:v", "copy"]
    return [*cmd, "-movflags", "+faststart", str(output)]


def prepend(video: Path, folder: Path, *, seed: int, canvas: tuple[int, int],
            stickers: list[Path], crf: str, keep_audio: bool) -> dict | None:
    check_stop()
    files = list_videos_recursive(folder, max_depth=None, raise_on_error=True)
    if not files:
        log(f"[跳过] 前贴目录没有视频，直接输出正文：{folder}")
        return None
    rng = random.Random(derive_seed(seed, "intro"))
    source = rng.choice(files)
    item = _probe_one(source)
    if item is None or item.duration <= 0:
        raise RuntimeError(f"前贴无法探测：{source.name}")
    body_info = _video_signature(video)
    if body_info is None or (keep_audio and not body_info["has_audio"]):
        raise RuntimeError("完整正文的视频流或原声音轨不可用")
    # Read the complete intro; use CFR frame quantization without flooring away its final frame.
    segment = {"item": item, "offset": 0.0, "take": item.duration,
               "dedup_seed": derive_seed(seed, "intro", source.name)}
    if stickers:
        segment["sticker"] = str(rng.choice(stickers))
    with tempfile.TemporaryDirectory(prefix="intro_", dir=video.parent) as td:
        folder_tmp = Path(td)
        intro = folder_tmp / "intro.mp4"
        combined = folder_tmp / "combined.mp4"
        if not concat_clips([segment], intro, canvas=canvas,
                            fps=config.MONTAGE_FPS, crf=crf)["ok"]:
            raise RuntimeError("前贴去重/贴纸处理失败")
        intro = _adapt_intro(intro, body_info, folder_tmp / "adapted.mp4", crf)
        check_stop()
        intro_info = _video_signature(intro)
        if intro_info is None:
            raise RuntimeError("前贴视频流不可用")
        duration = intro_info["duration"]
        expected = duration + body_info["duration"]
        concat_list = folder_tmp / "concat.txt"
        paths = []
        for part in (intro, video):
            raw = str(part.resolve())
            if "\n" in raw or "\r" in raw:
                raise ValueError("拼接素材路径不能包含换行")
            escaped = raw.replace("'", "'\\''")
            paths.append(f"file '{escaped}'")
        concat_list.write_text("\n".join(paths) + "\n", encoding="utf-8")
        cmd = _video_copy_cmd(concat_list, video, source, item.has_audio,
                              duration, keep_audio, combined,
                              body_duration=body_info["duration"])
        rc, _, error = run_cmd(cmd, timeout=3600)
        check_stop()
        actual = _video_signature(combined) if not rc else None
        if (actual is None or not combined.is_file() or combined.stat().st_size < 1024
                or abs(actual["duration"] - expected) > 2 / config.MONTAGE_FPS
                or actual["nb_frames"] != intro_info["nb_frames"] + body_info["nb_frames"]
                or actual["has_audio"] != keep_audio):
            raise RuntimeError(f"前贴拼接流/帧数/时长校验失败，正文保持不变：{error.strip()[-500:]}")
        check_stop()
        os.replace(combined, video)
        log("  [前贴] 视频流复制拼接，已完成正文未重新编码")
    return {"source": str(source), "duration": duration,
            "dedup_seed": segment["dedup_seed"], "sticker": segment.get("sticker"),
            "sticker_applied": segment.get("sticker_applied", False)}
