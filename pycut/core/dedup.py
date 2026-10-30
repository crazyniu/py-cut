"""Deduplication filter construction, encoding, verification, and reports.

Build deterministic effects via dedup_presets, encode with libx264 for CRF support,
then check pHash, PSNR damage floor, and duration. Encode to a temporary file and
publish atomically. Normal output can retain a result that fails change thresholds;
in-place replacement requires verification. Scores do not prove platform acceptance."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import config
from .dedup_audio import AudioMixOptions, mix_audio_file, preflight_external_audio
from .dedup_presets import (build_params, derive_seed, ken_burns_for_seed,
                            video_bitrate_for_seed)
from .ffmpeg import FFMPEG, FFPROBE, VideoInfo, probe_scalar, probe_video
from .fingerprint import compare
from .runtime import STOP, check_stop
from .progress import progress
from .utils import fmt_bytes, fmt_dur, log, run_cmd


BORDER_SCALE = 0.8
BORDER_BLUR_SIGMA = 24
BORDER_DARKEN = -0.08

# drawbox uses iw/ih, not overlay W/H variables.
BADGE_RATIO = (0.60, 0.90, 0.32, 0.055)
BADGE_COLOR = "black@0.45"


ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600


DUR_MARGIN = 0.15

# Keep report PSNR finite for JSON consumers.
PSNR_CAP = 99.0


@dataclass
class DedupOptions:
    """Command-independent switches for one deduplication task."""

    preset: str = config.DEDUP_DEFAULT_PRESET
    seed: int | None = None
    mirror: bool = False
    speed: bool = False
    border: bool = False
    badge: bool = False
    vary_bitrate: bool = False
    ken_burns: bool = False
    audio_mix: AudioMixOptions = field(default_factory=AudioMixOptions)
    logo: str = ""
    delogo: str = ""                   # Visible-logo rectangle x,y,w,h for delogo.
    sticker: str = ""
    out_dir: str = ""
    in_place: bool = False
    phash_min: float = 0.0             # Zero selects the preset-specific pHash floor.
    psnr_min: float = config.DEDUP_MIN_PSNR   # Damage floor only; not a perceptual quality score.
    check_only: bool = False
    force: bool = False


@dataclass
class _Report:
    """Report row with stable keys consumed by the command summary."""

    src: str = ""
    dst: str = ""
    ok: bool = False
    seed: int = 0
    preset: str = ""
    params: dict = field(default_factory=dict)
    phash_dist: float = 0.0
    psnr: float = 0.0
    dur_src: float = 0.0
    dur_dst: float = 0.0
    seconds: float = 0.0
    gates: dict = field(default_factory=dict)
    error: str = ""
    skipped: bool = False          # An existing-output skip is neither success nor failure.

    def to_dict(self) -> dict:
        return {
            "src": self.src, "dst": self.dst, "ok": self.ok,
            "skipped": self.skipped,
            "seed": self.seed, "preset": self.preset, "params": self.params,
            "phash_dist": round(float(self.phash_dist), 2),
            "psnr": round(float(self.psnr), 2),
            "dur_src": round(float(self.dur_src), 3),
            "dur_dst": round(float(self.dur_dst), 3),
            "seconds": round(float(self.seconds), 2),
            "gates": self.gates,
            "error": self.error,
            "at": datetime.now().isoformat(timespec="seconds"),
        }


def default_output_path(src: Path) -> Path:
    from .output_paths import flat_output, sibling, source_root
    return flat_output(src, sibling(source_root(src), "去重"))


def resolve_output_path(src: Path, opts: DedupOptions) -> Path:
    from .output_paths import flat_output
    if opts.in_place:
        return src
    if opts.out_dir:
        return flat_output(src, Path(opts.out_dir).expanduser())
    return default_output_path(src)


def _even(v: float, lo: int = 2) -> int:
    """Round down to an even dimension, bounded below by lo."""
    n = int(v)
    n -= n % 2
    return max(lo, n)


def _fps_to_float(v: str) -> float:
    """Parse a decimal or fractional frame rate; return zero on failure."""
    v = (v or "").strip().splitlines()[0].strip() if v else ""
    if not v or v in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in v:
            a, b = v.split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_delogo(spec: str, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Parse and clamp x,y,w,h inside the frame with a one-pixel margin.

    Return None for invalid input; delogo rejects rectangles touching the frame boundary."""
    if not spec:
        return None
    parts = [p for p in str(spec).replace(" ", "").split(",") if p != ""]
    if len(parts) != 4:
        return None
    try:
        x, y, bw, bh = (int(float(p)) for p in parts)
    except (TypeError, ValueError):
        return None
    # delogo needs at least a one-pixel margin inside every frame boundary.
    x = max(1, min(x, w - 2))
    y = max(1, min(y, h - 2))
    bw = max(1, min(bw, w - 1 - x))
    bh = max(1, min(bh, h - 1 - y))
    if bw < 4 or bh < 4:
        return None
    return x, y, bw, bh


def build_video_chain(params: dict, info: VideoInfo) -> list[str] | None:
    """Return the linear video chain, or None when multi-input composition is needed."""
    if params.get("border") or (params.get("logo") or "").strip():
        return None
    return _build_chain(params, info)


