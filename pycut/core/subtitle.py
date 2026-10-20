"""Locate subtitles and apply one full-width rectangular Gaussian blur.

Preserve audio, write through a temporary file, and persist completion only after
verification. Uncertain detection requires review rather than a no-subtitle marker."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cv2

from .desktop_progress import encoding_progress, activity
from .. import config
from .ffmpeg import FFMPEG, probe_scalar
from .llm import LLMClient
from .runtime import STOP
from .utils import log
from .frame_sampling import FrameExtractionError, grab_frames

SAMPLES = 5
DETECT_WIDTH = 480
PAD_Y = 0.12
DETECT_TIMEOUT = 30.0   # One shared detection deadline per round, not a separate long wait per frame.


class SubtitleReviewNeeded(RuntimeError):
    """Detection is uncertain; retain review status rather than reporting an encoding failure."""


WORKERS = 4

# Read current config buckets at call time; internal callers may override paths.
DETECT_PROMPT = (
    "这些是竖屏短视频的采样帧。逐张检测画面中人为叠加的文字层"
    "(包括大号艺术标题、对白字幕、警示条、免责声明行等后期叠加的文字, "
    "不包括背景招牌、商品包装上的文字)。"
    "若有, 返回能同时覆盖画面中【所有】叠加文字的最小边界框"
    "(有多处文字时取整体并集框), 归一化坐标 0-1, 格式 [x1,y1,x2,y2], y 向下为正。"
)


def _grab_frame(clip: Path, t: float, jp: Path, width: int = DETECT_WIDTH, *, timeout: float = 60) -> bool:
    """Extract a detection frame using input seek; detection does not require frame accuracy."""
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(clip),
         "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(jp)],
        capture_output=True, timeout=timeout)
    return r.returncode == 0 and jp.exists()


def detect_region(llm: LLMClient, clip: Path,
                  samples: int = SAMPLES) -> tuple[int, int, int, int, list[str]] | None:
    """Return a subtitle rectangle or confirmed absence (None); raise on uncertain detection.

    Extraction/model failures must not persist a no-subtitle marker and suppress retries."""
    if samples < 1:
        raise RuntimeError("至少需要抽取一帧进行字幕检测")
    # Reuse cached metadata; fall back to scalar probes for unavailable/invalid metadata.
    from .montage_clips import _probe_one
    info = _probe_one(clip) if Path(clip).is_file() else None
    if info is not None:
        dur, W, H = info.duration, info.width, info.height
    else:
        dur_s = probe_scalar(clip, "format=duration")
        if not dur_s:
            raise RuntimeError("无法探测视频时长")
        dur = float(dur_s)
        if dur <= 0:
            raise RuntimeError("视频时长无效")
        w_s = probe_scalar(clip, "stream=width")
        h_s = probe_scalar(clip, "stream=height")
        if not w_s or not h_s:
            raise RuntimeError("无法探测视频分辨率")
        W, H = int(w_s), int(h_s)

    deadline = time.monotonic() + DETECT_TIMEOUT
    boxes: list[list[float]] = []
    texts: list[str] = []
    clean_replies = 0
    uncertain_positive = 0
    frames = []
    if STOP.flag:
        raise RuntimeError("用户中断字幕检测")
    if not getattr(llm, "enabled", True) and not getattr(llm, "_delegate", None):
        raise SubtitleReviewNeeded("识别服务暂不可用")
    times = [dur * (i + 0.5) / samples for i in range(samples)]
    with tempfile.TemporaryDirectory(prefix="subchk_") as td:
        try:
            paths = grab_frames(clip, times, Path(td), width=DETECT_WIDTH,
                                timeout=max(0.01, deadline - time.monotonic()), ffmpeg=FFMPEG)
        except FrameExtractionError as exc:
            raise SubtitleReviewNeeded(str(exc)) from exc
        if len(paths) != samples:
            raise SubtitleReviewNeeded("采样帧提取不完整")
        for jp in paths:
            frame = cv2.imread(str(jp))
            if frame is None:
                raise SubtitleReviewNeeded("采样帧读取失败")
            frames.append(frame)
    if STOP.flag:
        raise RuntimeError("用户中断字幕检测")
    if time.monotonic() >= deadline:
        raise SubtitleReviewNeeded("识别等待超时")
    prompt = (DETECT_PROMPT + f"\n这里有 {samples} 张按时间顺序排列的独立图片。"
              "每张图片的坐标以该图片为准，不要拼接坐标。逐张检测，"
              '只返回 {"frames":[{"frame":0,"has_subtitle":true或false,'
              '"box":[x1,y1,x2,y2]或null,"text":"字幕原文"},...]}'
              f"。frame 从0到{samples - 1}，每张必须恰好一项。")
    result = llm.ask_vision(frames, prompt, deadline=deadline)
    if STOP.flag:
        raise RuntimeError("用户中断字幕检测")
    replies = result.get("frames") if isinstance(result, dict) else None
    if (not isinstance(replies, list) or len(replies) != samples
            or any(not isinstance(r, dict) or type(r.get("frame")) is not int for r in replies)
            or {r["frame"] for r in replies} != set(range(samples))):
        raise SubtitleReviewNeeded("多帧识别返回不完整或不支持多图请求")
    for r in replies:
        if r.get("has_subtitle") is False:
            clean_replies += 1
            continue
        if r.get("has_subtitle") is not True:
            raise SubtitleReviewNeeded("多帧识别缺少明确字幕判断")
        box = r.get("box")
        if not isinstance(box, list) or len(box) != 4:
            uncertain_positive += 1
            continue
        try:
            b = [float(v) for v in box]
        except (TypeError, ValueError):
            uncertain_positive += 1
            continue
        if all(0.0 <= v <= 1.0 for v in b) and b[2] > b[0] and b[3] > b[1]:
            boxes.append(b)
            if r.get("text"):
                texts.append(str(r["text"]))
        else:
            uncertain_positive += 1

    if not boxes:
        # Persist absence only after a majority of explicit negatives without invalid-box conflicts.
        if clean_replies >= (samples // 2 + 1) and uncertain_positive == 0:
            return None
        raise SubtitleReviewNeeded(
            f"字幕检测结果不可靠：有效无字幕答复 {clean_replies}/{samples}，"
            f"无法定位的字幕答复 {uncertain_positive}；未标记已处理"
        )

    # Moving/two-line subtitles must fit every sampled bounding box.
    x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)

    # Cover the full video width while following detected subtitle height.
    pad = PAD_Y * (y2 - y1)
    y1 = max(0.0, y1 - pad)
    y2 = min(1.0, y2 + pad)

    # Convert normalized coordinates to pixels and align dimensions for H.264.
    X, Y = 0, int(y1 * H)
    BW, BH = W, int((y2 - y1) * H)
    X -= X % 2
    Y -= Y % 2
    BW += BW % 2
    BH += BH % 2
    BW = min(BW, W - X)
    BH = min(BH, H - Y)
    if BW < 8 or BH < 8:
        raise SubtitleReviewNeeded("字幕区域过小，无法可靠遮盖；未标记无字幕")
    return X, Y, BW, BH, texts


def cover_clip(clip: Path, region: tuple[int, int, int, int]) -> bool:
    """Apply a single full-width Gaussian blur inside FFmpeg without round-tripping full frames."""
    import os
    import time
    from .montage_clips import _probe_one

    if STOP.flag:
        return False
    info = _probe_one(clip)
    if info is None:
        log(f"  [遮盖失败] {clip.name}: 无法探测视频")
        return False
    _, y, _, height = region
    top, bottom = max(0, y), min(info.height, y + height)
    if bottom <= top:
        log(f"  [遮盖失败] {clip.name}: 字幕区域高度无效")
        return False
    height = bottom - top
    sigma = min(64, max(16, height * 0.45))
    filters = (f"[0:v:0]split[base][sub];"
               f"[sub]crop={info.width}:{height}:0:{top}:exact=1,"
               f"gblur=sigma={sigma:.2f}:steps=2[mask];"
               f"[base][mask]overlay=0:{top}:shortest=1[v]")
    tmp = clip.with_name(clip.stem + ".tmp_cover.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(clip),
           "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", config.SLICE_ENCODER, "-b:v", "8M", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    proc = None
    try:
        with tempfile.TemporaryFile() as errors, encoding_progress(cmd, info.duration) as measured_cmd:
            proc = subprocess.Popen(measured_cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=errors)
            deadline = time.monotonic() + max(300, info.duration * 20)
            while proc.poll() is None:
                if STOP.flag or time.monotonic() >= deadline:
                    proc.terminate()
                    return False
                try:
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if proc.returncode or not tmp.is_file() or tmp.stat().st_size < 1024:
                errors.seek(0)
                detail = errors.read().decode("utf-8", "replace").strip().splitlines()
                log(f"  [遮盖失败] {clip.name}: FFmpeg rc={proc.returncode} "
                    + (detail[-1] if detail else "无有效输出"))
                return False
        activity("检查中")
        output = _probe_one(tmp)
        if (STOP.flag or output is None or (output.width, output.height) != (info.width, info.height)
                or abs(output.duration - info.duration) > 0.15
                or output.has_audio != info.has_audio):
            log(f"  [遮盖失败] {clip.name}: 成片尺寸、时长或音轨不一致")
            return False
        os.replace(tmp, clip)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  [遮盖失败] {clip.name}: {type(exc).__name__}")
        return False
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        tmp.unlink(missing_ok=True)


def _marker(clip: Path) -> Path:
    return clip.with_name(clip.name + ".covered.json")


def verified_clip(clip: Path) -> bool:
    """Legacy markers only prove encoding, not subtitle removal."""
    try:
        record = json.loads(_marker(clip).read_text(encoding="utf-8"))
        return (isinstance(record, dict) and record.get("version") == 2
                and (record.get("subtitle") is False or
                     (record.get("verified") is True and record.get("effect") == "frosted")))
    except (OSError, ValueError):
        return False


def process_clip(llm: LLMClient, clip: Path, *, force: bool = False,
                 dry_run: bool = False, samples: int = SAMPLES) -> str:
    """Return covered, nosub, skip, review, fail, or dryrun for one clip."""
    mk = _marker(clip)
    if verified_clip(clip) and not force:
        return "skip"

    if not dry_run:
        mk.unlink(missing_ok=True)
    try:
        activity("识别中")
        region = detect_region(llm, clip, samples)
    except (SubtitleReviewNeeded, TimeoutError, subprocess.TimeoutExpired) as exc:
        log(f"  [待复查] {clip.name}: {exc}；继续处理后续片段")
        return "review"
    except (RuntimeError, ValueError, OSError) as exc:
        log(f"  [字幕检测失败] {clip.name}: {exc}")
        return "fail"
    if region is None:
        if not llm.enabled:
            return "review"  # Unavailable vision requires review; never apply an unlocated blur.
        if dry_run:
            log(f"  [无字幕] {clip.name}")
            return "nosub"
        mk.write_text(json.dumps({"version": 2, "subtitle": False,
                                  "checked_at": datetime.now().isoformat()},
                                 ensure_ascii=False), encoding="utf-8")
        return "nosub"

    X, Y, BW, BH, texts = region
    if dry_run:
        log(f"  [区域] {clip.name}: X={X} Y={Y} W={BW} H={BH} | "
            f"{' / '.join(dict.fromkeys(texts))[:60]}")
        return "dryrun"
    if not cover_clip(clip, (X, Y, BW, BH)):
        return "fail"
    mk.unlink(missing_ok=True)
    try:
        activity("检查中")
        residual = detect_region(llm, clip, min(samples, 3))
    except (RuntimeError, ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        log(f"  [待复查] {clip.name}: 复检不确定（{exc}）；继续处理")
        return "review"
    if residual is not None:
        log(f"  [待复查] {clip.name}: 擦除后可能仍有字幕；继续处理，未标记擦除成功")
        return "review"
    mk.write_text(json.dumps({"version": 2, "subtitle": True, "verified": True, "effect": "frosted", "verified_samples": min(samples, 3), "region": [X, Y, BW, BH],
                              "texts": texts,
                              "covered_at": datetime.now().isoformat()},
                             ensure_ascii=False), encoding="utf-8")
    log(f"  [擦除复检通过] {clip.name}: 区域({X},{Y},{BW}x{BH}) "
        f"{' / '.join(dict.fromkeys(texts))[:50]}")
    return "covered"


def collect_clips(paths: list[Path] | None = None) -> list[Path]:
    """Collect explicit paths first; otherwise scan the configured face/no-face buckets."""
    if paths:
        out = []
        for p in paths:
            p = Path(p).expanduser()
            if p.exists():
                out.append(p)
            else:
                log(f"[不存在] {p}")
        return out
    out: list[Path] = []
    for bucket in (config.OUT_FACE, config.OUT_NOFACE):
        if not bucket.is_dir():
            continue
        out += sorted(bucket.glob("*.mp4"))
        for vd in sorted(bucket.iterdir()):
            if vd.is_dir():
                out += sorted(vd.glob("*.mp4"))
    return out
