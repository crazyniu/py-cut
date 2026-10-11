"""检测字幕高度，在全视频宽度的矩形内覆盖马赛克，保留音轨并原子替换。"""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import cv2

from .. import config
from .ffmpeg import FFMPEG, probe_scalar
from .llm import LLMClient
from .runtime import STOP
from .utils import log

SAMPLES = 5             # 每片段抽帧检测数（越多越能覆盖所有文字行）
DETECT_WIDTH = 480      # 送检帧宽（足够 LLM 定位，省 token）
PAD_Y = 0.12  # 按字幕高度增加上下余量
WORKERS = 4             # 并发片段数（每片段 = N 次 LLM + 1 次逐帧修复）

# 切片桶在运行时可能被 slice/dedup 的 --src-dir/--out-dir 覆盖，故 collect_clips
# 直接读 config.OUT_FACE/OUT_NOFACE（动态），不再用 import 时的快照列表。

DETECT_PROMPT = (
    "这是竖屏短视频的一帧。检测画面中人为叠加的文字层"
    "(包括大号艺术标题、对白字幕、警示条、免责声明行等后期叠加的文字, "
    "不包括背景招牌、商品包装上的文字)。"
    "若有, 返回能同时覆盖画面中【所有】叠加文字的最小边界框"
    "(有多处文字时取整体并集框), 归一化坐标 0-1, 格式 [x1,y1,x2,y2], y 向下为正。"
    "只返回 JSON: "
    '{"has_subtitle": true 或 false, "box": [x1,y1,x2,y2] 或 null, "text": "字幕原文"}'
)


def _grab_frame(clip: Path, t: float, jp: Path, width: int = DETECT_WIDTH) -> bool:
    """ffmpeg 单帧抽图（检测用途，不要求帧级精度，输入侧 seek 足够）。"""
    r = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(clip),
         "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(jp)],
        capture_output=True, timeout=60)
    return r.returncode == 0 and jp.exists()


def detect_region(llm: LLMClient, clip: Path,
                  samples: int = SAMPLES) -> tuple[int, int, int, int, list[str]] | None:
    """返回字幕区域或明确的“无字幕”(None)；探测失败抛 RuntimeError。

    检测不到文字与抽帧/LLM 全部失败不是同一个状态。失败时禁止写入
    “无字幕”标记，否则往后每次重跑都会直接跳过可能存在的字幕。
    """
    if samples < 1:
        raise RuntimeError("至少需要抽取一帧进行字幕检测")
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

    boxes: list[list[float]] = []
    texts: list[str] = []
    clean_replies = 0
    uncertain_positive = 0
    with tempfile.TemporaryDirectory(prefix="subchk_") as td:
        for i in range(samples):
            if STOP.flag:
                raise RuntimeError("用户中断字幕检测")
            t = dur * (i + 0.5) / samples
            jp = Path(td) / f"f{i}.jpg"
            if not _grab_frame(clip, t, jp):
                continue
            frame = cv2.imread(str(jp))
            if frame is None:
                continue
            r = llm.ask_vision(frame, DETECT_PROMPT)
            if not isinstance(r, dict):
                continue
            if r.get("has_subtitle") is False:
                clean_replies += 1
                continue
            if r.get("has_subtitle") is not True:
                continue
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
        # 只有过半采样点得到明确否定且不存在“检测到字幕但框无效”
        # 的冲突回复，才能持久标记无字幕。其余情形保留重试机会。
        if clean_replies >= (samples // 2 + 1) and uncertain_positive == 0:
            return None
        raise RuntimeError(
            f"字幕检测结果不可靠：有效无字幕答复 {clean_replies}/{samples}，"
            f"无法定位的字幕答复 {uncertain_positive}；未标记已处理"
        )

    # Moving/two-line subtitles must fit every sampled bounding box.
    x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)

    # 高度跟随字幕边界，宽度固定覆盖整个视频。
    pad = PAD_Y * (y2 - y1)
    y1 = max(0.0, y1 - pad)
    y2 = min(1.0, y2 + pad)

    # 像素化 + 偶数对齐（h264 要求偶数尺寸）
    X, Y = 0, int(y1 * H)
    BW, BH = W, int((y2 - y1) * H)
    X -= X % 2
    Y -= Y % 2
    BW += BW % 2
    BH += BH % 2
    BW = min(BW, W - X)
    BH = min(BH, H - Y)
    if BW < 8 or BH < 8:
        raise RuntimeError("字幕区域过小，无法可靠遮盖；未标记无字幕")
    return X, Y, BW, BH, texts


def cover_clip(clip: Path, region: tuple[int, int, int, int]) -> bool:
    """FFmpeg 内完成全宽矩形马赛克，避免整帧往返 Python。"""
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
    block = max(12, info.width // 30)
    small_w, small_h = max(1, info.width // block), max(1, height // block)
    filters = (f"[0:v:0]split[base][sub];"
               f"[sub]crop={info.width}:{height}:0:{top}:exact=1,"
               f"scale={small_w}:{small_h}:flags=area,"
               f"scale={info.width}:{height}:flags=neighbor[mask];"
               f"[base][mask]overlay=0:{top}:shortest=1[v]")
    tmp = clip.with_name(clip.stem + ".tmp_cover.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(clip),
           "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", config.SLICE_ENCODER, "-b:v", "8M", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    proc = None
    try:
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
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
                and (record.get("subtitle") is False or record.get("verified") is True))
    except (OSError, ValueError):
        return False


def process_clip(llm: LLMClient, clip: Path, *, force: bool = False,
                 dry_run: bool = False, samples: int = SAMPLES) -> str:
    """处理单个切片，返回状态: covered / nosub / skip / fail / dryrun。"""
    mk = _marker(clip)
    if verified_clip(clip) and not force:
        return "skip"

    try:
        region = detect_region(llm, clip, samples)
    except (RuntimeError, ValueError, OSError) as exc:
        log(f"  [字幕检测失败] {clip.name}: {exc}")
        return "fail"
    if region is None:
        if not llm.enabled:
            return "fail"  # LLM 不可用时不能盲盖
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
        residual = detect_region(llm, clip, min(samples, 3))
    except (RuntimeError, ValueError, OSError) as exc:
        log(f"  [字幕复检失败] {clip.name}: {exc}；未标记完成")
        return "fail"
    if residual is not None:
        log(f"  [字幕残留] {clip.name}: 擦除后仍检测到字幕；未标记完成，请重试或检查画面")
        return "fail"
    mk.write_text(json.dumps({"version": 2, "subtitle": True, "verified": True, "verified_samples": min(samples, 3), "region": [X, Y, BW, BH],
                              "texts": texts,
                              "covered_at": datetime.now().isoformat()},
                             ensure_ascii=False), encoding="utf-8")
    log(f"  [擦除复检通过] {clip.name}: 区域({X},{Y},{BW}x{BH}) "
        f"{' / '.join(dict.fromkeys(texts))[:50]}")
    return "covered"


def collect_clips(paths: list[Path] | None = None) -> list[Path]:
    """收集待遮盖切片：指定路径优先，否则遍历两个切片桶。"""
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
