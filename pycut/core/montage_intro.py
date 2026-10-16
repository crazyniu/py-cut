"""前贴只在最后拼到完整正文之前；兼容时直接复制两段视频流。"""
from __future__ import annotations

import json
import math
import os
import random
import tempfile
from pathlib import Path

from .. import config
from .dedup_presets import derive_seed
from .montage_clips import _probe_one, concat_clips
from .scan import list_videos_recursive
from .utils import log, run_cmd


def _video_signature(path: Path) -> tuple | None:
    """仅在 H.264 配置、时间基和 SPS/PPS 都一致时尝试无损流复制。"""
    fields = (
        "codec_name", "profile", "level", "width", "height", "pix_fmt",
        "sample_aspect_ratio", "r_frame_rate", "time_base", "extradata_hash",
    )
    cmd = [
        config.FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=" + ",".join(fields),
        "-show_data_hash", "sha256", "-of", "json", str(path),
    ]
    rc, output, _ = run_cmd(cmd, timeout=60)
    if rc:
        return None
    try:
        stream = json.loads(output)["streams"][0]
        if stream.get("codec_name") != "h264" or any(key not in stream for key in fields):
            return None
        return tuple(stream[key] for key in fields)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _video_copy_cmd(concat_file: Path, body: Path, intro_source: Path,
                    has_intro_audio: bool, duration: float,
                    keep_audio: bool, output: Path) -> list[str]:
    """视频只读一次后 stream-copy；若需原声，独立合成 AAC 音轨。"""
    cmd = [
        config.FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
    ]
    if keep_audio:
        cmd.extend(["-i", str(body)])
        if has_intro_audio:
            cmd.extend(["-i", str(intro_source)])
        else:
            cmd.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        graph = (
            f"[2:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a0];"
            f"[1:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]"
        )
        cmd.extend([
            "-filter_complex", graph, "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        ])
    else:
        cmd.extend(["-map", "0:v:0", "-an", "-c:v", "copy"])
    return [*cmd, "-movflags", "+faststart", str(output)]


def _legacy_concat_cmd(intro: Path, body: Path, source: Path,
                       has_intro_audio: bool, duration: float,
                       keep_audio: bool, crf: str, output: Path) -> list[str]:
    """规格不兼容时保留原来的一次完整视频编码，且从不修改原正文。"""
    cmd = [
        config.FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(intro), "-i", str(body), "-i", str(source),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
    ]
    graph = ("[0:v]setsar=1,setpts=PTS-STARTPTS[v0];"
             "[1:v]setsar=1,setpts=PTS-STARTPTS[v1];")
    if keep_audio:
        first = "2:a:0" if has_intro_audio else "3:a:0"
        graph += (
            f"[{first}]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a0];"
            "[1:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
    else:
        graph += "[v0][v1]concat=n=2:v=1:a=0[v]"
    cmd.extend(["-filter_complex", graph, "-map", "[v]"])
    if keep_audio:
        cmd.extend(["-map", "[a]", "-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    return [
        *cmd, "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", str(config.MONTAGE_FPS),
        "-movflags", "+faststart", str(output),
    ]


def prepend(video: Path, folder: Path, *, seed: int, canvas: tuple[int, int],
            stickers: list[Path], crf: str, keep_audio: bool) -> dict | None:
    files = list_videos_recursive(folder, max_depth=None, raise_on_error=True)
    if not files:
        log(f"[跳过] 前贴目录没有视频，直接输出正文：{folder}")
        return None
    rng = random.Random(derive_seed(seed, "intro"))
    source = rng.choice(files)
    item = _probe_one(source)
    if item is None or item.duration <= 0:
        raise RuntimeError(f"前贴无法探测：{source.name}")

    # 原文件时长不是整帧时向下对齐；不可为了凑帧越过素材终点或克隆末帧。
    duration = math.floor(item.duration * config.MONTAGE_FPS) / config.MONTAGE_FPS
    if duration <= 0:
        raise RuntimeError(f"前贴没有完整的视频帧：{source.name}")
    segment = {
        "item": item, "offset": 0.0, "take": duration,
        "dedup_seed": derive_seed(seed, "intro", source.name),
    }
    if stickers:
        segment["sticker"] = str(rng.choice(stickers))

    from .dedup import _video_duration

    with tempfile.TemporaryDirectory(prefix="intro_", dir=video.parent) as td:
        folder_tmp = Path(td)
        intro = folder_tmp / "intro.mp4"
        combined = folder_tmp / "combined.mp4"
        if not concat_clips([segment], intro, canvas=canvas,
                            fps=config.MONTAGE_FPS, crf=crf)["ok"]:
            raise RuntimeError("前贴去重/贴纸处理失败")
        # 用实际完成编码的视频流时长计算拼接，不用源文件音轨或容器的总时长。
        duration = _video_duration(intro)
        body_duration = _video_duration(video)
        if duration <= 0 or body_duration <= 0:
            raise RuntimeError("前贴或正文没有有效视频时长")
        expected = duration + body_duration
        tolerance = 2 / config.MONTAGE_FPS

        signature = _video_signature(intro)
        if signature is not None and signature == _video_signature(video):
            concat_list = folder_tmp / "concat.txt"
            paths = []
            for part in (intro, video):
                escaped = str(part.resolve()).replace("'", "'\\''")
                paths.append(f"file '{escaped}'")
            concat_list.write_text("\n".join(paths) + "\n", encoding="utf-8")
            cmd = _video_copy_cmd(concat_list, video, source, item.has_audio,
                                  duration, keep_audio, combined)
            rc, _, error = run_cmd(cmd, timeout=3600)
            if rc == 0 and combined.is_file() and combined.stat().st_size >= 1024:
                actual = _video_duration(combined)
                if abs(actual - expected) <= tolerance:
                    os.replace(combined, video)
                    log("  [前贴] 兼容 H.264 无损拼接，正文视频流未重新编码")
                    return {
                        "source": str(source), "duration": duration,
                        "dedup_seed": segment["dedup_seed"],
                        "sticker": segment.get("sticker"),
                        "sticker_applied": segment.get("sticker_applied", False),
                    }
            log(f"  [前贴] 流复制不可用，回退兼容编码：{error.strip()[-240:] if error else '时长/产物不匹配'}")
            combined.unlink(missing_ok=True)

        cmd = _legacy_concat_cmd(intro, video, source, item.has_audio,
                                 duration, keep_audio, crf, combined)
        rc, _, error = run_cmd(cmd, timeout=3600)
        if rc or not combined.is_file() or combined.stat().st_size < 1024:
            raise RuntimeError(f"前贴与正文拼接失败: {error.strip()[-500:]}")
        actual = _video_duration(combined)
        if actual <= 0 or abs(actual - expected) > tolerance:
            raise RuntimeError("前贴拼接后时长异常")
        os.replace(combined, video)

    return {
        "source": str(source), "duration": duration,
        "dedup_seed": segment["dedup_seed"],
        "sticker": segment.get("sticker"),
        "sticker_applied": segment.get("sticker_applied", False),
    }
