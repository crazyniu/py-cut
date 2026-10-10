"""Opt-in authorized audio mixing after dedup video encoding.

The audio muxer writes to a sibling temporary file, checks the audio/video
streams and durations, and replaces only the caller's staging video. It never
overwrites an existing completed output when FFmpeg or validation fails.
"""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import FFMPEG, FFPROBE, probe_video
from .utils import run_cmd

# Audio packet / codec-priming allowance; no calibrated video values changed.
AUDIO_SYNC_TOLERANCE = 0.30


class AudioMixError(ValueError):
    """Invalid optional audio input or failed output verification."""


@dataclass(frozen=True)
class AudioMixOptions:
    bgm: str = ""
    ambience: str = ""
    voiceover: str = ""
    original_gain: float = 1.0
    bgm_gain: float = 0.25
    ambience_gain: float = 0.30
    voice_gain: float = 1.0
    fade_out: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.bgm or self.ambience or self.voiceover
                    or self.original_gain != 1.0 or self.fade_out > 0.0)

    def snapshot(self) -> dict:
        """Output the effective, reproducible audio-editing contract."""
        return {
            "enabled": self.enabled,
            "source_audio": "replaced" if self.voiceover else "preserved",
            "bgm": self.bgm,
            "ambience": self.ambience,
            "voiceover": self.voiceover,
            "original_gain": self.original_gain,
            "bgm_gain": self.bgm_gain if self.bgm else None,
            "ambience_gain": self.ambience_gain if self.ambience else None,
            "voice_gain": self.voice_gain if self.voiceover else None,
            "fade_out_seconds": self.fade_out,
            "short_track_rule": "loop bgm/ambience, silence-pad voiceover",
        }


def validate_audio_options(options: AudioMixOptions, *, check_only: bool = False) -> None:
    """Reject inconsistent arguments before any encoding can start."""
    gains = {
        "original_gain": options.original_gain,
        "bgm_gain": options.bgm_gain,
        "ambience_gain": options.ambience_gain,
        "voice_gain": options.voice_gain,
    }
    for name, val in gains.items():
        if not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= val <= 2:
            raise AudioMixError(f"{name} 必须在 0～2 之间")
    if (not isinstance(options.fade_out, (int, float))
            or not math.isfinite(options.fade_out)
            or not 0 <= options.fade_out <= 10):
        raise AudioMixError("audio-fade-out 必须在 0～10 秒之间")
    if check_only and options.enabled:
        raise AudioMixError("--check 不能与音频编辑参数合用（--check 只自检）")
    if options.voiceover and options.original_gain != 1.0:
        raise AudioMixError("替换配音不混入原声，请移除 --original-gain")
    for path, label in ((options.bgm, "BGM"), (options.ambience, "环境音"),
                        (options.voiceover, "配音")):
        if path and (not Path(path).expanduser().is_file()
                     or not os.access(Path(path).expanduser(), os.R_OK)):
            raise AudioMixError(f"{label} 不存在或不可读: {path}")
    if options.bgm_gain != 0.25 and not options.bgm:
        raise AudioMixError("--bgm-gain 需要同时指定 --bgm")
    if options.ambience_gain != 0.30 and not options.ambience:
        raise AudioMixError("--ambience-gain 需要同时指定 --ambience")
    if options.voice_gain != 1.0 and not options.voiceover:
        raise AudioMixError("--voice-gain 需要同时指定 --voiceover")


def _probe_audio_streams(path: Path) -> list[dict]:
    """Probe actual audio stream metadata, independent of file extensions."""
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type,channels,channel_layout,duration",
         "-of", "json", str(path)], timeout=60)
    if rc != 0:
        raise AudioMixError(f"无法解析音轨: {path.name}")
    try:
        streams = json.loads(out).get("streams") or []
    except (ValueError, AttributeError) as exc:
        raise AudioMixError(f"音轨探测返回无效数据: {path.name}") from exc
    if not isinstance(streams, list):
        raise AudioMixError(f"音轨探测格式错误: {path.name}")
    return streams


def _require_one_audio(path: Path) -> dict:
    streams = _probe_audio_streams(path)
    if len(streams) != 1:
        raise AudioMixError(f"{path.name} 必须包含且仅包含一条音轨，实际 {len(streams)} 条")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise AudioMixError(f"{path.name} 的音轨元数据无效")
    channels = stream.get("channels")
    if not isinstance(channels, int) or not 1 <= channels <= 8:
        raise AudioMixError(f"{path.name} 的音轨声道数无法识别（需要 1～8 声道）")
    return stream


def preflight_external_audio(options: AudioMixOptions) -> None:
    """Reject unreadable, silent or multi-track extra inputs before processing."""
    validate_audio_options(options)
    if not options.enabled:
        return
    for path in (options.bgm, options.ambience, options.voiceover):
        if path:
            _require_one_audio(Path(path).expanduser())


