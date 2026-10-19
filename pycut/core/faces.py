"""Three-state face classification using strict/loose Haar detection and optional LLM review.

Only ambiguous segments reach the LLM. noface requires decoded frames with no
detection; extraction failure remains ambiguous."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2

from .. import config
from .ffmpeg import FFMPEG
from .utils import run_cmd


def load_cascades(cv2_module) -> tuple:
    """Load frontal/profile Haar cascades; raise when unavailable."""
    cdir = cv2_module.data.haarcascades
    f_path = Path(cdir) / "haarcascade_frontalface_default.xml"
    p_path = Path(cdir) / "haarcascade_profileface.xml"
    frontal = cv2_module.CascadeClassifier(str(f_path))
    profile = cv2_module.CascadeClassifier(str(p_path))
    if frontal.empty() or profile.empty():
        raise RuntimeError(f"Haar 级联加载失败: {f_path} / {p_path}")
    return frontal, profile


def _extract_one(path: Path, t: float, tmpdir: Path, tag: str):
    """Extract one frame using coarse input seek plus accurate output seek."""
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
    """Sample configured positions inside the segment, inset from transition frames."""
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
    """Return detection boxes from both Haar classifiers for one frame."""
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
    strict_frames: int = 0
    loose_frames: int = 0
    max_ratio: float = 0.0
    total_frames: int = 0
    mid_frame: Any = field(default=None, repr=False)


def analyze_segment(path: Path, seg: tuple[float, float], tmpdir: Path) -> FaceStat:
    """Sample frames, count strict/loose detections, and retain a middle frame for LLM review."""
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


# The command layer injects cascades before analyze_segment runs.
_CASCADES = None


def bind_cascades(cascades) -> None:
    """Inject checked Haar cascades before segment analysis."""
    global _CASCADES
    _CASCADES = cascades


def decide_face(stat: FaceStat) -> str:
    """Return face, noface, or ambiguous from the sampled detection statistics."""
    if stat.total_frames <= 0:
        return "ambiguous"
    if stat.strict_frames >= stat.total_frames / 2.0 and stat.max_ratio >= config.FACE_WIDTH_RATIO:
        return "face"
    if stat.strict_frames == 0 and stat.loose_frames == 0:
        return "noface"
    return "ambiguous"