def montage_variation(width: int, height: int, seed: int) -> tuple[dict, str]:
    """Draw stronger geometry/pixel effects within the existing calibrated bounds.

    Move magnitudes into the upper half of their ranges; preserve random signs and
    draws. Background, PIP and intro share this policy without changing timing/audio.
    """
    from .dedup_presets import build_params

    params = build_params("standard", seed, has_audio=False,
                          width=width, height=height)
    for key, upper in (("zoom", config.DEDUP_ZOOM_RANGE[1]),
                       ("contrast", config.DEDUP_CONTRAST_RANGE[1]),
                       ("saturation", config.DEDUP_SATURATION_RANGE[1]),
                       ("sharpen", config.DEDUP_SHARPEN_RANGE[1])):
        params[key] = round((params[key] + upper) / 2, 4)
    for key, upper in (("rotate_deg", config.DEDUP_ROTATE_DEG[1]),
                       ("brightness", config.DEDUP_BRIGHTNESS),
                       ("hue_deg", config.DEDUP_HUE_DEG)):
        value = params[key]
        params[key] = round(math.copysign((abs(value) + upper) / 2, value), 4)
    cap = min(config.DEDUP_CROP_RANGE[1], max(2, int(min(width, height) * .05)))
    params["crop_px"] = min(cap, math.ceil((params["crop_px"] + cap) / 4) * 2)
    params["noise"] = math.ceil((params["noise"] + config.DEDUP_NOISE_RANGE[1]) / 2)
    params["channel_gains"] = tuple(round(1 + math.copysign(
        (abs(gain - 1) + config.DEDUP_RGB_DRIFT) / 2, gain - 1), 4)
        for gain in params["channel_gains"])
    params["rgb_drift"] = max(abs(gain - 1) for gain in params["channel_gains"])
    params["preset"] = "montage-strong"
    params["fps"] = ""
    info = VideoInfo(Path("."), "", 0, width, height, False, 0, "")
    chain = ",".join(build_video_chain(params, info))
    # Report only executed geometry/pixel effects; montage does not use audio/encoding fields.
    fields = ("crop_px", "zoom", "rotate_deg", "brightness", "contrast",
              "saturation", "hue_deg", "rgb_drift", "channel_gains",
              "noise", "sharpen", "preset", "seed")
    return {key: params[key] for key in fields}, chain


def _ken_burns_chain(params: dict, info: VideoInfo) -> list[str]:
    """Animate scale(eval=frame) and crop to the original canvas without changing PTS/FPS.

    Avoid zoompan frame-rate changes. Nonnegative crop margins prevent exposed black edges."""
    w, h = int(info.width), int(info.height)
    direction = str(params.get("ken_burns_direction") or "")
    zoom = float(params.get("ken_burns_zoom_start") or 0.0)
    delta = float(params.get("ken_burns_zoom_delta") or 0.0)
    period = float(params.get("ken_burns_period") or 0.0)
    if w < 16 or h < 16 or direction not in ("lr", "rl", "tb", "bt"):
        raise ValueError("Ken Burns 需要有效视频尺寸和方向")
    if not (1.0 < zoom <= 1.1 and 0 < delta <= 0.1 and period >= 0.05
            and math.isfinite(period)):
        raise ValueError("Ken Burns 的缩放幅度或运动周期无效")

    # Cosine easing retains zoom >= 1 and one output frame per input frame.
    zexpr = f"({zoom:.4f}+{delta:.4f}*(1-cos(PI*t/{period:.3f}))/2)"
    sx = f"trunc(iw*{zexpr}/2)*2"
    sy = f"trunc(ih*{zexpr}/2)*2"
    step = f"cos(PI*t/{period:.3f})"
    forward = f"(0.5-0.45*{step})"
    backward = f"(0.5+0.45*{step})"
    fx = forward if direction == "lr" else (backward if direction == "rl" else "0.5")
    fy = forward if direction == "tb" else (backward if direction == "bt" else "0.5")
    return [
        f"scale=w='{sx}':h='{sy}':eval=frame",
        f"crop={w}:{h}:x='(in_w-out_w)*{fx}':y='(in_h-out_h)*{fy}'",
    ]


