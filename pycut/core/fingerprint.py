"""Video change measurements using perceptual hashes and grayscale PSNR.

Sequentially decode and sample by frame index; keyframe seeks can select different
shots and invalidate comparisons. pHash measures visual change; PSNR is a coarse
damage floor after geometry changes, not a perceptual quality score. Neither
measurement proves platform acceptance. Uses only cv2, numpy, and the standard library."""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .. import config


DEFAULT_SAMPLES = config.DEDUP_PHASH_SAMPLES


_PHASH_DCT_SIZE = 32
_PHASH_LOW_SIZE = 8

# Finite cap for identical frames (MSE=0); not a calibrated quality threshold.
_PSNR_MAX = 99.0


_PSNR_PEAK = 255.0 ** 2


def phash(frame_bgr: np.ndarray) -> int:
    """Convert a BGR frame to a packed DCT perceptual hash.

    Resize to 32x32, threshold the top-left 8x8 coefficients against the non-DC median,
    and pack least-significant bit first. The DC bit stays zero (63 effective bits)."""
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        raise ValueError("phash: 传入的是空帧")

    gray = _to_gray(frame_bgr)
    # Area downsampling limits high-frequency aliasing in the low-frequency hash.
    small = cv2.resize(gray, (_PHASH_DCT_SIZE, _PHASH_DCT_SIZE),
                       interpolation=cv2.INTER_AREA)
    coeffs = cv2.dct(small.astype(np.float32))[:_PHASH_LOW_SIZE, :_PHASH_LOW_SIZE]

    # Exclude DC from the median so brightness does not shift the hash baseline.
    body = coeffs.reshape(-1).copy()
    body[0] = 0.0
    median = float(np.median(body[1:]))

    bits = (coeffs.reshape(-1) > median)
    bits[0] = False                      # Force the DC bit to zero for brightness invariance.
    value = 0
    for i, bit in enumerate(bits):
        if bit:
            value |= 1 << i
    return value


def phash_distance(a: int, b: int) -> int:
    """Return the Hamming distance between packed hashes; zero means identical hashes."""
    return int(a ^ b).bit_count()


def sample_frames(path: Path | str, n: int = DEFAULT_SAMPLES) -> list[np.ndarray]:
    """Return up to n BGR frames sampled by index during sequential decoding.

    Short videos return available frames. Raise RuntimeError if none can be read."""
    if n <= 0:
        return []

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频（文件不存在或格式不支持）: {path}")

    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            # If metadata omits frame count, decode once and thin the sample stream.
            return _sample_without_count(cap, path, n)

        indices = _pick_indices(total, n)
        wanted = set(indices)
        frames: list[np.ndarray] = []
        idx = 0
        last = indices[-1]
        while idx <= last:
            # Decode sequentially; retrieve only selected frames and avoid keyframe-seek mismatches.
            if not cap.grab():
                break
            if idx in wanted:
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    break
                frames.append(frame)
            idx += 1
    finally:
        cap.release()

    if not frames:
        raise RuntimeError(f"抽帧失败：{path}（读到 0 帧）")
    return frames


def phash_profile(path: Path | str, n: int = DEFAULT_SAMPLES) -> list[int]:
    """Return hashes of sampled video frames; propagate read failures."""
    return [phash(f) for f in sample_frames(path, n)]


def compare(src: Path | str, dst: Path | str,
            n: int = DEFAULT_SAMPLES) -> dict:
    """Return phash_dist, psamples, psnr, and samples for paired video frames.

    Use the shorter available sample sequence for pairing. Read failures raise
    RuntimeError; the caller owns the failure policy."""
    frames_a = sample_frames(src, n)
    frames_b = sample_frames(dst, n)

    prof_a = [phash(f) for f in frames_a]
    prof_b = [phash(f) for f in frames_b]
    pairs = min(len(prof_a), len(prof_b))

    dist = sum(phash_distance(prof_a[i], prof_b[i]) for i in range(pairs)) / pairs
    psnr, _ = _psnr_from_frames(frames_a, frames_b)

    return {
        "phash_dist": round(dist, 3),
        "psamples": pairs,
        "psnr": round(psnr, 3),
        "samples": len(frames_a),
    }


def _pick_indices(total: int, n: int) -> list[int]:
    """Return sorted unique sample indices, including endpoints when n permits.

    Videos shorter than n contribute every frame."""
    if total <= 0 or n <= 0:
        return []
    if total <= n:
        return list(range(total))
    if n == 1:
        return [0]
    step = (total - 1) / (n - 1)
    return sorted({min(total - 1, int(round(i * step))) for i in range(n)})


def _sample_without_count(cap, path: Path | str, n: int) -> list[np.ndarray]:
    """Sample a stream with unknown frame count using at most 2n buffered frames.

    At 2n frames, discard alternating samples and double the stride; reduce the final
    buffer to n approximately evenly spaced samples."""
    kept: list[np.ndarray] = []
    stride = 1
    seen = 0
    while cap.grab():
        # The unknown-count fallback also retrieves only sampled frames within its memory budget.
        if (seen % stride) == 0:
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                break
            kept.append(frame)
            if len(kept) >= 2 * n:
                kept = kept[::2]
                stride *= 2
        seen += 1

    if not kept:
        raise RuntimeError(f"抽帧失败：{path}（读到 0 帧）")

    return [kept[i] for i in _pick_indices(len(kept), n)]


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """Convert BGR or grayscale data to grayscale uint8."""
    if frame.ndim == 2:
        return frame if frame.dtype == np.uint8 else frame.astype(np.uint8)
    code = cv2.COLOR_BGR2GRAY if frame.shape[2] >= 3 else cv2.COLOR_BGRA2GRAY
    return cv2.cvtColor(frame, code)


def _psnr_from_frames(frames_a: list[np.ndarray],
                      frames_b: list[np.ndarray]) -> tuple[float, int]:
    """Return (mean PSNR in dB, paired frame count)."""
    pairs = min(len(frames_a), len(frames_b))
    if pairs == 0:
        raise RuntimeError("PSNR 计算失败：两侧都没有可用帧")

    total = 0.0
    for i in range(pairs):
        ga = _to_gray(frames_a[i])
        gb = _to_gray(frames_b[i])
        if ga.shape != gb.shape:
            # Resize both frames to their shared minimum dimensions before pixel subtraction.
            h = min(ga.shape[0], gb.shape[0])
            w = min(ga.shape[1], gb.shape[1])
            ga = cv2.resize(ga, (w, h), interpolation=cv2.INTER_AREA)
            gb = cv2.resize(gb, (w, h), interpolation=cv2.INTER_AREA)

        diff = ga.astype(np.float32) - gb.astype(np.float32)
        mse = float(np.mean(diff * diff))
        total += _PSNR_MAX if mse <= 0.0 else 10.0 * math.log10(_PSNR_PEAK / mse)

    return total / pairs, pairs
