"""Deterministic deduplication parameters derived from a preset and seed.

No media I/O or FFmpeg execution. Read calibrated ranges from config; callers
control optional geometry, structure, temporal, audio, and visible-logo operations.
Keep the existing parameter keys and RNG draw order stable for reproducibility."""
from __future__ import annotations

import hashlib
import random

from .. import config


# geo/pixel/audio/encode are range-strength factors; time/struct/container are switches.
# Preset mirror probability applies only when the caller enables mirroring.
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


LOGO_POSITIONS = ("tr", "bl", "br", "tl")

# Downweight the 60-FPS choice to avoid doubling frame rates throughout a batch.
_FPS_CHANCE = 0.6
_FPS_WEIGHTS = (3, 3, 3, 1)


def derive_seed(*parts) -> int:
    """Derive a stable 32-bit seed from stringifiable components using blake2b.

    Do not use hash(): Python salts string hashes between processes."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _span(rng: random.Random, lo: float, hi: float, strength: float,
          signed: bool = False) -> float:
    """Sample [lo, lo + (hi-lo)*strength]; signed=True randomly chooses the sign."""
    top = lo + (hi - lo) * _clamp01(strength)
    v = rng.uniform(lo, top)
    if signed and rng.random() < 0.5:
        v = -v
    return v


def _int_span(rng: random.Random, lo: int, hi: int, strength: float) -> int:
    return int(round(_span(rng, float(lo), float(hi), strength)))


def _resolve(preset: str) -> tuple[str, dict]:
    """Resolve a preset, falling back to DEDUP_DEFAULT_PRESET."""
    name = preset if preset in PRESETS else config.DEDUP_DEFAULT_PRESET
    return name, PRESETS[name]


# Use a separate RNG so optional bitrate variation preserves the existing parameter sequence.
# Enable only explicitly; keep the target within 6% of the existing bitrate baseline.
_VIDEO_BITRATE_SHIFT_PCTS = (-6, -4, -2, 2, 4, 6)


def video_bitrate_for_seed(seed: int, baseline: str) -> tuple[str, int]:
    """Return (libx264 ABR target, percent offset) from a separate deterministic RNG.

    Use the existing VideoInfo bitrate baseline; do not perturb build_params draw order."""
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


# Use a separate RNG for optional pan/zoom; preserve default preset draws.
_KEN_BURNS_DIRECTIONS = ("lr", "rl", "tb", "bt")


def ken_burns_for_seed(seed: int, duration: float) -> dict:
    """Derive slow pan/zoom parameters from a separate deterministic RNG.

    Limit the cycle to actual duration with a 0.05-second floor to avoid division by zero."""
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


def build_params(preset: str, seed: int, *, has_audio: bool = True,
                 width: int = 0, height: int = 0,
                 mirror: bool = False, speed: bool = False,
                 border: bool = False, badge: bool = False,
                 logo: str = "", delogo: str = "") -> dict:
    """Return reproducible parameters for the preset, seed, and caller switches.

    Preset rules still control explicitly enabled effects. Known width/height limit
    cropping to 5% of the shorter side; zero dimensions leave that bound unknown."""
    name, cfg = _resolve(preset)
    rng = random.Random(seed)

    geo = float(cfg["geo"])
    pixel = float(cfg["pixel"])
    audio = float(cfg["audio"])
    encode = float(cfg["encode"])
    notes: list[str] = []

    crop_px = _int_span(rng, config.DEDUP_CROP_RANGE[0],
                        config.DEDUP_CROP_RANGE[1], geo)
    if width > 0 and height > 0:
        cap = max(2, int(min(width, height) * 0.05))
        crop_px = min(crop_px, cap)
    crop_px = min(int(config.DEDUP_CROP_RANGE[1]), crop_px + crop_px % 2)
    zoom = round(1.0 + _span(rng, config.DEDUP_ZOOM_RANGE[0] - 1.0,
                             config.DEDUP_ZOOM_RANGE[1] - 1.0, geo), 4)
    rotate_deg = round(_span(rng, config.DEDUP_ROTATE_DEG[0],
                             config.DEDUP_ROTATE_DEG[1], geo, signed=True), 3)

    # Keep perspective_px=0 for the stable parameter schema; the disabled filter distorted frames.
    perspective_px = 0

    use_mirror = bool(mirror) and rng.random() < float(cfg["mirror"])

    brightness = round(_span(rng, 0.0, config.DEDUP_BRIGHTNESS, pixel,
                             signed=True), 4)
    contrast = round(_span(rng, config.DEDUP_CONTRAST_RANGE[0],
                           config.DEDUP_CONTRAST_RANGE[1], pixel), 4)
    saturation = round(_span(rng, config.DEDUP_SATURATION_RANGE[0],
                             config.DEDUP_SATURATION_RANGE[1], pixel), 4)
    hue_deg = round(_span(rng, 0.0, config.DEDUP_HUE_DEG, pixel, signed=True), 2)

    drift = round(_span(rng, 0.0, config.DEDUP_RGB_DRIFT, pixel), 4)
    gains = tuple(round(1.0 + rng.uniform(-drift, drift), 4) for _ in range(3))
    if all(abs(g - 1.0) < 1e-9 for g in gains):      # Ensure at least one colour channel changes.
        gains = (round(1.0 + drift, 4), 1.0, 1.0)
    rgb_drift = drift

    noise = _int_span(rng, config.DEDUP_NOISE_RANGE[0],
                      config.DEDUP_NOISE_RANGE[1], pixel)
    sharpen = round(_span(rng, config.DEDUP_SHARPEN_RANGE[0],
                          config.DEDUP_SHARPEN_RANGE[1], pixel), 3)

    # Speed requires both preset permission and an explicit caller switch.
    speed_val = 1.0
    if cfg["time"] and speed:
        speed_val = round(_span(rng, config.DEDUP_SPEED_RANGE[0],
                                config.DEDUP_SPEED_RANGE[1], 1.0), 4)

    # Pitch preserves duration; silent inputs retain ratio 1.0.
    pitch = 1.0
    if has_audio and audio > 0:
        pitch = round(_span(rng, config.DEDUP_PITCH_RANGE[0],
                            config.DEDUP_PITCH_RANGE[1], audio), 5)

    if cfg["container"]:
        crf = _int_span(rng, config.DEDUP_CRF_RANGE[0],
                        config.DEDUP_CRF_RANGE[1], encode)
        gop = _int_span(rng, config.DEDUP_GOP_RANGE[0],
                        config.DEDUP_GOP_RANGE[1], encode)
        ab_kbps = _int_span(rng, config.DEDUP_ABITRATE_RANGE[0],
                            config.DEDUP_ABITRATE_RANGE[1], audio)
    else:
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

    # Respect explicit structure switches; otherwise only aggressive may enable them.
    use_border = bool(border) or (bool(cfg["struct"]) and rng.random() < 0.5)
    use_badge = bool(badge) or (bool(cfg["struct"]) and rng.random() < 0.6)
    if (use_border and not border) or (use_badge and not badge):
        notes.append("结构层自动启用（调用方未显式指定）")
    logo_pos = rng.choice(LOGO_POSITIONS)
    if logo and not cfg["struct"]:
        notes.append("画中画已按调用方要求保留")

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
        "crop_px": crop_px,
        "zoom": zoom,
        "rotate_deg": rotate_deg,
        "perspective_px": perspective_px,
        "mirror": use_mirror,

        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "hue_deg": hue_deg,
        "rgb_drift": rgb_drift,
        "channel_gains": gains,
        "noise": noise,
        "sharpen": sharpen,

        "speed": speed_val,

        "pitch": pitch,

        "fps": fps,
        "crf": crf,
        "gop": gop,
        "abitrate": abitrate,
        "encoder_tag": encoder_tag,

        "border": use_border,
        "badge": use_badge,
        "logo": logo,
        "logo_pos": logo_pos,
        "delogo": delogo,

        "preset": name,
        "seed": seed,
        "summary": summary,
    }
