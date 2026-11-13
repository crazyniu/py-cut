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
from .runtime import check_stop
from .utils import log, run_cmd
from .ab_audio import RMS_SILENCE, measure_rms, phase_filter, select_source, too_quiet

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
    voice_shift: float = 0.0
    ab_mode: str = "off"
    ab_sources: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.bgm or self.ambience or self.voiceover
                    or self.original_gain != 1.0 or self.fade_out > 0.0
                    or self.voice_shift != 0.0 or self.ab_mode != "off")

    def snapshot(self) -> dict:
        """Output the effective, reproducible audio-editing contract."""
        result = {
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
        if self.voice_shift:
            result["voice_shift_semitones"] = self.voice_shift
        if self.ab_mode != "off":
            result["ab_mode"] = self.ab_mode
        return result


def validate_audio_options(options: AudioMixOptions, *, check_only: bool = False) -> None:
    """Reject inconsistent arguments before any encoding can start."""
    if not -1.5 <= options.voice_shift <= 1.5:
        raise AudioMixError("voice-shift 必须在 -1.5～1.5 半音之间")
    if options.ab_mode not in ("off", "hidden", "mislead"):
        raise AudioMixError("未知 AB 音频模式")
    if options.ab_mode == "mislead" and not options.ab_sources:
        raise AudioMixError("声纹扰频需要 AB 音频素材")
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
                        options: AudioMixOptions, abitrate: str = "128k",
                        ab_source: Path | None = None, ab_gain: float = 0.0,
                        ab_compatible: bool = False) -> list[str]:
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
    if ab_source is not None:
        cmd += ["-stream_loop", "-1", "-i", str(ab_source)]
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
    if options.voice_shift:
        ratio = 2 ** (options.voice_shift / 12)
        low_gain = 1.5 if options.voice_shift < 0 else -1.5
        chain += (f",asetrate=48000*{ratio:.8f},aresample=48000,atempo={1 / ratio:.8f},"
                  f"equalizer=f=300:t=q:w=0.8:g={low_gain},"
                  f"equalizer=f=3000:t=q:w=0.8:g={-low_gain}")
    if options.ab_mode != "off":
        chain += "," + phase_filter(ab_compatible)
    if ab_source is not None:
        filters.append(chain + "[abmain]")
        filters.append(
            f"[{next_index}:a:0]aresample=async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={ab_gain:.8f},atrim=duration={length},asetpts=PTS-STARTPTS[abextra]")
        chain = "[abmain][abextra]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
    if options.voice_shift or options.ab_mode != "off":
        chain += ",alimiter=limit=0.95:level=false:latency=true"
    if options.fade_out:
        fade = min(options.fade_out, duration)
        start = max(0.0, duration - fade)
        chain += f",afade=t=out:st={start:.6f}:d={fade:.6f}"
    filters.append(f"{chain},apad,atrim=duration={length},asetpts=PTS-STARTPTS[aout]")
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", abitrate,
        "-t", length, "-movflags", "+faststart", str(dest),
    ]
    return cmd


def mix_audio_file(video: Path, options: AudioMixOptions, *,
                   abitrate: str = "128k", timeout: int = 600, seed: int = 0) -> dict:
    """Swap only a staging video after a verified temporary audio mux."""
    validate_audio_options(options)
    if not options.enabled:
        return {}
    video = Path(video)
    original = probe_video(video)
    if original is None:
        raise AudioMixError("编码后无法探测视频时长/尺寸，音频混合已停止")
    if not original.has_audio and not any((options.bgm, options.ambience, options.voiceover)):
        if options.ab_mode != "off" or options.voice_shift:
            log("    [音频跳过] 原视频没有音轨")
            return {"skipped": "no_source_audio"}
    if original.has_audio and not options.voiceover:
        _require_one_audio(video)
    preflight_external_audio(options)

    ab_source, ab_gain, source_rms = None, 0.0, 0.0
    ab_details = {}
    if options.ab_mode != "off":
        reference = Path(options.voiceover).expanduser() if options.voiceover else video
        source_rms = measure_rms(reference, original.duration, timeout)
        if options.ab_mode == "mislead":
            ab_source, ratio = select_source(options.ab_sources, seed)
            _require_one_audio(ab_source)
            background_rms = measure_rms(ab_source, original.duration, timeout, loop=True)
            if background_rms <= RMS_SILENCE:
                raise AudioMixError(f"AB 音频素材为静音: {ab_source.name}")
            ab_gain = min(4.0, max(0.0001, source_rms * ratio / background_rms))
            ab_details.update(source=str(ab_source), target_rms_ratio=ratio, gain=ab_gain)

    mixed = video.with_name(
        f"{video.stem}.audio_tmp{os.getpid()}_{threading.get_ident():x}{video.suffix}"
    )
    try:
        for compatible in (False, True):
            check_stop()
            cmd = build_audio_mux_cmd(
                video, mixed, duration=original.duration,
                video_has_audio=original.has_audio, options=options, abitrate=abitrate,
                ab_source=ab_source, ab_gain=ab_gain, ab_compatible=compatible)
            rc, _stdout, stderr = run_cmd(cmd, timeout=timeout)
            check_stop()
            if rc != 0 or not mixed.is_file() or mixed.stat().st_size < 1024:
                detail = stderr.strip().splitlines()[-1] if stderr.strip() else "无有效输出"
                raise AudioMixError(f"音频混合失败 rc={rc}: {detail}")
            if options.ab_mode == "off":
                break
            output_rms = measure_rms(mixed, original.duration, timeout)
            if not too_quiet(output_rms, source_rms):
                ab_details.update(compatible=compatible, source_rms=source_rms, output_rms=output_rms)
                break
            if compatible:
                raise AudioMixError("AB 音频兼容处理后仍接近静音")
            log("    [AB 音频] 输出音量过低，改用兼容声道")

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
        check_stop()
        os.replace(mixed, video)
        result = {
            "video_duration": round(float(out.duration), 3),
            "audio_duration": round(audio_duration, 3),
            "channels": audio["channels"], "tracks": 1,
            "video_codec": "copy", "audio_codec": "aac",
        }
        if ab_details:
            result["ab_audio"] = ab_details
        return result
    finally:
        try:
            mixed.unlink()
        except FileNotFoundError:
            pass
