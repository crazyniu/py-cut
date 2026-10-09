"""视频指纹自检：pHash（感知哈希）+ PSNR（峰值信噪比）。

对应 `docs/DEDUP.md` 第 6 节「自检闭环」——去重不能只管改、不管改了多少，
处理前后必须量化「视觉相似度下降了多少」与「画质掉了多少」：

- **pHash 距离**：DCT 32x32 取左上 8x8 低频，去掉 DC 后按位打包成 64 位整数，
  逐帧算汉明距离再取平均。距离越大 = 画面被改得越狠。
  门禁参考 `config.DEDUP_MIN_PHASH_DIST`（改动够不够）。
- **PSNR**：逐帧灰度 MSE → `10*log10(255^2/mse)` 后取平均，单位 dB，越高画质越好。
  门禁参考 `config.DEDUP_MIN_PSNR`（画质掉太多就要回退参数）。

本模块与去重编排（`core/dedup.py`）解耦，可独立用于 `--check` 基线测量。

抽帧策略（`AGENTS.md` 第 5 节「双重 seek」坑）：这里**故意不做 seek**，
而是用 cv2 顺序解码后按帧索引均匀取。视频关键帧间隔（常见 8.33s）往往远大于
抽帧间隔，seek 会落到邻近关键帧上、抽到别的镜头的帧，导致自检数值失真；
顺序解码虽然慢一些，但抽到的一定是第 i 帧本身。

纯标准库 + cv2 + numpy，不引入新依赖。
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .. import config

# 默认抽帧数（门禁与报告里的 N 都用它，保持一致）
DEFAULT_SAMPLES = config.DEDUP_PHASH_SAMPLES

# pHash 参数：DCT 32x32 -> 左上 8x8 低频 -> 64 位
_PHASH_DCT_SIZE = 32
_PHASH_LOW_SIZE = 8
_PHASH_BITS = _PHASH_LOW_SIZE * _PHASH_LOW_SIZE   # 64

# PSNR 完全一致时的上限值：mse=0 会让 log10(0) 出 -inf，用它兜住。
# 注意这是数学上的保护上限，不是标定出来的画质阈值。
_PSNR_MAX = 99.0

# PSNR 公式里的峰值信噪比基准（8bit 灰度）
_PSNR_PEAK = 255.0 ** 2


def phash(frame_bgr: np.ndarray) -> int:
    """单帧 BGR -> 64 位感知哈希（整数）。

    做法：灰度 -> 缩放到 32x32 -> 二维 DCT -> 取左上 8x8 低频系数 ->
    去掉 DC 分量（`[0,0]` 只反映整体亮度，留着会让哈希对亮度调整过于敏感）->
    与其余 63 个系数的中位数比较得 64 个比特 -> 低位在前打包成 int。

    打包时 DC 位恒为 0，所以哈希整体亮度不变；有效信息 63 bit。
    """
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        raise ValueError("phash: 传入的是空帧")

    gray = _to_gray(frame_bgr)
    # INTER_AREA 是缩小时的正确插值（避免混叠把高频混进低频，影响稳定性）
    small = cv2.resize(gray, (_PHASH_DCT_SIZE, _PHASH_DCT_SIZE),
                       interpolation=cv2.INTER_AREA)
    coeffs = cv2.dct(small.astype(np.float32))[:_PHASH_LOW_SIZE, :_PHASH_LOW_SIZE]

    # 中位数只用非 DC 的 63 个系数算，避免 DC 把基准拉偏
    body = coeffs.reshape(-1).copy()
    body[0] = 0.0
    median = float(np.median(body[1:]))

    bits = (coeffs.reshape(-1) > median)
    bits[0] = False                      # DC 位恒 0：哈希对整体亮度不敏感
    value = 0
    for i, bit in enumerate(bits):
        if bit:
            value |= 1 << i
    return value


def phash_distance(a: int, b: int) -> int:
    """两个 pHash 的汉明距离（0-64，越小越像；0 = 完全一致）。"""
    return int(a ^ b).bit_count()


def sample_frames(path: Path | str, n: int = DEFAULT_SAMPLES) -> list[np.ndarray]:
    """从视频均匀抽 n 帧，返回 BGR ndarray 列表（顺序解码后按索引取）。

    帧数不足 n 时返回实际能取到的全部帧（视频太短的正常情况）；
    一帧都读不到（路径不存在 / 损坏 / 非视频）抛 RuntimeError。
    """
    if n <= 0:
        return []

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频（文件不存在或格式不支持）: {path}")

    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            # 容器元数据不可靠（少见）：只能顺序解码后全量取，再抽稀
            return _sample_without_count(cap, path, n)

        indices = _pick_indices(total, n)
        wanted = set(indices)
        frames: list[np.ndarray] = []
        idx = 0
        last = indices[-1]
        while idx <= last:
            ok, frame = cap.read()
            if not ok or frame is None:
                break               # 实际帧数比元数据少，提前结束
            if idx in wanted:
                frames.append(frame)
            idx += 1
    finally:
        cap.release()

    if not frames:
        raise RuntimeError(f"抽帧失败：{path}（读到 0 帧）")
    return frames


def phash_profile(path: Path | str, n: int = DEFAULT_SAMPLES) -> list[int]:
    """视频的 pHash 序列（长度 = 实际抽到的帧数）。

    读帧失败抛 RuntimeError，由调用方决定是否降级。
    """
    return [phash(f) for f in sample_frames(path, n)]


def psnr_between(path_a: Path | str, path_b: Path | str,
                 n: int = DEFAULT_SAMPLES) -> float:
    """两视频平均 PSNR(dB)。

    逐帧算：灰度 -> 尺寸不同先缩放到较小者的尺寸（否则无法逐像素相减）->
    `mse = mean((a-b)^2)` -> `psnr = 10*log10(255^2/mse)`，最后取平均。
    两视频时长不同时按索引对齐（配对数 = 较少一侧的帧数），
    时长差异大时这个值只能当参考，别当严格结论。

    完全一致返回 99.0（避免 log10(0)）。
    """
    frames_a = sample_frames(path_a, n)
    frames_b = sample_frames(path_b, n)
    value, _pairs = _psnr_from_frames(frames_a, frames_b)
    return value


def compare(src: Path | str, dst: Path | str,
            n: int = DEFAULT_SAMPLES) -> dict:
    """去重自检主入口：对比原片与处理后视频的相似度与画质。

    返回::

        {"phash_dist": float,  # 两侧 pHash 序列的平均汉明距离（0-64）
         "psamples": int,      # 实际配对比较的帧数
         "psnr": float,        # 平均 PSNR(dB)
         "samples": int}       # 源侧抽到的帧数

    两序列长度不等时按可用配对数（较短一侧）计算。
    任一侧读帧失败抛 RuntimeError——是否降级（比如跳过该条）由调用方决定。
    """
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


def _seq_with_pts(path: Path | str) -> tuple[list[np.ndarray], list[float]]:
    """顺序解码全片，返回 (灰度帧列表, 每帧时间戳秒)。

    不用 seek：按帧索引对齐在**变速/帧率抖动/抽帧**之后会错位（见 compare_aligned）。
    只在需要时间戳对齐时调用，因此解码全片是可接受的代价（切片都是秒级短片）。
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频（文件不存在或格式不支持）: {path}")
    frames: list[np.ndarray] = []
    stamps: list[float] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            frames.append(_to_gray(frame))
            stamps.append(t if stamps or t > 0 else float(len(frames) - 1) / 30.0)
    finally:
        cap.release()
    if not frames:
        raise RuntimeError(f"抽帧失败：{path}（读到 0 帧）")
    return frames, stamps


