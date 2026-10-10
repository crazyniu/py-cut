"""去重档位定义 + 按 seed 随机化参数（docs/DEDUP.md 第 4/5 节）。

本模块只做一件事：给定「档位 + seed + 调用方开关」，产出一份**确定性**的参数 dict。
它不跑 ffmpeg、不碰文件，方便单测与复盘（同一 seed 永远得到同一份参数）。

分层与其它模块的分工：
    M1 容器层 container → crf / gop / abitrate / fps / encoder_tag
    M2 几何层 geo      → crop_px / zoom / rotate_deg / perspective_px / mirror
    M3 像素层 pixel    → brightness / contrast / saturation / hue_deg /
                         channel_gains / noise / sharpen
    M4 时间层 time     → speed
    M5 结构层 struct   → border / badge / logo（另加时间层的变速）
    M6 音频层 audio    → pitch
    M7 水印层          → delogo（调用方给的 "x,y,w,h"，本模块只透传）

所有取值区间一律从 config.DEDUP_* 读，本模块不写死标定数值。
"""
from __future__ import annotations

import hashlib
import random

from .. import config


# ---------------------------------------------------------------------------
# 档位定义
# ---------------------------------------------------------------------------
#   geo / pixel / audio : 0~1 的强度系数，用来在 config 给出的 [min,max] 里取子区间，
#                         系数越小 → 参数越轻（light 明显小于 aggressive）。
#   time / struct / container : 模块开关（bool）。
#   encode : M1 编码参数的强度系数（crf / gop / abitrate），让轻档码率更保守、画质更稳。
#   mirror : 调用方传 --mirror 时，实际启用镜像的概率（轻档不给镜像，见 docs/DEDUP.md 第 2 节）。
#   label  : 档位中文名，只用于日志/报告。
PRESETS: dict[str, dict] = {
    "light": {
        "geo": 0.4, "pixel": 0.4, "time": False, "struct": False,
        "audio": 0.4, "container": True,
        "encode": 0.4, "mirror": 0.0, "label": "轻度",
    },
    "standard": {
        "geo": 1.0, "pixel": 1.0, "time": False, "struct": False,
        "audio": 1.0, "container": True,
        "encode": 0.75, "mirror": 0.35, "label": "标准",
    },
    "aggressive": {
        "geo": 1.0, "pixel": 1.0, "time": True, "struct": True,
        "audio": 1.0, "container": True,
        "encode": 1.0, "mirror": 0.55, "label": "激进",
    },
}

# 画中画位置候选（对应 overlay 的 x/y 表达式由 dedup.py 负责）
LOGO_POSITIONS = ("tr", "bl", "br", "tl")