def _build_chain(params: dict, info: VideoInfo) -> list[str]:
    """Build geometry, pixel, delogo, badge, and timing filters in that order.

    Restore the source canvas after geometry; rotation/zoom crops must not expose black edges.
    The legacy perspective parameter remains inert for report compatibility."""

    W, H = int(info.width), int(info.height)
    chain: list[str] = []
    geo = False

    if params.get("mirror"):
        chain.append("hflip")
        geo = True

    # Rotate then crop the inscribed rectangle to remove exposed black edges.
    # Inscribed dimensions: W*cos(theta)-H*abs(sin(theta)), H*cos(theta)-W*abs(sin(theta)).
    rot_rad = _rotate_rad(params)
    cw, ch = W, H
    if abs(rot_rad) > 1e-6:
        s, c = abs(math.sin(rot_rad)), abs(math.cos(rot_rad))
        iw_ = _even(W * c - H * s)
        ih_ = _even(H * c - W * s)
        # Use a conservative crop if the inscribed rectangle degenerates.
        if iw_ >= 16 and ih_ >= 16:
            chain.append(f"rotate={rot_rad:.6f}:c=black")
            chain.append(f"crop={iw_}:{ih_}:(iw-{iw_})/2:(ih-{ih_})/2")
            cw, ch = iw_, ih_
            geo = True

    # Structural composition resizes later; skip zoom here to avoid a second resampling.
    zoom = float(params.get("zoom") or 1.0)
    if params.get("border") or params.get("logo"):
        zoom = 1.0
    if abs(zoom - 1.0) > 1e-4:
        chain.append(f"scale=iw*{zoom:.5f}:ih*{zoom:.5f}")
        # FFmpeg truncates scale dimensions; rounding can overestimate crop input by one pixel.
        cw, ch = int(cw * zoom), int(ch * zoom)
        geo = True

    # Keep perspective_px inert for old reports; do not restore the frame-distorting filter.
    cpx = int(params.get("crop_px") or 0)
    if cpx > 0:
        tw = _even(min(W - 2 * cpx, cw))
        th = _even(min(H - 2 * cpx, ch))
        chain.append(f"crop={tw}:{th}:(iw-{tw})/2:(ih-{th})/2")
        geo = True
    elif geo:
        tw = _even(min(W, cw))
        th = _even(min(H, ch))
        chain.append(f"crop={tw}:{th}:(iw-{tw})/2:(ih-{th})/2")

    if geo:
        chain.append(f"scale={W}:{H}")

    # Restore source geometry before optional pan/zoom; disabled means no extra filters.
    if params.get("ken_burns_direction"):
        chain.extend(_ken_burns_chain(params, info))

    bright = float(params.get("brightness") or 0.0)
    contrast = float(params.get("contrast") or 1.0)
    sat = float(params.get("saturation") or 1.0)
    if abs(bright) > 1e-4 or abs(contrast - 1.0) > 1e-4 or abs(sat - 1.0) > 1e-4:
        chain.append(
            f"eq=brightness={bright:.5f}:contrast={contrast:.5f}:saturation={sat:.5f}")

    hue = float(params.get("hue_deg") or 0.0)
    if abs(hue) > 1e-3:
        chain.append(f"hue=h={hue:.3f}")

    gains = params.get("channel_gains")
    if isinstance(gains, (list, tuple)) and len(gains) == 3:
        rr, gg, bb = (float(g) for g in gains)
    else:
        drift = float(params.get("rgb_drift") or 0.0)
        rr, gg, bb = 1.0 + drift, 1.0 - drift, 1.0 + drift * 0.5
    if max(abs(rr - 1.0), abs(gg - 1.0), abs(bb - 1.0)) > 1e-4:
        chain.append(f"colorchannelmixer=rr={rr:.5f}:gg={gg:.5f}:bb={bb:.5f}")

    noise = int(params.get("noise") or 0)
    if noise > 0:
        chain.append(f"noise=alls={noise}:allf=t+u")

    # Sharpening and light blur are alternatives; stacking cancels their effect.
    sharpen = float(params.get("sharpen") or 0.0)
    blur = float(params.get("blur") or 0.0)
    if abs(sharpen) > 1e-3:
        chain.append(f"unsharp=luma_amount={sharpen:.3f}"
                     f":luma_msize_x=3:luma_msize_y=3")
    elif abs(blur) > 1e-3:
        chain.append(f"gblur=sigma={blur:.3f}")

    region = _parse_delogo(params.get("delogo") or "", W, H)
    if region:
        x, y, bw, bh = region
        chain.append(f"delogo=x={x}:y={y}:w={bw}:h={bh}:show=0")

    if params.get("badge"):
        rx, ry, rw, rh = BADGE_RATIO
        bx = int(W * rx)
        by = int(H * ry)
        bwid = _even(max(8, int(W * rw)))
        bhei = _even(max(6, int(H * rh)))
        bx = max(0, min(bx, W - bwid))
        by = max(0, min(by, H - bhei))
        chain.append(f"drawbox=x={bx}:y={by}:w={bwid}:h={bhei}"
                     f":color={BADGE_COLOR}:t=fill")

    sp = _speed(params)
    if abs(sp - 1.0) > 1e-4:
        chain.append(f"setpts=PTS/{sp:.5f}")

    fps = str(params.get("fps") or "").strip()
    if fps:
        src_fps = _fps_to_float(probe_scalar(info.path, "stream=avg_frame_rate"))
        if src_fps <= 0 or abs(_fps_to_float(fps) - src_fps) > 0.01:
            chain.append(f"fps={fps}")

    return chain


def _rotate_rad(params: dict) -> float:
    """Return the requested rotation in radians."""
    rad = params.get("rotate_rad")
    if rad is not None:
        try:
            return float(rad)
        except (TypeError, ValueError):
            return 0.0
    deg = params.get("rotate_deg")
    if deg is None:
        return 0.0
    try:
        return math.radians(float(deg))
    except (TypeError, ValueError):
        return 0.0


def _speed(params: dict) -> float:
    """Return playback speed, or 1.0 when disabled or invalid."""
    sp = params.get("speed")
    if sp is None:
        return 1.0
    try:
        sp = float(sp)
    except (TypeError, ValueError):
        return 1.0
    return sp if 0.5 <= sp <= 2.0 else 1.0


def _pitch(params: dict) -> float:
    """Return pitch ratio, or 1.0 when disabled."""
    p = params.get("pitch")
    if p is None:
        return 1.0
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 1.0
    return p if 0.9 <= p <= 1.1 else 1.0


