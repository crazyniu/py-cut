"""检测字幕高度，在全视频宽度的矩形内覆盖马赛克，保留音轨并原子替换。"""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .. import config
from .ffmpeg import FFMPEG, probe_scalar
from .llm import LLMClient
from .runtime import STOP
from .utils import log

SAMPLES = 5             # 每片段抽帧检测数（越多越能覆盖所有文字行）
DETECT_WIDTH = 480      # 送检帧宽（足够 LLM 定位，省 token）
MAX_H_RATIO = 0.55      # 遮盖带最大高度占比（仅防误检把大半屏盖掉，正常不触发）
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


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


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

    # 中位数聚合：抗单帧位置漂移 / 个别帧把警示条框得偏上或偏下
    x1, y1 = _median([b[0] for b in boxes]), _median([b[1] for b in boxes])
    x2, y2 = _median([b[2] for b in boxes]), _median([b[3] for b in boxes])

    # 安全限高：仅当误检把带高撑得过大时才收缩，正常字幕很小不会触发
    if (y2 - y1) > MAX_H_RATIO:
        cy = (y1 + y2) / 2.0
        half = MAX_H_RATIO / 2.0
        y1 = max(0.0, cy - half)
        y2 = min(1.0, cy + half)
    y2 = min(y2, 0.995)

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
        return None
    return X, Y, BW, BH, texts


def mosaic_region(frame: np.ndarray, y: int, height: int) -> None:
    """修改一条全宽矩形带；带外像素保持不变。"""
    h, w = frame.shape[:2]
    top, bottom = max(0, y), min(h, y + height)
    if bottom <= top:
        raise ValueError("字幕区域高度无效")
    roi = frame[top:bottom, :]
    block = max(12, w // 30)
    small = cv2.resize(roi, (max(1, w // block), max(1, (bottom - top) // block)),
                       interpolation=cv2.INTER_AREA)
    frame[top:bottom, :] = cv2.resize(small, (w, bottom - top),
                                    interpolation=cv2.INTER_NEAREST)


def cover_clip(clip: Path, region: tuple[int, int, int, int]) -> bool:
    """全宽矩形马赛克，保留原声，编码成功才替换原文件。"""
    X, Y, BW, BH = region
    w_s = probe_scalar(clip, "stream=width")
    h_s = probe_scalar(clip, "stream=height")
    fps_s = (probe_scalar(clip, "stream=avg_frame_rate").splitlines() or ["30"])[0].strip() or "30"
    if not w_s or not h_s:
        log(f"  [遮盖失败] {clip.name}: probe 不到分辨率")
        return False
    W, H = int(w_s), int(h_s)
    dec = subprocess.Popen(
        [FFMPEG, "-loglevel", "error", "-i", str(clip),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=10 ** 8)
    tmp = clip.with_name(clip.stem + ".tmp_cover.mp4")
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", fps_s,
         "-i", "-",
         # 视频来自修复后的 rawvideo，音轨必须重新从原视频读取。
         # 旧代码仅有 stdin 这一路（无音轨），-c:a copy 并不能凭空保留声音。
         "-i", str(clip),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", config.SLICE_ENCODER, "-b:v", "8M", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE, bufsize=10 ** 8)
    try:
        frame_bytes = W * H * 3
        written = 0
        broken_pipe = False
        while True:
            if STOP.flag:
                broken_pipe = True
                break
            raw = dec.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3).copy()
            mosaic_region(frame, Y, BH)
            try:
                enc.stdin.write(frame.tobytes())
                written += 1
            except BrokenPipeError:
                broken_pipe = True
                break
        enc.stdin.close()
        enc.wait(timeout=300)
        dec.wait(timeout=60)
        if (broken_pipe or written == 0 or dec.returncode != 0
                or enc.returncode != 0 or not tmp.exists()
                or tmp.stat().st_size < 1024):
            log(f"  [遮盖失败] {clip.name}: dec rc={dec.returncode} / "
                f"enc rc={enc.returncode} / 已编码帧 {written}")
            return False
        import os  # noqa: PLC0415
        os.replace(str(tmp), str(clip))
        return True
    finally:
        for p in (dec, enc):
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _marker(clip: Path) -> Path:
    return clip.with_name(clip.name + ".covered.json")


def process_clip(llm: LLMClient, clip: Path, *, force: bool = False,
                 dry_run: bool = False, samples: int = SAMPLES) -> str:
    """处理单个切片，返回状态: covered / nosub / skip / fail / dryrun。"""
    mk = _marker(clip)
    if mk.exists() and not force:
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
        mk.write_text(json.dumps({"subtitle": False,
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
    mk.write_text(json.dumps({"subtitle": True, "region": [X, Y, BW, BH],
                              "texts": texts,
                              "covered_at": datetime.now().isoformat()},
                             ensure_ascii=False), encoding="utf-8")
    log(f"  [已遮盖] {clip.name}: 区域({X},{Y},{BW}x{BH}) "
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
        for vd in sorted(bucket.iterdir()):
            if vd.is_dir():
                out += sorted(vd.glob("*.mp4"))
    return out
