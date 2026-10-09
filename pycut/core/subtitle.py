"""切片字幕真实擦除（流水线第三步）。

原理：
  1. 每片段抽 N 帧送 LLM 视觉检测所有叠加文字（标题/对白/警示条/免责行）的并集边界框。
  2. 取各帧边界框中位数（抗单帧跳动），紧贴字幕外扩 -> 像素区域（高度=字幕真实高度）。
  3. 逐帧在区域内构建"文字像素掩膜"（局部对比 + 饱和度线索），只对文字像素做 TELEA 修复，
     并按周边纹理强度补偿颗粒 + 轻度锐化 —— 字幕被"真实擦除"，区域外零改动。

对比旧版毛玻璃：无白雾、无硬边、无大面积模糊。幂等：写 <.covered.json> 标记，重复运行跳过。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .. import config
from .ffmpeg import FFMPEG, FFPROBE, probe_scalar
from .llm import LLMClient
from .runtime import STOP
from .utils import log

SAMPLES = 5             # 每片段抽帧检测数（越多越能覆盖所有文字行）
DETECT_WIDTH = 480      # 送检帧宽（足够 LLM 定位，省 token）
MAX_H_RATIO = 0.55      # 遮盖带最大高度占比（仅防误检把大半屏盖掉，正常不触发）
PAD_X, PAD_Y = 0.12, 0.12   # 检测框外扩比例（掩膜法只动文字像素，外扩只是兜底防漏）
CONTRAST_TH = 20        # 文字掩膜：与局部背景估计的灰度差阈值
SAT_TH = 150            # 文字掩膜：饱和度阈值（捕捉红/黄/蓝色块标题）
DILATE_K = 9            # 掩膜膨胀核（盖住描边/光晕/抗锯齿边缘）
INPAINT_R = 4           # TELEA 修复半径
GRAIN_BOOST = 1.3       # 填补区颗粒增益（匹配周边纹理强度）
WORKERS = 4             # 并发片段数（每片段 = N 次 LLM + 1 次逐帧修复）

SRC_BUCKETS = [config.OUT_FACE, config.OUT_NOFACE]

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
    """返回像素区域 (X, Y, BW, BH) 与识别到的字幕文本；无字幕/检测失败返回 None。"""
    dur_s = probe_scalar(clip, "format=duration")
    if not dur_s:
        return None
    dur = float(dur_s)
    w_s = probe_scalar(clip, "stream=width")
    h_s = probe_scalar(clip, "stream=height")
    if not w_s or not h_s:
        return None
    W, H = int(w_s), int(h_s)

    boxes: list[list[float]] = []
    texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="subchk_") as td:
        for i in range(samples):
            if STOP.flag:
                return None
            t = dur * (i + 0.5) / samples
            jp = Path(td) / f"f{i}.jpg"
            if not _grab_frame(clip, t, jp):
                continue
            frame = cv2.imread(str(jp))
            if frame is None:
                continue
            r = llm.ask_vision(frame, DETECT_PROMPT)
            if r and r.get("has_subtitle") and isinstance(r.get("box"), list) \
                    and len(r["box"]) == 4:
                try:
                    b = [float(v) for v in r["box"]]
                except (TypeError, ValueError):
                    continue
                if all(0.0 <= v <= 1.0 for v in b) and b[2] > b[0] and b[3] > b[1]:
                    boxes.append(b)
                    if r.get("text"):
                        texts.append(str(r["text"]))

    if not boxes:
        return None

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

    # 紧贴字幕外扩（少量余量确保盖全字形，但不做成固定大色块）
    x1 = max(0.0, x1 - PAD_X * (x2 - x1))
    x2 = min(1.0, x2 + PAD_X * (x2 - x1))
    y1 = max(0.0, y1 - PAD_Y * (y2 - y1))
    y2 = min(1.0, y2 + PAD_Y * (y2 - y1))

    # 边缘吸附：框贴近视图左右边缘时延伸到边缘 —— 大标题的装饰/发光常出血到画外，
    # LLM 框往往从可见文字主体量起，漏掉贴边部分（实测左缘残留蓝色装饰）。
    EDGE_GLUE = 0.10
    if x1 <= EDGE_GLUE:
        x1 = 0.0
    if x2 >= 1.0 - EDGE_GLUE:
        x2 = 1.0

    # 像素化 + 偶数对齐（h264 要求偶数尺寸）
    X, Y = int(x1 * W), int(y1 * H)
    BW, BH = int((x2 - x1) * W), int((y2 - y1) * H)
    X -= X % 2
    Y -= Y % 2
    BW += BW % 2
    BH += BH % 2
    BW = min(BW, W - X)
    BH = min(BH, H - Y)
    if BW < 8 or BH < 8:
        return None
    return X, Y, BW, BH, texts


def _adaptive_kernel(bh: int, bw: int) -> int:
    """文字掩膜局部背景估计的中值核：需大于笔画宽度，且小于区域短边。"""
    k = min(81, max(31, min(bh, bw) // 3))
    if k % 2 == 0:
        k -= 1
    if k >= min(bh, bw):
        k = min(bh, bw) - 1
        if k % 2 == 0:
            k -= 1
    return max(k, 3)


def cover_clip(clip: Path, region: tuple[int, int, int, int]) -> bool:
    """真实去字幕：逐帧文字掩膜 + TELEA 修复 + 颗粒补偿，区域外零改动，原子替换。"""
    X, Y, BW, BH = region
    w_s = probe_scalar(clip, "stream=width")
    h_s = probe_scalar(clip, "stream=height")
    fps_s = (probe_scalar(clip, "stream=avg_frame_rate").splitlines() or ["30"])[0].strip() or "30"
    if not w_s or not h_s:
        log(f"  [遮盖失败] {clip.name}: probe 不到分辨率")
        return False
    W, H = int(w_s), int(h_s)
    K = _adaptive_kernel(BH, BW)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (DILATE_K, DILATE_K))
    rng = np.random.default_rng(7)

    dec = subprocess.Popen(
        [FFMPEG, "-loglevel", "error", "-i", str(clip),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=10 ** 8)
    tmp = clip.with_name(clip.stem + ".tmp_cover.mp4")
    enc = subprocess.Popen(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", fps_s,
         "-i", "-",
         "-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE, bufsize=10 ** 8)
    try:
        frame_bytes = W * H * 3
        while True:
            raw = dec.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3).copy()
            roi = frame[Y:Y + BH, X:X + BW]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

            # --- 文字掩膜：局部对比（细笔画/描边）+ 饱和度（彩色色块标题）---
            bg = cv2.medianBlur(gray, K)
            _, m1 = cv2.threshold(
                cv2.absdiff(gray, bg), CONTRAST_TH, 255, cv2.THRESH_BINARY)
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            _, m2 = cv2.threshold(hsv[..., 1], SAT_TH, 255, cv2.THRESH_BINARY)
            mask = cv2.dilate(cv2.bitwise_or(m1, m2), kernel, iterations=2)

            # --- 修复：TELEA 只填补文字像素 ---
            res = cv2.inpaint(roi, mask, INPAINT_R, cv2.INPAINT_TELEA)

            # --- 颗粒补偿：按周边纹理强度在填补区合成同强度噪声，避免平滑补丁感 ---
            f32 = roi.astype(np.float32)
            resid = f32 - cv2.GaussianBlur(f32, (0, 0), 3)
            resid_sq = cv2.boxFilter(resid * resid, -1, (15, 15))
            resid_sq[mask > 0] = 0.0
            localvar = np.maximum(cv2.boxFilter(resid_sq, -1, (25, 25)), 1.0)
            mm = mask > 0
            if (~mm).any():
                localvar[mm] = np.percentile(localvar[~mm], 75)
            nl = cv2.GaussianBlur(rng.normal(0, 1, (BH, BW))
                                  .astype(np.float32), (0, 0), 0.8)[..., None]
            nc = rng.normal(0, 1, (BH, BW, 3)).astype(np.float32) * 0.35
            out = res.astype(np.float32) + (nl + nc) * np.sqrt(localvar) * GRAIN_BOOST
            res = np.clip(out, 0, 255).astype(np.uint8)

            # --- 轻度锐化（仅填补区内），恢复局部对比 ---
            sharp = cv2.addWeighted(res, 1.35, cv2.GaussianBlur(res, (0, 0), 3),
                                    -0.35, 0)
            soft = cv2.GaussianBlur(mask, (0, 0), 2).astype(np.float32) / 255.0
            res = (sharp.astype(np.float32) * soft[..., None]
                   + res.astype(np.float32) * (1 - soft[..., None])).astype(np.uint8)

            frame[Y:Y + BH, X:X + BW] = res
            try:
                enc.stdin.write(frame.tobytes())
            except BrokenPipeError:
                break
        enc.stdin.close()
        enc.wait(timeout=300)
        dec.wait(timeout=60)
        if enc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
            log(f"  [遮盖失败] {clip.name}: enc rc={enc.returncode}")
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

    region = detect_region(llm, clip, samples)
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
    for bucket in SRC_BUCKETS:
        if not bucket.is_dir():
            continue
        for vd in sorted(bucket.iterdir()):
            if vd.is_dir():
                out += sorted(vd.glob("*.mp4"))
    return out