def build_filter_complex(params: dict, info: VideoInfo,
                         logo_path: str | None = None) -> str | None:
    """Return a graph for border, logo, or sticker composition; otherwise return None.

    Apply the linear chain before structural overlays. Input labels must match _build_cmd."""
    logo = (logo_path or params.get("logo") or "").strip()
    border = bool(params.get("border"))
    # A sticker alone still requires filter_complex; include it in the early-return condition.
    sticker = str(params.get("sticker") or "").strip()
    if not border and not logo and not sticker:
        return None

    W, H = int(info.width), int(info.height)
    line = _build_chain(params, info)
    head = ",".join(line)

    parts: list[str] = []
    cur = "dd_src"

    if border:
        parts.append(f"[0:v]{head},split=2[dd_bg][dd_fg]")
        parts.append(
            f"[dd_bg]scale={W}:{H},gblur=sigma={BORDER_BLUR_SIGMA},"
            f"eq=brightness={BORDER_DARKEN}[dd_bgv]")
        parts.append(f"[dd_fg]scale=iw*{BORDER_SCALE}:ih*{BORDER_SCALE}[dd_fgs]")
        parts.append("[dd_bgv][dd_fgs]overlay=(W-w)/2:(H-h)/2[dd_v0]")
        cur = "dd_v0"
    else:
        # An empty linear head must use null; bare adjacent labels are not a valid filter chain.
        parts.append(f"[0:v]{head + ',' if head else ''}null[dd_v0]"
                     if head else "[0:v]null[dd_v0]")
        cur = "dd_v0"

    if logo:
        parts.append("[1:v]format=rgba[dd_lg]")
        lw = _even(max(24, int(W * 0.16)))
        parts.append(f"[dd_lg]scale={lw}:-2[dd_lgs]")
        mx, my = _even(W * 0.03), _even(H * 0.03)
        pos = str(params.get("logo_pos") or "br").lower()
        if pos == "tl":
            off = f"{mx}:{my}"
        elif pos == "tr":
            off = f"W-w-{mx}:{my}"
        elif pos == "bl":
            off = f"{mx}:H-h-{my}"
        else:
            off = f"W-w-{mx}:H-h-{my}"
        parts.append(f"[{cur}][dd_lgs]overlay={off}:format=auto[dd_v1]")
        cur = "dd_v1"

    stk = str(params.get("sticker") or "").strip()
    if stk and Path(stk).is_file():
        sw = _even(max(24, int(W * config.MONTAGE_STICKER_SCALE)))
        fade = max(8, int(sw * 0.45))
        sp = str(params.get("sticker_pos") or "br").lower()
        sm = _even(W * config.MONTAGE_STICKER_MARGIN)
        off = {"tl": f"{sm}:{sm}", "tr": f"W-w-{sm}:{sm}",
               "bl": f"{sm}:H-h-{sm}"}.get(sp, f"W-w-{sm}:H-h-{sm}")
        op = config.MONTAGE_STICKER_OPACITY
        # Average feather axes; multiplying weights makes the sticker nearly invisible.
        fw = (f"(min(1\\,min(X\\,W-1-X)/{fade})"
              f"+min(1\\,min(Y\\,H-1-Y)/{fade}))/2")
        # Input order must match _build_cmd: logo=1, sticker=2 when both are present.
        sidx = 2 if logo else 1
        parts.append(
            f"[{sidx}:v]scale={sw}:-2,format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='alpha(X,Y)*{op}*max(0\\,({fw}))'[dd_sk]")
        parts.append(f"[{cur}][dd_sk]overlay={off}:format=auto[dd_v2]")
        cur = "dd_v2"

    parts.append(f"[{cur}]null[vout]")
    return ";".join(parts)


def build_audio_chain(params: dict) -> list[str]:
    """Build the audio filter chain.

    Pitch uses asetrate, aresample, and inverse atempo to retain duration. Playback
    speed adds its own atempo stage; enabled effects compose in sequence."""
    sr = int(params.get("audio_rate") or config.DEDUP_AUDIO_RATE)
    pitch = _pitch(params)
    speed = _speed(params)

    chain: list[str] = []
    if abs(pitch - 1.0) > 1e-4:
        chain += [f"asetrate={sr}*{pitch:.6f}",
                  f"aresample={sr}",
                  f"atempo={1.0 / pitch:.6f}"]
    if abs(speed - 1.0) > 1e-4:
        chain.append(f"atempo={speed:.6f}")
    return chain