def _nearest(frames: list[np.ndarray], stamps: list[float],
             targets: list[float]) -> list[np.ndarray]:
    """对每个目标时间戳取最近的一帧（双指针，O(n+m)）。"""
    out: list[np.ndarray] = []
    j = 0
    for t in targets:
        while j + 1 < len(stamps) and abs(stamps[j + 1] - t) <= abs(stamps[j] - t):
            j += 1
        out.append(frames[j])
    return out


def compare_aligned(src: Path | str, dst: Path | str,
                    n: int = DEFAULT_SAMPLES) -> dict:
    """**按时间戳对齐**的compare —— 变速 / 帧率抖动 / 时长变化时必须用这个。

    `compare` 按帧序号配对，一旦处理侧做了 setpts 变速或 fps 滤镜，两侧第 i 帧
    就不再是同一时刻，pHash 会虚高、PSNR 会虚低（实测变速 1.037 时会误判为不达标）。
    本函数先解出两边的帧时间戳，再在**同一批时间点**上各取最近帧比较，
    这样测到的差异才是真实的画面差异。

    返回同compare（多了 align="timestamp" 字段）。
    """
    fa, ta = _seq_with_pts(src)
    fb, tb = _seq_with_pts(dst)

    dur = min(ta[-1], tb[-1]) if ta and tb else 0.0
    if dur <= 0:
        raise RuntimeError("时间戳对齐失败：至少一侧没有有效时间戳")
    # 目标时间点取两侧共有的时间范围，避开首尾（首尾常有黑场/编码边界误差）
    span_lo, span_hi = ta[0], tb[-1]
    usable = min(dur, max(span_hi - span_lo, 0.0))
    if usable <= 0:
        usable = max(dur, 0.001)
    targets = [span_lo + usable * (i + 0.5) / n for i in range(n)]

    ga = _nearest(fa, ta, targets)
    gb = _nearest(fb, tb, targets)

    prof_a = [phash(f) for f in ga]
    prof_b = [phash(f) for f in gb]
    dist = sum(phash_distance(x, y) for x, y in zip(prof_a, prof_b)) / n
    psnr, _ = _psnr_from_frames(ga, gb)

    return {
        "phash_dist": round(dist, 3),
        "psamples": n,
        "psnr": round(psnr, 3),
        "samples": n,
        "align": "timestamp",
    }


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _pick_indices(total: int, n: int) -> list[int]:
    """在 [0, total-1] 上均匀取 n 个帧索引（含首尾），去重后升序。

    帧数 <= n 时全取。取整后相邻索引步长 >= 1，结果长度恰好为 min(total, n)。
    """
    if total <= 0 or n <= 0:
        return []
    if total <= n:
        return list(range(total))
    step = (total - 1) / (n - 1)
    return sorted({min(total - 1, int(round(i * step))) for i in range(n)})