# 帧率抖动：命中概率固定，与档位无关（DEDUP_FPS_JITTER 里 60fps 帧率翻倍，
# 故意降权，避免批量处理时把片子的帧率整体抬高）
_FPS_CHANCE = 0.6
_FPS_WEIGHTS = (3, 3, 3, 1)      # 对应 DEDUP_FPS_JITTER 的前三项与 "60"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def derive_seed(*parts) -> int:
    """由任意可转字符串的成分稳定推导 32 位 seed。

    刻意**不用**内置 hash()（str 的 hash 每进程加盐，跨进程不稳定），改用
    blake2b：同一组成分在任何机器、任何时间都得到同一个 seed。

    >>> derive_seed("a.mp4", 3, "acc-1") == derive_seed("a.mp4", 3, "acc-1")
    True
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _span(rng: random.Random, lo: float, hi: float, strength: float,
          signed: bool = False) -> float:
    """在 [lo,hi] 里按强度取子区间 [lo, lo+(hi-lo)*strength] 再随机取值。

    signed=True 时随机取正负（用于亮度/色相/旋转这类双向偏移）。
    """
    top = lo + (hi - lo) * _clamp01(strength)
    v = rng.uniform(lo, top)
    if signed and rng.random() < 0.5:
        v = -v
    return v


def _int_span(rng: random.Random, lo: int, hi: int, strength: float) -> int:
    return int(round(_span(rng, float(lo), float(hi), strength)))


def _resolve(preset: str) -> tuple[str, dict]:
    """未知档位回落到 config.DEDUP_DEFAULT_PRESET。"""
    name = preset if preset in PRESETS else config.DEDUP_DEFAULT_PRESET
    return name, PRESETS[name]


# ---------------------------------------------------------------------------
# M1 可选码率目标：隔离随机流，不改变现有 28 项 preset 的采样顺序
# ---------------------------------------------------------------------------
# 以既有 VideoInfo.bitrate (config.BITRATE_LOW/HIGH) 为基线。
# 仅显式 --vary-bitrate 时启用；变化严格限制为基线的 ±6%，
# 属于保守的初始产品范围，实际编码结果须在授权素材上测量。
_VIDEO_BITRATE_SHIFT_PCTS = (-6, -4, -2, 2, 4, 6)


def video_bitrate_for_seed(seed: int, baseline: str) -> tuple[str, int]:
    """返回 (libx264 ABR 目标码率, 偏移百分比)，同 seed 跨进程可复现。

    独立派生随机流，保证开启码率选项不会影响既有 build_params 的
    CRF/GOP、滤镜和音轨随机化序列。baseline 取既有 VideoInfo.bitrate。
    """
    value = str(baseline).strip().lower()
    try:
        if value.endswith("m"):
            base_kbps = round(float(value[:-1]) * 1000)
        elif value.endswith("k"):
            base_kbps = round(float(value[:-1]))
        else:
            raise ValueError("码率必须带 M/k 单位")
    except ValueError as exc:
        raise ValueError(f"不可用的视频基准码率: {baseline!r}") from exc
    if base_kbps <= 0:
        raise ValueError(f"视频基准码率必须大于零: {baseline!r}")
    rng = random.Random(derive_seed("dedup-bitrate-v1", seed))
    change = rng.choice(_VIDEO_BITRATE_SHIFT_PCTS)
    kbps = round(base_kbps * (100 + change) / 100)
    return f"{kbps}k", change


# ---------------------------------------------------------------------------
# M2 可选 Ken Burns：独立 seed 流，不触碰默认 28 键 preset 采样
# ---------------------------------------------------------------------------
_KEN_BURNS_DIRECTIONS = ("lr", "rl", "tb", "bt")


def ken_burns_for_seed(seed: int, duration: float) -> dict:
    """按 seed 生成缓慢缩放/平移参数，不加载媒体也不改变既有随机流。

    方向和幅度由单独派生的 RNG 决定；周期最长为视频实际时长，
    极短视频使用最少 0.05 秒周期，保证滤镜表达式无除零风险。
    """
    length = float(duration)
    if not (0 < length < float("inf")):
        raise ValueError(f"Ken Burns 视频时长无效: {duration!r}")
    rng = random.Random(derive_seed("dedup-ken-burns-v1", seed))
    period = round(max(0.05, min(length, rng.uniform(3.0, 5.0))), 3)
    return {
        "ken_burns_direction": rng.choice(_KEN_BURNS_DIRECTIONS),
        "ken_burns_zoom_start": round(rng.uniform(1.018, 1.025), 4),
        "ken_burns_zoom_delta": round(rng.uniform(0.010, 0.020), 4),
        "ken_burns_period": period,
    }


# ---------------------------------------------------------------------------
# 参数生成
# ---------------------------------------------------------------------------
def build_params(preset: str, seed: int, *, has_audio: bool = True,
                 width: int = 0, height: int = 0,
                 mirror: bool = False, speed: bool = False,
                 border: bool = False, badge: bool = False,
                 logo: str = "", delogo: str = "") -> dict:
    """按档位 + seed 生成确定性的去重参数（同样的输入 → 完全一样的 dict）。

    单项开关（mirror / speed / border / badge / logo / delogo）受调用方控制：
    调用方没开的一律不做；调用方开了但档位不允许（如 light 的结构层、镜像），
    则按档位概率或直接否决，并在 summary 里说明。

    width / height 用于把裁剪像素夹在安全范围内（不超过短边的 5%），0 表示未知则不夹。
    """
    name, cfg = _resolve(preset)
    rng = random.Random(seed)

    geo = float(cfg["geo"])
    pixel = float(cfg["pixel"])
    audio = float(cfg["audio"])
    encode = float(cfg["encode"])
    notes: list[str] = []

    # ---- M2 几何层 -------------------------------------------------------
    crop_px = _int_span(rng, config.DEDUP_CROP_RANGE[0],
                        config.DEDUP_CROP_RANGE[1], geo)
    if width > 0 and height > 0:                    # 别把画面裁狠了
        cap = max(2, int(min(width, height) * 0.05))
        crop_px = min(crop_px, cap)
    crop_px = min(int(config.DEDUP_CROP_RANGE[1]), crop_px + crop_px % 2)
    zoom = round(1.0 + _span(rng, config.DEDUP_ZOOM_RANGE[0] - 1.0,
                             config.DEDUP_ZOOM_RANGE[1] - 1.0, geo), 4)
    rotate_deg = round(_span(rng, config.DEDUP_ROTATE_DEG[0],
                             config.DEDUP_ROTATE_DEG[1], geo, signed=True), 3)

    # 梯形微变形：**已停用**，恒为 0。键保留是为了 28 键契约完整、读旧报告不报错。
    # 停用原因（2026-10-09 实测）：ffmpeg 9 的 perspective 不带 s= 时输出尺寸
    # 与输入不一致，四角坐标会把画面径向拉花成细长条（已抽帧确认为真实损坏，
    # 不是度量失真）；而去重收益仅 0.42~0.75 pHash，收益极小、破坏性极大。
    # dedup.py 侧同步不再生成该滤镜，故此处也必须为 0，
    # 否则 summary 会宣称做了实际没做的扰动，破坏 docs 第 5 节的复盘契约。
    perspective_px = 0

    use_mirror = bool(mirror) and rng.random() < float(cfg["mirror"])

    # ---- M3 像素层 -------------------------------------------------------
    brightness = round(_span(rng, 0.0, config.DEDUP_BRIGHTNESS, pixel,
                             signed=True), 4)
    contrast = round(_span(rng, config.DEDUP_CONTRAST_RANGE[0],
                           config.DEDUP_CONTRAST_RANGE[1], pixel), 4)
    saturation = round(_span(rng, config.DEDUP_SATURATION_RANGE[0],
                             config.DEDUP_SATURATION_RANGE[1], pixel), 4)
    hue_deg = round(_span(rng, 0.0, config.DEDUP_HUE_DEG, pixel, signed=True), 2)

    drift = round(_span(rng, 0.0, config.DEDUP_RGB_DRIFT, pixel), 4)
    gains = tuple(round(1.0 + rng.uniform(-drift, drift), 4) for _ in range(3))
    if all(abs(g - 1.0) < 1e-9 for g in gains):      # 兜底：至少一个通道要动
        gains = (round(1.0 + drift, 4), 1.0, 1.0)
    rgb_drift = drift

    noise = _int_span(rng, config.DEDUP_NOISE_RANGE[0],
                      config.DEDUP_NOISE_RANGE[1], pixel)
    sharpen = round(_span(rng, config.DEDUP_SHARPEN_RANGE[0],
                          config.DEDUP_SHARPEN_RANGE[1], pixel), 3)

    # ---- M4 时间层：变速要档位与调用方都同意 ------------------------------
    speed_val = 1.0
    if cfg["time"] and speed:
        speed_val = round(_span(rng, config.DEDUP_SPEED_RANGE[0],
                                config.DEDUP_SPEED_RANGE[1], 1.0), 4)

    # ---- M6 音频层：变调保留时长；无音轨就 1.0 --------------------------
    pitch = 1.0
    if has_audio and audio > 0:
        pitch = round(_span(rng, config.DEDUP_PITCH_RANGE[0],
                            config.DEDUP_PITCH_RANGE[1], audio), 5)

    # ---- M1 容器层 -------------------------------------------------------
    if cfg["container"]:
        crf = _int_span(rng, config.DEDUP_CRF_RANGE[0],
                        config.DEDUP_CRF_RANGE[1], encode)
        gop = _int_span(rng, config.DEDUP_GOP_RANGE[0],
                        config.DEDUP_GOP_RANGE[1], encode)
        ab_kbps = _int_span(rng, config.DEDUP_ABITRATE_RANGE[0],
                            config.DEDUP_ABITRATE_RANGE[1], audio)
    else:                                           # 理论不可达，留个兜底
        crf = int(config.DEDUP_CRF_RANGE[0])
        gop = int(config.DEDUP_GOP_RANGE[0])
        ab_kbps = int(config.DEDUP_ABITRATE_RANGE[0])
    abitrate = f"{ab_kbps}k"

    fps = ""
    if rng.random() < _FPS_CHANCE:
        fps = rng.choices(list(config.DEDUP_FPS_JITTER),
                          weights=_FPS_WEIGHTS[:len(config.DEDUP_FPS_JITTER)],
                          k=1)[0]

    tag_hex = "".join(rng.choice("0123456789abcdef") for _ in range(6))
    encoder_tag = f"pycut-{tag_hex}"

    # ---- M5 结构层：默认只有 aggressive 可能开，调用方显式传 True 则尊重 ---
    use_border = bool(border) or (bool(cfg["struct"]) and rng.random() < 0.5)
    use_badge = bool(badge) or (bool(cfg["struct"]) and rng.random() < 0.6)
    if (use_border and not border) or (use_badge and not badge):
        notes.append("结构层自动启用（调用方未显式指定）")
    logo_pos = rng.choice(LOGO_POSITIONS)
    if logo and not cfg["struct"]:
        notes.append("画中画已按调用方要求保留")

    # ---- 摘要 ------------------------------------------------------------
    bits = [f"档位={name}"]
    if cfg["container"]:
        bits.append(f"重编码 CRF={crf} GOP={gop} 音频={abitrate}")
    if fps:
        bits.append(f"帧率={fps}")
    bits.append(f"元数据编码串={encoder_tag}")
    if crop_px:
        bits.append(f"边缘微裁 {crop_px}px")
    if zoom > 1.0:
        bits.append(f"微缩放 {zoom}")
    if rotate_deg:
        bits.append(f"微旋转 {rotate_deg}°")
    if perspective_px:
        bits.append(f"梯形变形 {perspective_px}px")
    if use_mirror:
        bits.append("水平镜像（有人审风险）")
    if brightness:
        bits.append(f"亮度 {brightness:+}")
    if contrast != 1.0:
        bits.append(f"对比度 {contrast}")
    if saturation != 1.0:
        bits.append(f"饱和度 {saturation}")
    if hue_deg:
        bits.append(f"色相 {hue_deg:+}°")
    if rgb_drift:
        bits.append(f"通道增益漂移 ±{rgb_drift}")
    if noise:
        bits.append(f"微噪 {noise}")
    if sharpen:
        bits.append(f"微锐化 {sharpen}")
    if speed_val != 1.0:
        bits.append(f"变速 {speed_val}（音视频同步）")
    if pitch != 1.0:
        bits.append(f"变调 {pitch}（保留时长）")
    if use_border:
        bits.append("模糊边框填充")
    if use_badge:
        bits.append("右下角色块角标")
    if logo:
        bits.append(f"画中画 {logo_pos}")
    if delogo:
        bits.append(f"delogo {delogo}")
    if notes:
        bits.extend(notes)
    summary = "；".join(bits)

    return {
        # M2 几何
        "crop_px": crop_px,
        "zoom": zoom,
        "rotate_deg": rotate_deg,
        "perspective_px": perspective_px,
        "mirror": use_mirror,
        # M3 像素
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "hue_deg": hue_deg,
        "rgb_drift": rgb_drift,
        "channel_gains": gains,
        "noise": noise,
        "sharpen": sharpen,
        # M4 时间
        "speed": speed_val,
        # M6 音频
        "pitch": pitch,
        # M1 容器
        "fps": fps,
        "crf": crf,
        "gop": gop,
        "abitrate": abitrate,
        "encoder_tag": encoder_tag,
        # M5 结构 / M7 水印
        "border": use_border,
        "badge": use_badge,
        "logo": logo,
        "logo_pos": logo_pos,
        "delogo": delogo,
        # 回填
        "preset": name,
        "seed": seed,
        "summary": summary,
    }