def _build_cmd(src: Path, dst_tmp: Path, params: dict, info: VideoInfo,
               opts: DedupOptions, logo_path: str) -> tuple[list[str], list[str]]:
    """Return (FFmpeg argv, descriptive filter text) for the temporary output."""
    cmd: list[str] = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(src)]
    used: list[str] = []

    if logo_path:
        # Do not loop image inputs forever; overlay repeats a single frame by default.
        cmd += ["-i", str(logo_path)]

    stk_path = str(params.get("sticker") or "").strip()
    if stk_path and Path(stk_path).is_file():
        # Keep image input order synchronized with build_filter_complex.
        cmd += ["-i", stk_path]

    cmd += ["-map_metadata", "-1"]

    fcomplex = build_filter_complex(params, info, logo_path)
    if fcomplex:
        cmd += ["-filter_complex", fcomplex, "-map", "[vout]"]
        used.append(f"filter_complex: {fcomplex}")
    else:
        chain = build_video_chain(params, info) or []
        if chain:
            cmd += ["-vf", ",".join(chain)]
            used.append("-vf: " + ",".join(chain))
        # Any -map disables automatic selection; map video explicitly before optional audio.
        cmd += ["-map", "0:v:0"]

    cmd += ["-map", "0:a?"]

    ac = build_audio_chain(params)
    if info.has_audio and ac:
        cmd += ["-af", ",".join(ac)]
        used.append("-af: " + ",".join(ac))

    # CRF and ABR targets are mutually exclusive; retain the original CRF path when disabled.
    if opts.vary_bitrate:
        bitrate = str(params.get("video_bitrate") or "")
        if not bitrate.endswith("k") or not bitrate[:-1].isdigit() or int(bitrate[:-1]) <= 0:
            raise ValueError(f"无效的 ABR 视频码率目标: {bitrate!r}")
        quality_args = ["-b:v", bitrate]
        used.append(f"视频编码: libx264 ABR 目标={bitrate}（不使用 CRF）")
    else:
        quality_args = ["-crf", str(int(params.get("crf") or config.DEDUP_CRF_RANGE[0]))]
    cmd += [
        "-c:v", "libx264", "-preset", str(config.DEDUP_X264_PRESET),
        *quality_args,
        "-g", str(int(params.get("gop") or config.DEDUP_GOP_RANGE[0])),
        "-pix_fmt", "yuv420p",
    ]
    if info.has_audio:
        # Audio bitrate already includes its k suffix.
        ab = str(params.get("abitrate") or "").strip()
        cmd += ["-c:a", "aac", "-b:a", ab or f"{config.DEDUP_ABITRATE_RANGE[0]}k"]
    else:
        cmd += ["-an"]

    cmd += ["-movflags", "+faststart"]

    # Clear source metadata first, then set the chosen metadata values.
    tag = str(params.get("encoder_tag") or "").strip()
    if tag:
        cmd += ["-metadata", f"title={tag}",
                "-metadata:s:v", f"encoder={tag}"]
        used.append(f"-metadata:s:v encoder={tag}")

    cmd += [str(dst_tmp)]
    return cmd, used


def _dur_tolerance(dur_src: float, speed: float) -> float:
    """Return fixed tolerance plus expected speed-induced duration change and margin.

    Do not divide the fixed tolerance by speed; it covers independent encoding/muxing error."""
    tol = float(config.DEDUP_DUR_TOLERANCE)
    if abs(speed - 1.0) > 1e-4:
        expected = abs(dur_src * (1.0 - 1.0 / speed))
        tol = tol + expected + DUR_MARGIN
    return tol


def _phash_threshold(preset: str) -> float:
    """Return the calibrated preset-specific minimum pHash distance.

    Structural effects alter distances far more than geometry/pixel effects, so a
    single high threshold would reject valid non-structural processing."""
    table = config.DEDUP_MIN_PHASH_BY_PRESET
    name = preset if preset in table else config.DEDUP_DEFAULT_PRESET
    return float(table.get(name, config.DEDUP_MIN_PHASH_DIST))


def _gates(item: _Report, opts: DedupOptions, speed: float) -> dict:
    """Evaluate pHash change, PSNR damage floor, and duration deviation.

    Geometry changes break pixel alignment; PSNR is not a perceptual quality score."""
    tol = _dur_tolerance(item.dur_src, speed)

    need = _phash_threshold(opts.preset) if not opts.phash_min else float(opts.phash_min)
    phash_ok = item.phash_dist >= need
    # Structural rearrangements need their own PSNR floor to avoid false damage reports.
    p = item.params or {}
    structural = bool(p.get("border") or p.get("mirror") or p.get("logo"))
    psnr_floor = opts.psnr_min / 2.0 if structural else opts.psnr_min
    psnr_ok = item.psnr >= psnr_floor
    # Unknown duration is a probe failure, not a measured zero-length output.
    dur_unknown = item.dur_dst <= 0
    dur_ok = True if dur_unknown else abs(item.dur_dst - item.dur_src) <= tol
    gates = {"phash_ok": bool(phash_ok), "psnr_ok": bool(psnr_ok),
             "dur_ok": bool(dur_ok), "dur_tolerance": round(tol, 3),
             "phash_threshold": round(need, 2),
             "psnr_floor": round(psnr_floor, 2), "structural": structural,
             "dur_unknown": dur_unknown,
             "align": "frame"}
    # Above-range hash distances are advisory, not failures.
    span = config.DEDUP_PHASH_EXPECT.get(
        opts.preset if opts.preset in config.DEDUP_PHASH_EXPECT
        else config.DEDUP_DEFAULT_PRESET)
    if span and phash_ok and item.phash_dist > span[1]:
        gates["over_modified"] = True
        gates["phash_expected"] = list(span)
    return gates


