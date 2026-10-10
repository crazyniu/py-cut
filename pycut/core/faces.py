"""人脸检测与三态判定（Haar 快速分筛 + LLM 兜底）。

策略：
  - Haar 严格 / 宽松两档先做快速分筛，省下绝大多数 LLM 调用。
  - 仅 ambiguous 段（抽帧失败 / 严格无命中但宽松有命中）才送中间帧给 LLM 兜底。
  - 三态：face / noface / ambiguous（noface 必须建立在"确实看到了帧且确实没检出脸"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2

from .. import config
from .ffmpeg import FFMPEG
from .utils import run_cmd


def load_cascades(cv2_module) -> tuple:
    """加载 Haar 级联（正面 + 侧面）。失败抛异常由调用方处理。"""
    cdir = cv2_module.data.haarcascades
    f_path = Path(cdir) / "haarcascade_frontalface_default.xml"
    p_path = Path(cdir) / "haarcascade_profileface.xml"
    frontal = cv2_module.CascadeClassifier(str(f_path))
    profile = cv2_module.CascadeClassifier(str(p_path))
    if frontal.empty() or profile.empty():
        raise RuntimeError(f"Haar 级联加载失败: {f_path} / {p_path}")
    return frontal, profile


def _extract_one(path: Path, t: float, tmpdir: Path, tag: str):
    """单帧抽取，必须用双重 seek（与切片同逻辑，否则会抽到别的镜头的帧）。"""
    dst = tmpdir / f"{tag}.jpg"
    coarse = max(0.0, t - config.COARSE_BACKOFF)
    fine = t - coarse
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if coarse > 0:
        args += ["-ss", f"{coarse:.3f}"]
    args += ["-i", str(path), "-ss", f"{fine:.3f}"]
    args += ["-frames:v", "1", "-vf", f"scale={config.FRAME_WIDTH}:-2",
             "-q:v", "3", str(dst)]
    rc, _o, _e = run_cmd(args, timeout=120)
    if rc != 0 or not dst.exists() or dst.stat().st_size < 500:
        return None
    return cv2.imread(str(dst))


def extract_frames(path: Path, seg: tuple[float, float], tmpdir: Path):
    """段内按 25/50/75% 抽 3 帧，两端内缩 12% 避开转场帧。"""
    s, e = seg
    span = e - s
    if span <= 0:
        return []
    inset = span * config.INSET
    frames = []
    for r in config.SAMPLE_RATIOS:
        t = s + inset + (span - 2 * inset) * r
        t = min(max(t, s + 0.01), e - 0.01)
        img = _extract_one(path, t, tmpdir,
                           f"f{len(frames)}_{t:.3f}".replace(".", "_"))
        if img is not None:
            frames.append(img)
    return frames


def haar_faces(cascades, frame, scale_factor: float, min_neighbors: int,
               min_size_ratio: float):
    """在单帧上跑 Haar 两分类器，返回检测框列表。"""
    frontal, profile = cascades
    h, w = frame.shape[:2]
    min_side = max(8, int(h * min_size_ratio))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    boxes = []
    for c in (frontal, profile):
        try:
            found = c.detectMultiScale(
                gray,
                scaleFactor=scale_factor,
                minNeighbors=min_neighbors,
                minSize=(min_side, min_side),
            )
        except cv2.error:
            continue
        for (x, y, fw, fh) in found:
            boxes.append((int(x), int(y), int(fw), int(fh)))
    return boxes


@dataclass
class FaceStat:
    strict_frames: int = 0      # 严格档命中人脸的帧数
    loose_frames: int = 0       # 宽松档命中人脸的帧数
    max_ratio: float = 0.0      # 最大人脸宽 / 帧高
    total_frames: int = 0
    mid_frame: Any = field(default=None, repr=False)


def analyze_segment(path: Path, seg: tuple[float, float], tmpdir: Path) -> FaceStat:
    """段内抽帧 -> Haar 两档统计 -> 记录中间帧供 LLM 兜底。"""
    frames = extract_frames(path, seg, tmpdir)
    stat = FaceStat(total_frames=len(frames))
    if not frames:
        return stat

    for idx, fr in enumerate(frames):
        fh = fr.shape[0]
        strict = haar_faces(_CASCADES, fr,
                            config.HAAR_STRICT["scaleFactor"], config.HAAR_STRICT["minNeighbors"],
                            config.HAAR_STRICT["min_size_ratio"])
        if strict:
            stat.strict_frames += 1
            stat.max_ratio = max(stat.max_ratio,
                                 max(w for (_x, _y, w, _h) in strict) / float(fh))
        else:
            loose = haar_faces(_CASCADES, fr,
                               config.HAAR_LOOSE["scaleFactor"], config.HAAR_LOOSE["minNeighbors"],
                               config.HAAR_LOOSE["min_size_ratio"])
            if loose:
                stat.loose_frames += 1
                stat.max_ratio = max(stat.max_ratio,
                                     max(w for (_x, _y, w, _h) in loose) / float(fh))
        if idx == len(frames) // 2:
            stat.mid_frame = fr

    return stat


# analyze_segment 需要 cascades；由命令层在启动时注入。
_CASCADES = None


def bind_cascades(cascades) -> None:
    """命令层 self_check 后注入 Haar 级联，供 analyze_segment 使用。"""
    global _CASCADES
    _CASCADES = cascades


def decide_face(stat: FaceStat) -> str:
    """三态判定：face / noface / ambiguous。"""
    if stat.total_frames <= 0:
        return "ambiguous"
    if stat.strict_frames >= stat.total_frames / 2.0 and stat.max_ratio >= config.FACE_WIDTH_RATIO:
        return "face"
    if stat.strict_frames == 0 and stat.loose_frames == 0:
        return "noface"
    return "ambiguous"
