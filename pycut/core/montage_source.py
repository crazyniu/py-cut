"""Reference video profiling: duration, format, and speech rhythm (no ASR).

silencedetect reports silence on stderr. Skip video decoding and complement the
silence intervals to obtain speech. Close an unmatched trailing silence at the
reference duration. Background shots come from the clip library, not this video."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG
from .dedup import _video_duration
from .utils import log, run_cmd
from .montage_clips import _probe_one


FALLBACK_FPS = 30.0


_SILENCE_TIMEOUT = 600


#   [Parsed_silencedetect_0 @ 0x...] silence_start: 31.746667
#   [Parsed_silencedetect_0 @ 0x...] silence_end: 32.110272 | silence_duration: 0.363605
_RE_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_RE_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")
_RE_SILENCE_DUR = re.compile(r"silence_duration:\s*([0-9.]+)")

# Merge tiny speech gaps to avoid flash-length slots.
_MERGE_GAP = 0.05


@dataclass
class SpeechSegment:
    """Continuous speech interval in seconds on the reference timeline."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """Return interval length in seconds."""
        return self.end - self.start


@dataclass
class SourceProfile:
    """Reference metadata and speech rhythm; montage output uses its configured canvas."""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    segments: list[SpeechSegment] = field(default_factory=list)
    speech_total: float = 0.0

    @property
    def silence_total(self) -> float:
        """Return nonnegative duration not classified as speech."""
        return max(0.0, self.duration - self.speech_total)


def _float_or_zero(text: str) -> float:
    """Parse the first probe line; missing fields such as N/A return zero."""
    try:
        return float((text or "").strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _fps_to_float(text: str) -> float:
    """Parse a rate or fraction from the first probe line; invalid or zero denominators return zero.

    Unselected probes can append an audio 0/0 line after the video frame rate."""
    v = (text or "").strip().splitlines()
    v = v[0].strip() if v else ""
    if not v or v in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in v:
            a, b = v.split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_silence_events(text: str) -> tuple[list[float], list[float], list[float]]:
    """Parse ordered (starts, ends, durations) from silencedetect stderr.

    Trailing silence can have a start without a corresponding end."""
    starts: list[float] = []
    ends: list[float] = []
    durs: list[float] = []
    for line in (text or "").splitlines():
        if "silence_" not in line:
            continue
        m_end = _RE_SILENCE_END.search(line)
        if m_end:
            ends.append(float(m_end.group(1)))
            m_dur = _RE_SILENCE_DUR.search(line)
            durs.append(float(m_dur.group(1)) if m_dur else 0.0)
            continue
        m_start = _RE_SILENCE_START.search(line)
        if m_start:
            starts.append(float(m_start.group(1)))
    return starts, ends, durs


def _complement(starts: list[float], ends: list[float], duration: float) -> list[tuple[float, float]]:
    """Return speech as the complement of silence intervals within [0, duration].

    Close a missing trailing silence end at duration."""
    total = max(0.0, duration)
    silences: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        silences.append((s, e))
    # Close unmatched trailing silence and clamp intervals to the reference duration.
    silences = [(max(0.0, s), min(total, e)) for s, e in silences if e > s]

    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if total > cursor:
        speech.append((cursor, total))
    return speech


def _merge_close(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge adjacent speech intervals separated by less than _MERGE_GAP."""
    out: list[tuple[float, float]] = []
    for s, e in spans:
        if out and s - out[-1][1] < _MERGE_GAP:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def detect_speech_segments(path: Path, noise_db: float | None = None,
                           min_silence: float | None = None,
                           seg_min: float | None = None,
                           seg_max: float | None = None, *,
                           duration: float | None = None) -> list[SpeechSegment]:
    """Detect speech using configured silence thresholds; drop short and cap long intervals.

    Return speech intervals, not the silence events emitted by FFmpeg."""
    noise_db = config.MONTAGE_SILENCE_NOISE_DB if noise_db is None else noise_db
    min_silence = config.MONTAGE_SILENCE_MIN_DUR if min_silence is None else min_silence
    seg_min = config.MONTAGE_SEG_MIN_DUR if seg_min is None else seg_min
    seg_max = config.MONTAGE_SEG_MAX_DUR if seg_max is None else seg_max

    duration = _video_duration(Path(path)) if duration is None else duration
    if duration <= 0:
        log(f"[步骤A] {Path(path).name}: 探测不到视频时长，语音段为空")
        return []

    args = [
        FFMPEG, "-hide_banner", "-nostats",
        "-vn", "-sn", "-dn",# Audio analysis must not decode the video stream.
        "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    rc, _out, err = run_cmd(args, timeout=_SILENCE_TIMEOUT)
    if rc != 0:
        log(f"[步骤A] silencedetect 失败 rc={rc}: {err.strip().splitlines()[-1] if err.strip() else '无输出'}")
        return []

    # silencedetect writes events to stderr, not stdout.
    starts, ends, _durs = _parse_silence_events(err)
    spans = _complement(starts, ends, duration)

    kept: list[tuple[float, float]] = []
    for s, e in spans:
        if e - s < seg_min:
            continue
        kept.append((s, min(e, s + seg_max)))
    kept = _merge_close(kept)

    segs = [SpeechSegment(start=round(s, 3), end=round(e, 3)) for s, e in kept]
    log(f"[步骤A] {Path(path).name}: 静音 {len(starts)} 段 -> 语音 {len(segs)} 段"
        f"（noise={noise_db}dB d={min_silence}s seg[{seg_min},{seg_max}]）")
    return segs


def probe_source(path: Path, *, noise_db: float | None = None,
                 min_silence: float | None = None) -> SourceProfile | None:
    """Return cached reference metadata plus speech analysis, or None on probe failure."""
    path = Path(path)
    if not path.is_file():
        log(f"[步骤A] 基准视频不存在: {path}")
        return None

    # Share one file-identity-validated JSON metadata query with background/PIP/subtitle paths.
    details: dict = {}
    info = _probe_one(path, details=details)
    if info is None:
        log(f"[步骤A] {path.name}: 探测不到有效视频信息")
        return None
    # Body timing uses the video stream, not a potentially longer audio/container duration.
    duration = _float_or_zero(str(details.get("duration") or ""))
    if duration <= 0:
        log(f"[步骤A] {path.name}: 探测不到视频时长")
        return None
    width, height = info.width, info.height
    fps = _fps_to_float(str(details.get("avg_frame_rate") or "")) or FALLBACK_FPS
    has_audio = info.has_audio
    segments = (detect_speech_segments(path, noise_db=noise_db, min_silence=min_silence,
                                       duration=duration) if has_audio else [])
    speech_total = sum(s.duration for s in segments)

    profile = SourceProfile(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=has_audio,
        segments=segments,
        speech_total=round(speech_total, 3),
    )
    log(f"[步骤A] {path.name}: {duration:.2f}s {width}x{height} @{fps:g}fps "
        f"音轨={'有' if has_audio else '无'} 语音 {len(segments)} 段 "
        f"合计 {speech_total:.2f}s / 静音 {profile.silence_total:.2f}s")
    return profile


def _even_fill(total: float, min_slot: float, max_slot: float) -> list[tuple[float, float]]:
    """Fill without speech using bounded slots; merge a short tail into its predecessor."""
    slots: list[tuple[float, float]] = []
    cursor = 0.0
    while total - cursor > 1e-6:
        dur = min(max_slot, total - cursor)
        slots.append((cursor, cursor + dur))
        cursor += dur
    if len(slots) >= 2 and slots[-1][1] - slots[-1][0] < min_slot:
        s, _ = slots[-2]
        slots[-2] = (s, slots[-1][1])
        slots.pop()
    return slots


def plan_clip_slots(segments: list[SpeechSegment], total_duration: float,
                    fill_ratio: float | None = None,
                    min_slot: float | None = None,
                    max_slot: float | None = None
                    ) -> list[tuple[float, float]]:
    """Convert speech intervals into duration slots for background selection.

    Bound each slot by min_slot/max_slot and the total_duration * fill_ratio budget.
    The planner consumes interval lengths; these positions do not select reference pictures."""
    fill_ratio = config.MONTAGE_TARGET_FILL_RATIO if fill_ratio is None else fill_ratio
    min_slot = config.MONTAGE_CLIP_MIN_DUR if min_slot is None else min_slot
    max_slot = config.MONTAGE_CLIP_MAX_DUR if max_slot is None else max_slot

    budget = max(0.0, total_duration * fill_ratio)
    if budget < min_slot:
        # A budget smaller than min_slot cannot produce a valid slot.
        log(f"[步骤A] 预算 {budget:.2f}s < 最小槽 {min_slot:.2f}s，无可用时间槽")
        return []

    if not segments:
        log(f"[步骤A] 无语音段，退化为按{max_slot}s 均匀铺满 {budget:.2f}s")
        return _even_fill(budget, min_slot, max_slot)

    # Merge short speech into the previous slot without exceeding max_slot; cap long speech.
    spans: list[list[float]] = []
    for seg in segments:
        s, e = float(seg.start), float(seg.end)
        e = min(e, s + max_slot)
        if e - s < min_slot:
            if spans:
                spans[-1][1] = min(max(spans[-1][1], e), spans[-1][0] + max_slot)
                continue
            e = s + min_slot
            if e > budget:
                break
        spans.append([s, e])

    slots: list[tuple[float, float]] = []
    acc = 0.0
    for s, e in spans:
        remain = budget - acc
        if remain < min_slot:
            break
        if e - s > remain:
            e = s + remain
        slots.append((s, e))
        acc += e - s

    # Merge a short remainder without returning a sub-minimum slot.
    if len(slots) >= 2 and slots[-1][1] - slots[-1][0] < min_slot:
        ps, _ = slots[-2]
        slots[-2] = (ps, slots[-1][1])
        slots.pop()

    total = sum(e - s for s, e in slots)
    log(f"[步骤A] 时间槽 {len(slots)} 个，合计 {total:.2f}s / 上限 {budget:.2f}s"
        f"（{total_duration:.2f}×{fill_ratio}）")
    return slots