def _gate_error(item: _Report, gates: dict, opts: DedupOptions) -> str:
    """Describe failed verification without modifying the output file."""
    bad = []
    if not gates.get("phash_ok"):
        need = gates.get("phash_threshold", config.DEDUP_MIN_PHASH_DIST)
        bad.append(f"改动不足：pHash 距离 {item.phash_dist:.1f}/64 < {need:g}")
    if not gates.get("psnr_ok"):
        floor = gates.get("psnr_floor", opts.psnr_min)
        bad.append(f"画面疑似损坏：PSNR {item.psnr:.1f}dB < {floor:g}dB"
                   f"（几何/结构改动导致 PSNR 偏低属正常，此处只兜底真的糊了/错位了）")
    if not gates.get("dur_ok"):
        tol = gates.get("dur_tolerance", config.DEDUP_DUR_TOLERANCE)
        if gates.get("dur_unknown"):
            bad.append("时长无法探测（容器无该字段且兜底失败），本项跳过判定")
        else:
            bad.append(f"时长偏差 {abs(item.dur_dst - item.dur_src):.2f}s > {tol:.2f}s")
    return "；".join(bad)


def process_one(src: Path, dst: Path, opts: DedupOptions, *, on_publish=None) -> dict:
    """Probe, plan, encode, verify, publish atomically, and return a report dict.

    Convert per-file exceptions into ok=False. Normal output may retain a below-threshold
    result; never replace an input in place unless verification passes."""
    src = Path(src)
    dst = Path(dst)
    item = _Report(src=str(src), dst=str(dst), preset=opts.preset)
    t0 = time.time()

    try:
        check_stop()
        if not src.is_file():
            item.error = f"源文件不存在: {src}"
            return item.to_dict()

        if opts.audio_mix.enabled:
            preflight_external_audio(opts.audio_mix)
        info = probe_video(src)
        if info is None:
            item.error = f"probe 失败（不是可解码的视频？）: {src.name}"
            return item.to_dict()
        # Use video-stream duration consistently on both source and output.
        item.dur_src = _video_duration(src) or float(info.duration)

        seed = int(opts.seed) if opts.seed else int(derive_seed(src))
        item.seed = seed

        params = build_params(
            opts.preset, seed,
            has_audio=bool(info.has_audio),
            width=int(info.width), height=int(info.height),
            mirror=opts.mirror, speed=opts.speed, border=opts.border,
            badge=opts.badge, logo=opts.logo, delogo=opts.delogo,
        )
        # Inject caller-selected stickers outside the stable seeded parameter schema.
        if opts.sticker:
            params["sticker"] = opts.sticker
        params = dict(params or {})
        if opts.vary_bitrate:
            # Optional bitrate targets use existing baselines without changing calibrated constants.
            bitrate, shift = video_bitrate_for_seed(seed, info.bitrate)
            params["video_rate_control"] = "abr"
            params["video_bitrate"] = bitrate
            params["video_bitrate_shift_pct"] = shift
            params["summary"] = str(params.get("summary", "")).replace(
                f"重编码 CRF={params.get('crf')}", f"重编码 ABR={bitrate}")
        if opts.ken_burns:
            motion = ken_burns_for_seed(seed, info.duration)
            params.update(motion)
            params["summary"] = (
                str(params.get("summary", ""))
                + f"；Ken Burns {motion['ken_burns_direction']} "
                  f"{motion['ken_burns_zoom_start']:.4f}"
                  f"+{motion['ken_burns_zoom_delta']:.4f} "
                  f"{motion['ken_burns_period']:.3f}s"
            )
        if opts.audio_mix.enabled:
            # Original 28-key params/report unchanged when audio edit is off.
            params["audio_mix"] = opts.audio_mix.snapshot()
        item.params = params

        if opts.check_only:
            return _check_only(item, src, dst, opts, t0).to_dict()

        if dst.resolve() == src.resolve() and not opts.in_place:
            item.error = "输出路径与源相同（需要 --in-place 才允许覆盖）"
            return item.to_dict()
        if opts.in_place and not opts.force:
            # Require explicit force before replacing a source in place.
            item.error = "原地覆盖必须同时传 --in-place --force，且产物自检达标后才会替换源文件"
            return item.to_dict()
        if not opts.in_place and dst.is_file() and not opts.force:
            item.skipped = True
            item.error = f"已存在同名产物，跳过（--force 可覆盖）: {dst.name}"
            return item.to_dict()

        dst.parent.mkdir(parents=True, exist_ok=True)

        # Process/thread-specific temporary names prevent concurrent encoders from sharing a file.
        tmp = dst.with_name(
            f"{dst.stem}.dedup_tmp{os.getpid()}_"
            f"{threading.get_ident():x}{dst.suffix}")
        cmd, used = _build_cmd(src, tmp, params, info, opts, (params.get("logo") or "").strip())
        for line in used:
            log(f"    {line}")

        timeout = min(ENC_TIMEOUT_MAX,
                      max(ENC_TIMEOUT_MIN, int(info.duration * ENC_TIMEOUT_PER_SEC)))
        try:
            rc, _out, err = run_cmd(cmd, timeout=timeout)
            check_stop()

            if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
                item.error = (f"ffmpeg 失败 rc={rc}: "
                              f"{err.strip().splitlines()[-1] if err.strip() else '无输出'}")
                item.seconds = time.time() - t0
                return item.to_dict()

            if opts.audio_mix.enabled:
                # Audio stream-copy mux verifies temp audio/video streams before
                # publishing any final output. The video filters are untouched.
                checked = mix_audio_file(
                    tmp, opts.audio_mix,
                    abitrate=str(params.get("abitrate") or "128k"),
                    timeout=timeout)
                params["audio_mix"]["verified_output"] = checked

            check_stop()
            # Validate the staged file before publishing in either mode. Default
            # mode still retains a completed but below-threshold result; an
            # interrupted self-check must never replace an earlier good file.
            dw, dh = _probe_size(tmp)
            if dw and (dw != info.width or dh != info.height):
                log(f"    [警告] 输出分辨率 {dw}x{dh} 与源 {info.width}x{info.height} 不一致")
            _self_check(item, src, tmp, opts)
            check_stop()
            if opts.in_place and not item.ok:
                item.error = "原地覆盖已取消（自检未通过）：" + (item.error or "未知原因")
                item.seconds = time.time() - t0
                return item.to_dict()
            os.replace(str(tmp), str(dst))
            if on_publish is not None:
                on_publish(src, dst)
        finally:
            if tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        item.seconds = time.time() - t0
        log(f"    [已产出] {dst.name}  {fmt_bytes(dst.stat().st_size)}  "
            f"{fmt_dur(time.time() - t0)}")

    except InterruptedError as exc:
        item.ok = False
        item.error = str(exc)
        item.seconds = time.time() - t0
    except Exception as e:  # noqa: BLE001 - isolate per-file failures from the rest of the batch.
        item.ok = False
        item.error = f"{type(e).__name__}: {e}"
        item.seconds = time.time() - t0
        log(f"    [异常] {src.name}: {type(e).__name__} {e}")

    return item.to_dict()


