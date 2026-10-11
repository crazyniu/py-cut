"""混剪步骤A：从爆款视频提取「语音节奏 + 时长基准」。

混剪三步（需求见 docs/MONTAGE.md）：
    A. 本模块——量出基准视频的语音段（节奏骨架）与总时长
    B. core/montage_clips.py——按 A 给出的槽位从片段库取画面填满
    C. core/montage_pip.py——把切片以画中画叠在 B 的结果上

本步骤**不做 ASR 转写**：混剪要的是「节奏」不是「文案」——成片沿用基准视频的
断句与语速，画面则完全换成片段库/切片的画面，文字必然对不上，转写没有用处。
引入 whisper 之类的 ASR 会带来模型下载与推理耗时，收益为零，故不做。

silencedetect 的踩坑记录（本机 ffmpeg 9.0.2 实测）：
  - **结果全在 stderr**，`-f null -` 时 stdout 是 0 字节。读 stdout 会得到空结果。
  - 加 `-vn -sn -dn` 跳过视频解码：同一条77s 素材 2.55s -> 0.11s（20倍），
    音频滤镜不需要画面。
  - `silencedetect` 只报**达到 d 阈值的静音段**，不是「有声音的段」。所以语音段
    必须由静音区间取补集得到。
  - 视频以静音结尾时，最后一个 `silence_start` 没有配对的 `silence_end`
    （实测-25dB 下 3 个start 全部配上了，但不能指望），补集时必须补到容器时长，
    否则会漏掉最后一段语音。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG, FFPROBE, probe_scalar
from .dedup import _video_duration
from .utils import log, run_cmd

# ffprobe 解析失败时的兜底帧率（本素材实测 30fps）
FALLBACK_FPS = 30.0

# ffprobe 单次探测超时（秒）
_PROBE_TIMEOUT = 60
# silencedetect 跑一遍的超时，按音频长度估算并封顶（77s 素材实测 0.11s）
_SILENCE_TIMEOUT = 600

# silencedetect 原始行（实测格式）：
#   [Parsed_silencedetect_0 @ 0x...] silence_start: 31.746667
#   [Parsed_silencedetect_0 @ 0x...] silence_end: 32.110272 | silence_duration: 0.363605
# 时间字段的形态本机实测只有「有效数值」一种，不会有 nan/inf，故不做 isfinite 防御。
_RE_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_RE_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")
_RE_SILENCE_DUR = re.compile(r"silence_duration:\s*([0-9.]+)")

# 相邻语音段间隔小于此值就合并，避免切出 0.05s 的碎片槽
_MERGE_GAP = 0.05


@dataclass
class SpeechSegment:
    """一段连续语音（秒，容器时间轴）。"""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """段时长 = end - start。"""
        return self.end - self.start


@dataclass
class SourceProfile:
    """基准视频画像：步骤B/C 的画布、帧率、时长与节奏骨架都从这里取。"""

    path: Path
    duration: float               # 容器总时长
    width: int
    height: int
    fps: float
    has_audio: bool
    segments: list[SpeechSegment] = field(default_factory=list)
    speech_total: float = 0.0

    @property
    def silence_total(self) -> float:
        """总静音时长 = 容器时长 - 语音总时长（下限0）。"""
        return max(0.0, self.duration - self.speech_total)


def _float_or_zero(text: str) -> float:
    """把 ffprobe 的单行输出转float，失败返回 0.0。

    ffprobe 缺字段时 **rc 仍是 0**，stdout 才是 `N/A`（见 dedup._video_duration
    的实测记录），所以只能靠 float() 抛ValueError 来兜。
    """
    try:
        return float((text or "").strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _fps_to_float(text: str) -> float:
    """'30/1' / '30000/1001' / '29.97' -> float；`0/0`、`N/A`、空 -> 0.0。

    必须取**首行**：不带 `-select_streams` 的通用封装（probe_scalar）会同时返回
    视频流与音频流两行，音频流那行是 `0/0`（AGENTS.md 第 5 节 / dedup 实测
    12 个里 10 个如此，有音轨的才有）。直接整串float() 会抛 ValueError。
    """
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
    """从 ffmpeg stderr 里解析 silencedetect 事件。

    返回 (静音起点列表, 静音终点列表, 静音时长列表)。三个列表按出现顺序一一对应；
    尾部静音缺 `silence_end` 时，终点列表比起点列表少一个（调用方按duration 补）。
    """
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
    """静音区间取补集得语音区间。

    语音 = [0, s1_start] + [s1_end, s2_start] + ... + [sn_end, duration]。
    末尾的 `ends` 若缺失（视频以静音收尾），最后一段直接补到 duration——
    否则会漏掉结尾的一段语音。
    """
    total = max(0.0, duration)
    silences: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        silences.append((s, e))
    # 补齐缺尾的静音段，并夹紧到 [0, total]
    silences = [(max(0.0, s), min(total, e)) for s, e in silences if e > s]

    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:                      # 两段静音之间的空隙即语音
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if total > cursor:                      # 最后一段静音之后的语音
        speech.append((cursor, total))
    return speech


def _merge_close(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并间隔 < _MERGE_GAP 的相邻语音段，避免碎片。"""
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
                           seg_max: float | None = None) -> list[SpeechSegment]:
    """用 ffmpeg silencedetect 提取语音段（已按 seg_min 过滤、超 seg_max 截断）。

    参数默认全部取config.MONTAGE_SILENCE_NOISE_DB / SILENCE_MIN_DUR /
    SEG_MIN_DUR / SEG_MAX_DUR。返回值是语音段，不是静音段。
    """
    noise_db = config.MONTAGE_SILENCE_NOISE_DB if noise_db is None else noise_db
    min_silence = config.MONTAGE_SILENCE_MIN_DUR if min_silence is None else min_silence
    seg_min = config.MONTAGE_SEG_MIN_DUR if seg_min is None else seg_min
    seg_max = config.MONTAGE_SEG_MAX_DUR if seg_max is None else seg_max

    duration = _video_duration(Path(path))
    if duration <= 0:
        log(f"[步骤A] {Path(path).name}: 探测不到视频时长，语音段为空")
        return []

    args = [
        FFMPEG, "-hide_banner", "-nostats",
        "-vn", "-sn", "-dn",# 只要音频，别解视频（77s 素材 2.55s -> 0.11s）
        "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    rc, _out, err = run_cmd(args, timeout=_SILENCE_TIMEOUT)
    if rc != 0:
        log(f"[步骤A] silencedetect 失败 rc={rc}: {err.strip().splitlines()[-1] if err.strip() else '无输出'}")
        return []

    # silencedetect 的结果**只在 stderr**，stdout 是空的（见模块 docstring）
    starts, ends, _durs = _parse_silence_events(err)
    spans = _complement(starts, ends, duration)

    # 太短的段丢弃、过长的段保留头部截断
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


def probe_source(path: Path) -> SourceProfile | None:
    """探测基准视频并跑一遍语音检测，返回完整画像；失败返回 None。"""
    path = Path(path)
    if not path.is_file():
        log(f"[步骤A] 基准视频不存在: {path}")
        return None

    duration = _video_duration(path)
    if duration <= 0:
        log(f"[步骤A] {path.name}: 探测不到视频时长")
        return None

    width = int(_float_or_zero(probe_scalar(path, "stream=width")))
    height = int(_float_or_zero(probe_scalar(path, "stream=height")))
    if width <= 0 or height <= 0:
        log(f"[步骤A] {path.name}: 视频流缺失有效分辨率")
        return None
    # 必须走 probe_scalar + 取首行：见 _fps_to_float 注释里的 0/0 多行坑
    fps = _fps_to_float(probe_scalar(path, "stream=avg_frame_rate")) or FALLBACK_FPS

    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        timeout=_PROBE_TIMEOUT)
    has_audio = rc == 0 and out.strip() != ""

    segments = detect_speech_segments(path) if has_audio else []
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
    """无语音段时的退化铺法：按 max_slot 均匀切，末段不足 min_slot 就并入前一段。"""
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
    """把语音段转成「每个片段该取多长」的时间槽（步骤B 的直接输入）。

    以语音段为节奏骨架：一段语音对应一个槽，槽长= 段长，成片节奏因此与原视频
    话术一致（混剪的核心价值）。总时长受 `total_duration × fill_ratio` 约束：
    累加到接近上限后，最后一个槽按剩余量截断，超出的段直接丢弃。

    返回的 (start, end) 是**基准视频里的时间位置**（从原视频哪一秒取画面），
    步骤B 用双seek 从基准视频截取对应片段。保证：每槽长度 >0 且落在
    [min_slot, max_slot]、起点单调递增、总时长 <= 上限。
    """
    fill_ratio = config.MONTAGE_TARGET_FILL_RATIO if fill_ratio is None else fill_ratio
    min_slot = config.MONTAGE_CLIP_MIN_DUR if min_slot is None else min_slot
    max_slot = config.MONTAGE_CLIP_MAX_DUR if max_slot is None else max_slot

    budget = max(0.0, total_duration * fill_ratio)
    if budget < min_slot:
        # 预算本身还不到一个最小槽：无论怎么切都会违反 min_slot 下限，只能不出槽
        log(f"[步骤A] 预算 {budget:.2f}s < 最小槽 {min_slot:.2f}s，无可用时间槽")
        return []

    if not segments:
        log(f"[步骤A] 无语音段，退化为按{max_slot}s 均匀铺满 {budget:.2f}s")
        return _even_fill(budget, min_slot, max_slot)

    # 语音段太短 -> 并入前一段（并入后仍不得超max_slot）；超过上限 -> 截断（保留头部）
    spans: list[list[float]] = []
    for seg in segments:
        s, e = float(seg.start), float(seg.end)
        e = min(e, s + max_slot)
        if e - s < min_slot:
            if spans:
                # 并入前一段：只吃到 max_slot 为止，溢出部分不接
                spans[-1][1] = min(max(spans[-1][1], e), spans[-1][0] + max_slot)
                continue
            e = s + min_slot           # 首段太短：撑到 min_slot
            if e > budget:
                break
        spans.append([s, e])

    slots: list[tuple[float, float]] = []
    acc = 0.0
    for s, e in spans:
        remain = budget - acc
        if remain < min_slot:
            break                # 剩余额度不够一个最小槽，丢弃多余的段
        if e - s > remain:
            e = s + remain        # 最后一个槽按剩余量截断
        slots.append((s, e))
        acc += e - s

    # 兜底：截断后可能不足 min_slot，与前一段合并（绝不返回小于 min_slot 的槽）
    if len(slots) >= 2 and slots[-1][1] - slots[-1][0] < min_slot:
        ps, _ = slots[-2]
        slots[-2] = (ps, slots[-1][1])
        slots.pop()

    total = sum(e - s for s, e in slots)
    log(f"[步骤A] 时间槽 {len(slots)} 个，合计 {total:.2f}s / 上限 {budget:.2f}s"
        f"（{total_duration:.2f}×{fill_ratio}）")
    return slots