def build_audio_mux_cmd(video: Path, dest: Path, *,
                        duration: float, video_has_audio: bool,
                        options: AudioMixOptions, abitrate: str = "128k") -> list[str]:
    """Build video stream-copy and one explicitly mapped AAC audio output."""
    validate_audio_options(options)
    if not options.enabled:
        raise AudioMixError("未启用音频处理")
    if not math.isfinite(duration) or duration <= 0:
        raise AudioMixError("视频时长不可用，不能安全同步音轨")
    if not video_has_audio and not any((options.bgm, options.ambience,
                                        options.voiceover)):
        raise AudioMixError("原视频没有音轨，且未提供可替代音频")
    if not isinstance(abitrate, str) or not abitrate.endswith("k") or not abitrate[:-1].isdigit():
        raise AudioMixError("无效 AAC 码率配置")

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(video)]
    sources: list[tuple[int, float]] = []
    if video_has_audio and not options.voiceover:
        sources.append((0, options.original_gain))
    next_index = 1
    for path, gain, kind in (
        (options.bgm, options.bgm_gain, "bgm"),
        (options.ambience, options.ambience_gain, "ambience"),
        (options.voiceover, options.voice_gain, "voiceover"),
    ):
        if not path:
            continue
        if kind in ("bgm", "ambience"):
            # Repeat background tracks rather than shortening the output.
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(Path(path).expanduser())]
        sources.append((next_index, gain))
        next_index += 1
    if not sources:
        raise AudioMixError("无音轨可混合")
    length = f"{duration:.6f}"
    filters = []
    pads = []
    for n, (idx, gain) in enumerate(sources):
        name = f"mx{n}"
        pads.append(f"[{name}]")
        # apad silences voiceover tails; atrim stops even infinite loop inputs.
        filters.append(
            f"[{idx}:a:0]aresample=async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain:.6f},apad,atrim=duration={length},"
            f"asetpts=PTS-STARTPTS[{name}]"
        )
    chain = f"{pads[0]}anull" if len(pads) == 1 else (
        f"{''.join(pads)}amix=inputs={len(pads)}:duration=longest:normalize=0")
    if options.fade_out:
        fade = min(options.fade_out, duration)
        start = max(0.0, duration - fade)
        chain += f",afade=t=out:st={start:.6f}:d={fade:.6f}"
    filters.append(f"{chain},atrim=duration={length},asetpts=PTS-STARTPTS[aout]")
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", abitrate,
        "-t", length, "-movflags", "+faststart", str(dest),
    ]
    return cmd


def mix_audio_file(video: Path, options: AudioMixOptions, *,
                   abitrate: str = "128k", timeout: int = 600) -> dict:
    """Swap only a staging video after a verified temporary audio mux."""
    validate_audio_options(options)
    if not options.enabled:
        return {}
    video = Path(video)
    original = probe_video(video)
    if original is None:
        raise AudioMixError("编码后无法探测视频时长/尺寸，音频混合已停止")
    if original.has_audio and not options.voiceover:
        _require_one_audio(video)
    preflight_external_audio(options)

    mixed = video.with_name(
        f"{video.stem}.audio_tmp{os.getpid()}_{threading.get_ident():x}{video.suffix}"
    )
    try:
        cmd = build_audio_mux_cmd(
            video, mixed, duration=original.duration,
            video_has_audio=original.has_audio, options=options, abitrate=abitrate)
        rc, _stdout, stderr = run_cmd(cmd, timeout=timeout)
        if rc != 0 or not mixed.is_file() or mixed.stat().st_size < 1024:
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else "无有效输出"
            raise AudioMixError(f"音频混合失败 rc={rc}: {detail}")

        out = probe_video(mixed)
        if out is None or not out.has_audio:
            raise AudioMixError("混音结果缺少视频或音轨")
        if (out.width, out.height) != (original.width, original.height):
            raise AudioMixError("混音结果分辨率异常，拒绝覆盖")
        audio = _require_one_audio(mixed)
        try:
            audio_duration = float(audio.get("duration"))
        except (ValueError, TypeError) as exc:
            raise AudioMixError("混音结果无法读取音轨时长，拒绝覆盖") from exc
        if not math.isfinite(audio_duration) or audio_duration <= 0:
            raise AudioMixError("混音结果音轨时长无效，拒绝覆盖")
        if (abs(out.duration - original.duration) > AUDIO_SYNC_TOLERANCE
                or abs(out.duration - audio_duration) > AUDIO_SYNC_TOLERANCE):
            raise AudioMixError(
                f"混音后音画时长不一致：视频 {out.duration:.3f}s / "
                f"音频 {audio_duration:.3f}s（期望视频 {original.duration:.3f}s）"
            )
        os.replace(mixed, video)
        return {
            "video_duration": round(float(out.duration), 3),
            "audio_duration": round(audio_duration, 3),
            "channels": audio["channels"], "tracks": 1,
            "video_codec": "copy", "audio_codec": "aac",
        }
    finally:
        try:
            mixed.unlink()
        except FileNotFoundError:
            pass