def _probe_size(path: Path) -> tuple[int, int]:
    """Return probed (width, height), or (0, 0) on failure."""
    w = probe_scalar(path, "stream=width")
    h = probe_scalar(path, "stream=height")
    try:
        return int(w), int(h)
    except (TypeError, ValueError):
        return 0, 0


def _video_duration(path: Path) -> float:
    """Read video-stream duration in seconds, returning zero when unavailable.

    Container duration is the longest stream and can hide shortened video behind
    unchanged audio. MKV/FLV may return N/A with rc=0; callers must use the fallback
    rather than treating zero as a measured duration."""
    # CSV appends an empty field for ambient-viewing side data on phone footage.
    # Scalar output keeps a numeric duration parseable without discarding metadata.
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
        timeout=30)
    if rc != 0:
        return 0.0
    try:
        return float(out.strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _probe_fallback_duration(path: Path) -> float:
    """Recover missing video duration: frame count/rate, stream duration, then container.

    Frame counting decodes the video and runs only after the fast probe fails.
    Container duration is least reliable when streams differ. Parse the first rate
    line because unselected probes can append audio 0/0; zero rates use the fallback."""
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        timeout=180)
    if rc == 0:
        try:
            frames = int(out.strip().splitlines()[0])
        except (IndexError, TypeError, ValueError):
            frames = 0
        if frames > 0:
            fps = _fps_to_float(probe_scalar(path, "stream=avg_frame_rate")) or 30.0
            return frames / fps

    for value in (probe_scalar(path, "stream=duration"),
                  probe_scalar(path, "format=duration")):
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def _self_check(item: _Report, src: Path, dst: Path, opts: DedupOptions) -> None:
    """Populate fingerprint measurements and gates; a failed check marks ok=False."""
    # Compare video-stream durations on both sides.
    item.dur_dst = _video_duration(dst)
    if item.dur_dst <= 0:
        item.dur_dst = _probe_fallback_duration(dst)

    speed = _speed(item.params)
    # Pair samples by frame index; resampling prevents exact reversal of speed changes.
    item.gates = _gates(item, opts, speed)

    try:
        r = compare(src, dst, config.DEDUP_PHASH_SAMPLES)
        item.phash_dist = float(r.get("phash_dist") or 0.0)
        psnr = float(r.get("psnr") or 0.0)
        item.psnr = min(psnr, PSNR_CAP)   # Clamp infinity for finite JSON report values.
        item.gates = _gates(item, opts, speed)
    except Exception as e:  # noqa: BLE001 - verification failure becomes a failed report row.
        item.gates = _gates(item, opts, speed)
        item.error = f"自检失败: {type(e).__name__}: {e}"
        return

    item.ok = all(item.gates.get(k) for k in ("phash_ok", "psnr_ok", "dur_ok"))
    if not item.ok:
        item.error = _gate_error(item, item.gates, opts)
    log(f"    [{'达标' if item.ok else '不达标'}] pHash {item.phash_dist:.1f}/64"
        f"  PSNR {item.psnr:.1f}dB  时长 {item.dur_src:.2f}→{item.dur_dst:.2f}s"
        + ("  [改动过头]" if item.gates.get("over_modified") else ""))


def _check_only(item: _Report, src: Path, dst: Path, opts: DedupOptions,
                t0: float) -> _Report:
    """Compare the source with its existing output without encoding."""
    if not dst.is_file():
        item.skipped = True
        item.error = f"还没有去重产物可比对: {dst.name}"
        item.seconds = time.time() - t0
        return item
    _self_check(item, src, dst, opts)
    item.seconds = time.time() - t0
    return item