def _sample_without_count(cap, path: Path | str, n: int) -> list[np.ndarray]:
    """元数据没有帧数时：顺序解码 + 流式均匀抽稀。

    事先不知道总帧数，没法直接算索引；这里的做法是每攒满 2n 帧就隔帧丢一半、
    同时把步长翻倍——结束后剩余帧在时间轴上近似等距，且内存峰值只有 2n 帧。
    只在容器元数据异常时走这条路径。
    """
    kept: list[np.ndarray] = []
    stride = 1          # 当前保留步长（每隔 stride-1 帧丢一帧）
    seen = 0            # 已解码帧数
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if (seen % stride) == 0:
            kept.append(frame)
            if len(kept) >= 2 * n:
                kept = kept[::2]
                stride *= 2
        seen += 1

    if not kept:
        raise RuntimeError(f"抽帧失败：{path}（读到 0 帧）")
    # 抽稀后多于 n 帧时，按索引均匀压到 n 帧
    return [kept[i] for i in _pick_indices(len(kept), n)]


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """BGR（或已是灰度）-> 灰度 uint8。"""
    if frame.ndim == 2:
        return frame if frame.dtype == np.uint8 else frame.astype(np.uint8)
    code = cv2.COLOR_BGR2GRAY if frame.shape[2] >= 3 else cv2.COLOR_BGRA2GRAY
    return cv2.cvtColor(frame, code)


def _psnr_from_frames(frames_a: list[np.ndarray],
                      frames_b: list[np.ndarray]) -> tuple[float, int]:
    """逐帧 PSNR 取平均，返回 (平均 dB, 配对数)。"""
    pairs = min(len(frames_a), len(frames_b))
    if pairs == 0:
        raise RuntimeError("PSNR 计算失败：两侧都没有可用帧")

    total = 0.0
    for i in range(pairs):
        ga = _to_gray(frames_a[i])
        gb = _to_gray(frames_b[i])
        if ga.shape != gb.shape:
            # 尺寸不同无法逐像素相减：统一缩到「宽高各取较小者」
            h = min(ga.shape[0], gb.shape[0])
            w = min(ga.shape[1], gb.shape[1])
            ga = cv2.resize(ga, (w, h), interpolation=cv2.INTER_AREA)
            gb = cv2.resize(gb, (w, h), interpolation=cv2.INTER_AREA)

        diff = ga.astype(np.float32) - gb.astype(np.float32)
        mse = float(np.mean(diff * diff))
        total += _PSNR_MAX if mse <= 0.0 else 10.0 * math.log10(_PSNR_PEAK / mse)

    return total / pairs, pairs