def process_batch(srcs: list[Path], opts: DedupOptions,
                  workers: int = 0, *, on_start=None, on_result=None, on_publish=None) -> list[dict]:
    """Process concurrently, isolate per-file failures, and return rows in input order."""
    srcs = [Path(s) for s in srcs]
    if not srcs:
        return []

    n = workers if workers and workers > 0 else config.DEDUP_WORKERS
    n = max(1, min(n, len(srcs)))

    # Preflight every destination: distinct inputs can otherwise overwrite a shared output.
    destinations: dict[Path, list[int]] = {}
    for i, src in enumerate(srcs):
        dst = resolve_output_path(src, opts).expanduser().resolve()
        destinations.setdefault(dst, []).append(i)
    collided = {
        i: dst
        for dst, indices in destinations.items() if len(indices) > 1
        for i in indices
    }
    if collided:
        log(f"[输出冲突] {len(collided)} 个输入指向重复的输出位置；这些文件不会被处理")

    def _one(pair: tuple[int, Path]) -> dict:
        idx, s = pair
        if STOP.flag:
            return {"src": str(s), "dst": "", "ok": False, "seed": 0,
                    "preset": opts.preset, "params": {}, "phash_dist": 0.0,
                    "psnr": 0.0, "dur_src": 0.0, "dur_dst": 0.0, "seconds": 0.0,
                    "gates": {}, "error": "已中断", "at": datetime.now().isoformat(
                        timespec="seconds")}
        if on_start is not None:
            on_start(s)
        dst = resolve_output_path(s, opts)
        log(f"  [{idx + 1}/{len(srcs)}] {s.name} -> {dst.name}")
        try:
            return process_one(s, dst, opts, **({"on_publish": on_publish} if on_publish is not None else {}))
        except Exception as e:  # noqa: BLE001 - process_one normally converts per-file exceptions into report rows.
            log(f"  [异常] {s.name}: {type(e).__name__} {e}")
            return {"src": str(s), "dst": str(dst), "ok": False, "seed": 0,
                    "preset": opts.preset, "params": {}, "phash_dist": 0.0,
                    "psnr": 0.0, "dur_src": 0.0, "dur_dst": 0.0, "seconds": 0.0,
                    "gates": {}, "error": f"{type(e).__name__}: {e}",
                    "at": datetime.now().isoformat(timespec="seconds")}

    # Carry explicit input indices through concurrent completion to preserve pairing.
    out: list[dict] = [{} for _ in srcs]
    for i, dst in collided.items():
        log(f"  [拒绝覆盖] {srcs[i]} -> {dst}")
        out[i] = _Report(
            src=str(srcs[i]), dst=str(dst), preset=opts.preset,
            error=f"输出路径冲突（多个源文件会写入同一位置）: {dst}",
        ).to_dict()
    remaining = [(i, s) for i, s in enumerate(srcs) if i not in collided]
    if on_result is not None:
        for i in collided:
            on_result(out[i])
    if not remaining:
        return out
    completed = len(collided)
    progress("去重", completed, len(srcs), failed=len(collided), detail="编码扰动 → pHash / PSNR 自检")
    with futures.ThreadPoolExecutor(max_workers=min(n, len(remaining))) as ex:
        futs = {ex.submit(_one, (i, s)): i for i, s in remaining}
        for fut in futures.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[i] = {"src": str(srcs[i]), "dst": "", "ok": False, "seed": 0,
                          "preset": opts.preset, "params": {}, "phash_dist": 0.0,
                          "psnr": 0.0, "dur_src": 0.0, "dur_dst": 0.0,
                          "seconds": 0.0, "gates": {},
                          "error": f"{type(e).__name__}: {e}"}
            if on_result is not None:
                on_result(out[i])
            completed += 1
            good = sum(bool(item.get("ok")) for item in out if item)
            failed = sum(not item.get("ok") and not item.get("skipped") for item in out if item)
            metrics = f"pHash {(out[i].get('phash_dist') or 0):.2f} · PSNR {(out[i].get('psnr') or 0):.1f} dB"
            detail = ("已有产物跳过，本次未执行扰动" if out[i].get("skipped") else
                      ("自检达标：" if out[i].get("ok") else "处理或自检失败：") + metrics)
            if out[i].get("gates", {}).get("over_modified"):
                detail = "变化偏大，请目检；" + metrics
            progress("去重", completed, len(srcs), success=good, failed=failed, skipped=sum(bool(item.get("skipped")) for item in out if item), detail=detail, output=str(resolve_output_path(srcs[i], opts).parent))
    return out


def write_report(items: list[dict], report_dir: Path = None) -> Path:
    """Write a UTF-8 BOM JSON report and return its path."""
    d = Path(report_dir) if report_dir else Path(config.DEDUP_REPORT)
    d.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = d / f"去重报告_{stamp}.json"

    ok = sum(1 for i in items if i.get("ok"))
    # Report existing-output skips separately from successes and failures.
    skipped = sum(1 for i in items if i.get("skipped"))
    summary = {
        "total": len(items),
        "ok": ok,
        "failed": sum(1 for i in items
                      if not i.get("ok") and not i.get("skipped")),
        "skipped": skipped,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    dists = [i["phash_dist"] for i in items
             if isinstance(i.get("phash_dist"), (int, float))]
    psnrs = [i["psnr"] for i in items if isinstance(i.get("psnr"), (int, float))]
    if dists:
        summary["phash_avg"] = round(sum(dists) / len(dists), 2)
    if psnrs:
        summary["psnr_avg"] = round(sum(psnrs) / len(psnrs), 2)

    payload = {"summary": summary, "items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8-sig")
    return path
