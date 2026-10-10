"""视频去重（过平台查重）核心：滤镜链构造 + 单文件处理编排。

分层扰动（需求见 docs/DEDUP.md 第 3 节 M1-M8）：
    容器/元数据 → 几何构图 → 像素色彩 → 时间帧序列 → 结构内容 → 音频
每条视频由 seed 决定一份参数组合（`core/dedup_presets.py`），本模块只负责：
    1. 把参数组合翻译成 ffmpeg 滤镜链（几何部分**必须**保证输出尺寸与源一致且无黑边）
    2. 调 ffmpeg 重编码（写 .tmp 再 os.replace 原子替换，参照 core/subtitle.py）
    3. 用 `core/fingerprint.py` 自检改动幅度（pHash 距离）与画质损失（PSNR）
    4. 出JSON 报告

自检不通过（改得不够 / 画质损失过大）时**文件仍然保留**，只把 ok 置 False——
去重没有「绝对通过」，只能量化「改了多少、损失多少」，取舍由使用者决定。

编码器说明：本机 ffmpeg 9.0.2 full build 的 `h264_videotoolbox` 只支持码率模式、
**没有 CRF**，而去重必须做 CRF 随机化（M1），所以视频统一走 `libx264`。
"""
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
from .runtime import STOP
from .utils import fmt_bytes, fmt_dur, log, run_cmd

# 模糊边框：前景缩到 80% 居中，背景同片放大铺满 + 高斯模糊
BORDER_SCALE = 0.8
BORDER_BLUR_SIGMA = 24
BORDER_DARKEN = -0.08# 背景压暗一点，视觉上更像刻意做的边框

# 角标：右下角色块（坐标按输出尺寸算，不可用 drawbox 的 W/H 常量，只认 iw/ih）
BADGE_RATIO = (0.60, 0.90, 0.32, 0.055)# x, y, w, h （占宽/高比例）
BADGE_COLOR = "black@0.45"

# 编码超时：按时长估算，短片2分钟足够；长片放宽
ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600

# 时长门禁余量（秒）
DUR_MARGIN = 0.15

# 报告里的 psnr 上限（两片完全相同时 PSNR=inf，JSON 不可序列化 / Excel 打不开）
PSNR_CAP = 99.0


@dataclass
class DedupOptions:
    """一次去重任务的全部开关（命令层按此填充）。"""

    preset: str = config.DEDUP_DEFAULT_PRESET
    seed: int | None = None
    mirror: bool = False
    speed: bool = False
    border: bool = False
    badge: bool = False
    vary_bitrate: bool = False         # M1：默认 CRF，显式启用时使用 seed 派生 ABR 目标
    ken_burns: bool = False            # M2：默认关闭动态缩放/平移，不改现有滤镜
    audio_mix: AudioMixOptions = field(default_factory=AudioMixOptions)
    logo: str = ""
    delogo: str = ""                   # 可见水印位置 "x,y,w,h"，走 delogo 滤镜
    sticker: str = ""                   # 贴纸图片路径（背面半透明 + 边缘羽化）
    out_dir: str = ""                  # 空 = 用 config.DEDUP_OUT 默认目录
    in_place: bool = False             # 原地覆盖（危险，默认 False）
    phash_min: float = 0.0             # 0 = 按档位自动取 config.DEDUP_MIN_PHASH_BY_PRESET
    psnr_min: float = config.DEDUP_MIN_PSNR   # 灾难下限，非质量指标（见 config 标定注释）
    check_only: bool = False           # 只自检不产出（对比 src 与「已存在的去重产物」）
    force: bool = False


@dataclass
class _Report:
    """报告条目（dict 字段固定，命令层按这些 key 汇总）。"""

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
    skipped: bool = False          # 产物已存在而跳过：既不是成功也不算失败

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


# ---------------------------------------------------------------------------
# 输出路径
# ---------------------------------------------------------------------------
def default_output_path(src: Path) -> Path:
    """默认输出路径。

    src 位于桌面切片桶（切片_有人脸 / 切片_无人脸）里时，
    输出到 `config.DEDUP_OUT/<视频名>/<桶内子目录>/xxx.mp4`（保留桶内的层级结构）；
    其它情况输出到 `config.DEDUP_OUT/<src 父目录名>/<stem>_去重.mp4`。
    """
    src = Path(src)
    out_root = Path(opts_out_root())

    for bucket in (config.OUT_FACE, config.OUT_NOFACE):
        try:
            rel = src.resolve().relative_to(Path(bucket).resolve())
        except (ValueError, OSError):
            continue
        # rel = <视频名>[/<子目录>...]/<文件名>；整体搬到 DEDUP_OUT 下
        return out_root / rel

    return out_root / src.parent.name / f"{src.stem}_去重{src.suffix}"


def opts_out_root() -> Path:
    """默认输出根目录（未显式指定 out_dir 时）。"""
    return Path(config.DEDUP_OUT)


def resolve_output_path(src: Path, opts: DedupOptions) -> Path:
    """按opts 算出最终输出路径：in_place 覆盖源文件，out_dir 只换根目录。"""
    src = Path(src)
    if opts.in_place:
        return src
    if opts.out_dir:
        rel = default_output_path(src).relative_to(Path(opts_out_root()))
        return Path(opts.out_dir).expanduser() / rel
    return default_output_path(src)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _even(v: float, lo: int = 2) -> int:
    """向下取偶数（h264 要求宽高为偶），并保证不小于 lo。"""
    n = int(v)
    n -= n % 2
    return max(lo, n)


def _fps_to_float(v: str) -> float:
    """'30000/1001' / '30' / '29.97' -> float，失败返回 0。"""
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
    """解析并**夹紧** delogo 区域。

    delogo 对越界（含 x=0 / y=0）会直接报 "Logo area is outside of the frame"，
    这里把坐标夹到 [1, W-1] × [1, H-1] 内，解析不了返回 None（不加该滤镜）。
    """
    if not spec:
        return None
    parts = [p for p in str(spec).replace(" ", "").split(",") if p != ""]
    if len(parts) != 4:
        return None
    try:
        x, y, bw, bh = (int(float(p)) for p in parts)
    except (TypeError, ValueError):
        return None
    # delogo 要求四周至少留1px，且整块落在画面内
    x = max(1, min(x, w - 2))
    y = max(1, min(y, h - 2))
    bw = max(1, min(bw, w - 1 - x))
    bh = max(1, min(bh, h - 1 - y))
    if bw < 4 or bh < 4:
        return None
    return x, y, bw, bh


# ---------------------------------------------------------------------------
# 视频滤镜链
# ---------------------------------------------------------------------------
def build_video_chain(params: dict, info: VideoInfo) -> list[str] | None:
    """按 params 构造视频滤镜链（-vf，逐级串联的列表）。

    需要多路视频（模糊边框 / 画中画）时返回 None，改用 build_filter_complex()。
    """
    if params.get("border") or (params.get("logo") or "").strip():
        return None
    return _build_chain(params, info)


def _ken_burns_chain(params: dict, info: VideoInfo) -> list[str]:
    """逐帧放大后裁回原画幅；不改变原始 PTS/FPS 或音轨。

    不使用 zoompan（它可引入固定输出 fps/帧数变化），而用
    scale(eval=frame) 的 t 动画再 crop 固定 W×H。
    两方向余量始终非负，crop 坐标位于 [5%,95%]，不会露出黑边。
    """
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

    # 余弦缓入缓出，缩放倍率始终 >=1；过滤器 eval=frame 保持 1 输入帧 → 1 输出帧。
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
    """线性滤镜链（单路视频即可完成的部分）。

    顺序：几何(镜像/旋转/缩放/裁剪/梯形) -> 像素(eq/hue/colorchannelmixer/noise/
    unsharp) -> delogo(可见水印) -> 结构(角标) -> 时间(setpts/fps)。

    几何部分结束后**必定**是 `scale=W:H`，保证输出分辨率与源完全一致；
    旋转/缩放先扩画再裁回，绝不输出黑边。
    """

    W, H = int(info.width), int(info.height)
    chain: list[str] = []
    geo = False

    # --- 镜像（几何最前，后续裁剪保持对称）---
    if params.get("mirror"):
        chain.append("hflip")
        geo = True

    # --- 微旋转：先扩画（rotate 默认把画布扩到能容下整幅），再裁内接矩形 ---
    #   内接矩形： w' = W·cosθ − H·|sinθ| ,  h' = H·cosθ − W·|sinθ|
    #   裁掉的就是旋转露出的黑边。
    rot_rad = _rotate_rad(params)
    cw, ch = W, H
    if abs(rot_rad) > 1e-6:
        s, c = abs(math.sin(rot_rad)), abs(math.cos(rot_rad))
        iw_ = _even(W * c - H * s)
        ih_ = _even(H * c - W * s)
        #内接矩形退化时（角度异常大）退回按 96% 裁，至少不黑边
        if iw_ >= 16 and ih_ >= 16:
            chain.append(f"rotate={rot_rad:.6f}:c=black")
            chain.append(f"crop={iw_}:{ih_}:(iw-{iw_})/2:(ih-{ih_})/2")
            cw, ch = iw_, ih_
            geo = True

    # --- 微缩放：放大后由末尾统一裁回 ---
    # 结构层（模糊边框/画中画）内部还要再缩一次，这里若再放大就是**二次重采样**，
    # 白白损失清晰度（实测约 1.2%）。两者同时启用时以结构层为主，放弃微缩放。
    zoom = float(params.get("zoom") or 1.0)
    if params.get("border") or params.get("logo"):
        zoom = 1.0
    if abs(zoom - 1.0) > 1e-4:
        chain.append(f"scale=iw*{zoom:.5f}:ih*{zoom:.5f}")
        cw, ch = int(round(cw * zoom)), int(round(ch * zoom))
        geo = True

    # --- 梯形微变形：**已停用**，详见下方「为什么不用 perspective」---
    # 参数键保留（读旧报告不报错），但不再生成该滤镜。
    # 停用原因（2026-10-09 实测）：ffmpeg 9 的 perspective 不带 s= 时输出尺寸
    # 与输入不一致，四角坐标会把画面径向拉花成细长条（已抽帧确认是真实损坏，
    # 不是度量失真）；而它的去重收益实测只有 0.42~0.75 pHash，收益极小。
    # 宁可不加，也不用一个会毁画面的滤镜。

    # --- 边缘微裁：几何改动的最后一步，把画布收回 W×H ---
    cpx = int(params.get("crop_px") or 0)
    if cpx > 0:
        tw = _even(min(W - 2 * cpx, cw))
        th = _even(min(H - 2 * cpx, ch))
        chain.append(f"crop={tw}:{th}:(iw-{tw})/2:(ih-{th})/2")
        geo = True
    elif geo:
        # 无微裁但几何改过（只缩放/只梯形）：把放大出来的边缘裁回原尺寸
        tw = _even(min(W, cw))
        th = _even(min(H, ch))
        chain.append(f"crop={tw}:{th}:(iw-{tw})/2:(ih-{th})/2")

    if geo:
        chain.append(f"scale={W}:{H}")

    # 现有几何改动先归位到源尺寸，再施加 Ken Burns；不开关时零额外滤镜。
    if params.get("ken_burns_direction"):
        chain.extend(_ken_burns_chain(params, info))

    # --- 像素 / 色彩层 ---
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

    # 锐化与极轻模糊二选一（叠加会互相抵消，见 AGENTS.md）
    sharpen = float(params.get("sharpen") or 0.0)
    blur = float(params.get("blur") or 0.0)
    if abs(sharpen) > 1e-3:
        chain.append(f"unsharp=luma_amount={sharpen:.3f}"
                     f":luma_msize_x=3:luma_msize_y=3")
    elif abs(blur) > 1e-3:
        chain.append(f"gblur=sigma={blur:.3f}")

    # --- 可见水印（坐标基于已归位的 W×H）---
    region = _parse_delogo(params.get("delogo") or "", W, H)
    if region:
        x, y, bw, bh = region
        chain.append(f"delogo=x={x}:y={y}:w={bw}:h={bh}:show=0")

    # --- 结构层：角标 ---
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

    # --- 时间层：变速（音频侧同步 atempo）---
    sp = _speed(params)
    if abs(sp - 1.0) > 1e-4:
        chain.append(f"setpts=PTS/{sp:.5f}")

    # --- 帧率微调：与源帧率一致时跳过，避免无谓重采样 ---
    fps = str(params.get("fps") or "").strip()
    if fps:
        src_fps = _fps_to_float(probe_scalar(info.path, "stream=avg_frame_rate"))
        if src_fps <= 0 or abs(_fps_to_float(fps) - src_fps) > 0.01:
            chain.append(f"fps={fps}")

    return chain


def _rotate_rad(params: dict) -> float:
    """取旋转角度（弧度）。params 给的是度就换算。"""
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
    """取变速倍率；未启用 / 解析不了返回 1.0。"""
    sp = params.get("speed")
    if sp is None:
        return 1.0
    try:
        sp = float(sp)
    except (TypeError, ValueError):
        return 1.0
    return sp if 0.5 <= sp <= 2.0 else 1.0


def _pitch(params: dict) -> float:
    """取变调倍率；未启用返回 1.0。"""
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
    """构造 filter_complex 字符串（不含 `-filter_complex` 参数名本身）。

    模糊边框与画中画都需要多路视频，必须走 filter_complex。
    线性链（几何/像素/delogo/角标）由 build_video_chain 生成后接在源上，
    结构层再叠上去；没有这两项时返回 None（调用方改用 -vf）。
    """
    logo = (logo_path or params.get("logo") or "").strip()
    border = bool(params.get("border"))
    # 贴纸也是多路输入（图片要单独 -i 再overlay），所以同样必须走 filter_complex。
    # **早退条件必须把它算进去**：否则「只有贴纸、没有边框/画中画」的组合
    # （例如 standard 档 + --sticker）会直接返回 None，贴纸被静默跳过——
    # 实测踩过：命令跑成功、报告正常，但画面上根本没有贴纸。
    sticker = str(params.get("sticker") or "").strip()
    if not border and not logo and not sticker:
        return None

    W, H = int(info.width), int(info.height)
    line = _build_chain(params, info)
    head = ",".join(line)

    parts: list[str] = []
    cur = "dd_src"

    if border:
        # 背景：同片铺满 + 高斯模糊 + 压暗；前景：缩到 80% 居中
        parts.append(f"[0:v]{head},split=2[dd_bg][dd_fg]")
        parts.append(
            f"[dd_bg]scale={W}:{H},gblur=sigma={BORDER_BLUR_SIGMA},"
            f"eq=brightness={BORDER_DARKEN}[dd_bgv]")
        parts.append(f"[dd_fg]scale=iw*{BORDER_SCALE}:ih*{BORDER_SCALE}[dd_fgs]")
        parts.append("[dd_bgv][dd_fgs]overlay=(W-w)/2:(H-h)/2[dd_v0]")
        cur = "dd_v0"
    else:
        # 无边框：主画面不需要分路（多余的分路会产生没人用的输出 pad）。
        # head 可能为空串（几何/像素层全关时），此时**必须走 null**，
        # 否则会拼出裸标签 `[0:v][dd_v0]`——ffmpeg 报
        # "No such filter: ''"（实测踩过：standard 档 + 只开贴纸就是这个坑）。
        parts.append(f"[0:v]{head + ',' if head else ''}null[dd_v0]"
                     if head else "[0:v]null[dd_v0]")
        cur = "dd_v0"

    if logo:
        # 画中画：logo 作为第 2 路输入（输入序号 1），缩到宽度的 16% 贴到指定角
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
        else:                      # br（默认）
            off = f"W-w-{mx}:H-h-{my}"
        parts.append(f"[{cur}][dd_lgs]overlay={off}:format=auto[dd_v1]")
        cur = "dd_v1"

    # 贴纸（静态图片干扰层）：与画中画同层级，但**背面半透明 + 边缘羽化**，
    # 视觉上融进画面而不是硬贴上去。素材路径由 params['sticker'] 给。
    stk = str(params.get("sticker") or "").strip()
    if stk and Path(stk).is_file():
        sw = _even(max(24, int(W * config.MONTAGE_STICKER_SCALE)))
        fade = max(8, int(sw * 0.45))
        sp = str(params.get("sticker_pos") or "br").lower()
        sm = _even(W * config.MONTAGE_STICKER_MARGIN)
        off = {"tl": f"{sm}:{sm}", "tr": f"W-w-{sm}:{sm}",
               "bl": f"{sm}:H-h-{sm}"}.get(sp, f"W-w-{sm}:H-h-{sm}")
        op = config.MONTAGE_STICKER_OPACITY
        # 羽化权重取两方向**平均**而非连乘：连乘会让「距边 100px、带宽 90px」
        # 处权重只剩 0.55，再乘不透明度就几乎看不见贴纸（实测踩过）。
        fw = (f"(min(1\\,min(X\\,W-1-X)/{fade})"
              f"+min(1\\,min(Y\\,H-1-Y)/{fade}))/2")
        # 输入序号：logo 占 1，故贴纸在有 logo 时是 2，否则是 1。
        # （与 _build_cmd 里 -i 的追加顺序严格对应，改一处必须改另一处）
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
    """音频滤镜链（-af，逗号串联）。

    变调保持时长用标准组合：`asetrate={sr}*{pitch}, aresample={sr}, atempo={1/pitch}`
    （asetrate 改播放速率 → 变调变快，atempo 补回原速，时长不变）。
    变速用 `atempo={speed}`；两者同时启用时两个 atempo 串联。
    """
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


# ---------------------------------------------------------------------------
# 编码命令
# ---------------------------------------------------------------------------
def _build_cmd(src: Path, dst_tmp: Path, params: dict, info: VideoInfo,
               opts: DedupOptions, logo_path: str) -> tuple[list[str], list[str]]:
    """拼 ffmpeg 命令行，返回 (cmd, 说明用的滤镜链文本)。"""
    cmd: list[str] = [FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-i", str(src)]
    used: list[str] = []

    if logo_path:
        # logo 放在 -i src 之后 -> 输入序号 1，供 filter_complex 的 [1:v] 引用。
        # **不要加 -loop 1**：图片输入会被无限循环，ffmpeg 永不结束（实测挂死）。
        # 单帧图片 + overlay 默认 eof_action=repeat 会一直保持最后一帧，够用。
        cmd += ["-i", str(logo_path)]

    stk_path = str(params.get("sticker") or "").strip()
    if stk_path and Path(stk_path).is_file():
        # 贴纸占下一个输入序号；与 logo 同时存在时 logo=1、贴纸=2，
        # build_filter_complex 里按同一顺序取 [1:v] / [2:v]（两者同时启用才需要 2）。
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
        # 必须显式映射视频：一旦出现任何 -map，ffmpeg 就停止自动选流，
        # 只写 -map 0:a? 会让**视频流整个消失**（产物只剩音频，已实测）。
        cmd += ["-map", "0:v:0"]

    cmd += ["-map", "0:a?"]

    ac = build_audio_chain(params)
    if info.has_audio and ac:
        cmd += ["-af", ",".join(ac)]
        used.append("-af: " + ",".join(ac))

    # CRF 与 ABR 码率目标互斥；不在同一条 libx264 命令中同时设置。
    # 关闭开关时严格复用原来的 CRF 参数和顺序。
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
        # abitrate 已是 '105k' 这样的字符串（preset 生成），别再拼k
        ab = str(params.get("abitrate") or "").strip()
        cmd += ["-c:a", "aac", "-b:a", ab or f"{config.DEDUP_ABITRATE_RANGE[0]}k"]
    else:
        cmd += ["-an"]

    cmd += ["-movflags", "+faststart"]

    # -map_metadata -1 已在前，这里追加的 -metadata 会覆盖它（顺序：先清空后写）
    tag = str(params.get("encoder_tag") or "").strip()
    if tag:
        cmd += ["-metadata", f"title={tag}",
                "-metadata:s:v", f"encoder={tag}"]
        used.append(f"-metadata:s:v encoder={tag}")

    cmd += [str(dst_tmp)]
    return cmd, used


def _dur_tolerance(dur_src: float, speed: float) -> float:
    """时长门禁容差。启用了变速就按 speed 放宽。

    公式：`DEDUP_DUR_TOLERANCE + d×(1−1/speed) + DUR_MARGIN`，**固定项不除以 speed**。
    依据（preset-dev 在 39.8s + 6 档×3 seed 实测）：变速本身不引入额外容器误差，
    偏差几乎全部来自预期的 `d×(1−1/speed)`，所以固定项要留满；实测余量
    0.38~0.56s，稳定覆盖。若写成 `tol/speed`（speed=1.05 时 0.35→0.33）
    余量会掉到 0.18~0.37s，长切片有误判风险。
    """
    tol = float(config.DEDUP_DUR_TOLERANCE)
    if abs(speed - 1.0) > 1e-4:
        expected = abs(dur_src * (1.0 - 1.0 / speed))
        tol = tol + expected + DUR_MARGIN
    return tol


def _phash_threshold(preset: str) -> float:
    """按档位取 pHash 距离下限（实测标定值，见 config.DEDUP_MIN_PHASH_BY_PRESET）。

    分档的原因：几何/像素层改动只能把距离推到 3~8，只有结构层（模糊边框/镜像）
    才到 18~21，用单一高门槛会把所有非结构层处理误判成「改动不足」。
    """
    table = config.DEDUP_MIN_PHASH_BY_PRESET
    name = preset if preset in table else config.DEDUP_DEFAULT_PRESET
    return float(table.get(name, config.DEDUP_MIN_PHASH_DIST))


def _gates(item: _Report, opts: DedupOptions, speed: float) -> dict:
    """自检门禁：pHash 距离（分档） / PSNR（灾难下限） / 时长偏差。

    PSNR 只当下限用，不当质量指标：一旦做几何改动，逐像素对齐必然崩，
    实测 17~25dB 属正常观感。详见 config 里的标定注释。
    """
    tol = _dur_tolerance(item.dur_src, speed)
    # 变速时按时间对齐自检；未显式覆盖 phash_min 时按档位取
    need = _phash_threshold(opts.preset) if not opts.phash_min else float(opts.phash_min)
    phash_ok = item.phash_dist >= need
    # 灾难下限按「有没有做结构层」区分：模糊边框/镜像会把画面重排，
    # PSNR 必然掉到 13~17dB（实测抽帧确认画面完好），再用统一阈值会误报损坏。
    p = item.params or {}
    structural = bool(p.get("border") or p.get("mirror") or p.get("logo"))
    psnr_floor = opts.psnr_min / 2.0 if structural else opts.psnr_min
    psnr_ok = item.psnr >= psnr_floor
    # 时长取不到（容器无该字段且兜底也失败）时，0 会被算成「偏差 = 整片时长」，
    # 报「时长不达标」是误导——真实原因是探测失败。故单列一档并从判定中剔除。
    dur_unknown = item.dur_dst <= 0
    dur_ok = True if dur_unknown else abs(item.dur_dst - item.dur_src) <= tol
    gates = {"phash_ok": bool(phash_ok), "psnr_ok": bool(psnr_ok),
             "dur_ok": bool(dur_ok), "dur_tolerance": round(tol, 3),
             "phash_threshold": round(need, 2),
             "psnr_floor": round(psnr_floor, 2), "structural": structural,
             "dur_unknown": dur_unknown,
             "align": "frame"}
    # 超出期望区间上限 = 改动过头（常见于开了结构层/镜像），只提示不判失败
    span = config.DEDUP_PHASH_EXPECT.get(
        opts.preset if opts.preset in config.DEDUP_PHASH_EXPECT
        else config.DEDUP_DEFAULT_PRESET)
    if span and phash_ok and item.phash_dist > span[1]:
        gates["over_modified"] = True
        gates["phash_expected"] = list(span)
    return gates


def _gate_error(item: _Report, gates: dict, opts: DedupOptions) -> str:
    """门禁未过时给一句人话原因（不改文件，只标ok=False）。"""
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


# ---------------------------------------------------------------------------
# 单文件处理
# ---------------------------------------------------------------------------
def process_one(src: Path, dst: Path, opts: DedupOptions) -> dict:
    """处理单个视频，返回报告条目dict。

    流程：探测 → 生成参数 → 构造滤镜 → ffmpeg 编码（.tmp + os.replace 原子替换）
    → 自检（pHash 距离 / PSNR）→ 报告。

    **任何异常都不抛到批处理层**，统一捕获后返回 ok=False 的条目。
    自检不达标时文件仍然保留，只把 ok 置 False。
    """
    src = Path(src)
    dst = Path(dst)
    item = _Report(src=str(src), dst=str(dst), preset=opts.preset)
    t0 = time.time()

    try:
        if not src.is_file():
            item.error = f"源文件不存在: {src}"
            return item.to_dict()

        if opts.audio_mix.enabled:
            preflight_external_audio(opts.audio_mix)
        info = probe_video(src)
        if info is None:
            item.error = f"probe 失败（不是可解码的视频？）: {src.name}"
            return item.to_dict()
        # 时长口径与产物侧对齐：都取视频流时长，不混用 format=duration
        # （probe_video 内部的 duration 取的是容器最长流，见 _video_duration 注释）
        item.dur_src = _video_duration(src) or float(info.duration)

        # --- seed：显式 seed 优先，否则按源路径派生（同 seed 结果可复现）---
        seed = int(opts.seed) if opts.seed else int(derive_seed(src))
        item.seed = seed

        params = build_params(
            opts.preset, seed,
            has_audio=bool(info.has_audio),
            width=int(info.width), height=int(info.height),
            mirror=opts.mirror, speed=opts.speed, border=opts.border,
            badge=opts.badge, logo=opts.logo, delogo=opts.delogo,
        )
        # 贴纸不在 build_params 的 28 键契约里（那是「按 seed 随机化」的参数集，
        # 而贴纸是调用方显式指定的路径），在这里注入，供 filter_complex 消费。
        if opts.sticker:
            params["sticker"] = opts.sticker
        params = dict(params or {})
        if opts.vary_bitrate:
            # 视频基础目标来自已有 VideoInfo/config，不改标定常量。
            # 只在显式开启时往报告参数加入 ABR 信息；默认 28 键契约不变。
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

        # --- 只自检模式：不产出，比对 src 与「已存在的去重产物」---
        if opts.check_only:
            return _check_only(item, src, dst, opts, t0).to_dict()

        if dst.resolve() == src.resolve() and not opts.in_place:
            item.error = "输出路径与源相同（需要 --in-place 才允许覆盖）"
            return item.to_dict()
        if opts.in_place and not opts.force:
            # 原地覆盖属于不可撤销操作，要求调用方明确传 --force。
            item.error = "原地覆盖必须同时传 --in-place --force，且产物自检达标后才会替换源文件"
            return item.to_dict()
        if not opts.in_place and dst.is_file() and not opts.force:
            item.skipped = True
            item.error = f"已存在同名产物，跳过（--force 可覆盖）: {dst.name}"
            return item.to_dict()

        dst.parent.mkdir(parents=True, exist_ok=True)

        # --- 编码：先写 .tmp，成功后 os.replace 原子替换 ---
        #tmp 名带 pid+线程 id：同一批里若出现重复源路径（resolve_output_path
        # 会把它们解析到同一个 dst），固定名会让两个 ffmpeg 抢写同一文件，
        # 结果是产物损坏（实测 ffprobe 报 N/A）。
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

            if opts.in_place:
                # 必须趁 src 尚未被覆盖时与临时产物比较。先替换再
                # compare(src, dst) 等价于自己比自己，pHash 永远趋近 0。
                _self_check(item, src, tmp, opts)
                if not item.ok:
                    item.error = "原地覆盖已取消（自检未通过）：" + (item.error or "未知原因")
                    item.seconds = time.time() - t0
                    return item.to_dict()
            os.replace(str(tmp), str(dst))
        finally:
            if tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        item.seconds = time.time() - t0
        log(f"    [已产出] {dst.name}  {fmt_bytes(dst.stat().st_size)}  "
            f"{fmt_dur(time.time() - t0)}")

        # --- 规格校验：分辨率必须与源一致 ---
        dw, dh = _probe_size(dst)
        if dw and (dw != info.width or dh != info.height):
            log(f"    [警告] 输出分辨率 {dw}x{dh} 与源 {info.width}x{info.height} 不一致")

        # --- 自检：原地覆盖已在写入前校验，不能再与自身比较 ---
        if not opts.in_place:
            _self_check(item, src, dst, opts)

    except Exception as e:  # noqa: BLE001 单文件失败不拖垮整批
        item.error = f"{type(e).__name__}: {e}"
        item.seconds = time.time() - t0
        log(f"    [异常] {src.name}: {type(e).__name__} {e}")

    return item.to_dict()


def _probe_size(path: Path) -> tuple[int, int]:
    """ffprobe 取宽高，失败返回 (0, 0)。"""
    w = probe_scalar(path, "stream=width")
    h = probe_scalar(path, "stream=height")
    try:
        return int(w), int(h)
    except (TypeError, ValueError):
        return 0, 0


def _video_duration(path: Path) -> float:
    """取**视频流**时长（秒），读不到返回 0（调用方需走兜底）。

    为什么不用 `format=duration`：那是容器里最长一条流的长度。只改视频流而音频
    未同步时（音轨处理失败、或只做 crop 未变速），format 取的是未变的音频时长，
    会把视频侧的偏差完全抹平。实测 56.96s 素材只做 `setpts/1.05`、音频 copy：
    产物 v:0=54.30（真实）、a:0=56.93、format=56.93——用 format 算Δ 只有 0.03s，
    用视频流算才是 2.66s。偏差被抹到 0.03s 时门禁形同虚设，故只读视频流。

    读不到值时**必须**走 `_probe_fallback_duration`、不能拿 0 直接参与门禁：
    `dur_ok` 判的是 `|dur_dst - dur_src| <= 容差`，dur_dst=0 必然超限，会把整批
    正常文件全判成「时长不达标」——这是 100% 触发的假失败，比误报更糟（误报能
    靠人眼复核，确定性误判会让人直接怀疑整个工具）。

    实测哪些容器读不到（ffprobe 9.0.2，同一素材仅换 muxer）：
        mp4 / mov / mpegts  -> 有值（`-c copy`、带不带音轨都一样）
        matroska(.mkv) / flv -> stdout 为字符串 `N/A`（退出码仍是 0）  <- 会走兜底
    注意别依赖退出码判断：缺字段时 ffprobe **rc=0**、stdout 才是 `N/A`；
    只有文件不存在/损坏才 rc=1 + 空stdout。下面的 `float()` 由
    `except ValueError` 兜住 `N/A`、由 `IndexError` 兜住空 stdout，两条都返回 0。
    另外这类容器上 `format.duration` 反而有值（mkv 实测 5.30），正好是兜底链
    最后一档的用武之地。

    `stream.duration` **不会**因 fps 重采样而消失（实测 fps=60 / 30000/1001 /
    30003/1000 / 24000/1001 四种均正常给出），fps 不是字段缺失的原因。
    """
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        timeout=30)
    if rc != 0:
        return 0.0
    try:
        return float(out.strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _probe_fallback_duration(path: Path) -> float:
    """`_video_duration` 读不到值时的兜底（防御性，正常路径不会走到）。

    存在的理由：`stream=duration` 并非在所有容器上都可靠——实测 matroska(.mkv)
    与 flv 的 stdout 是 `N/A`（mp4/mov/mpegts 有值），此时若直接返回 0 会让
    时长门禁假失败。顺序：**先数帧/帧率（最准）-> 再试 stream=duration ->
    最后 format**。这三档在 mkv 上实测都能取到 5.300（stream缺、数帧与 format
    兜住），所以最后一档并非摆设。`format=duration` 是容器里最长流的长度，
    音视频不同步时会给错值（实测只做 setpts 时 format 取的是未变的音频时长），
    精度最低，只能当最后兜底。

    代价：`-count_frames` 需解码全片，实测 695ms，仅在主路径失败时触发。

    边界（fp-dev 报备后复核，本机 ffprobe 9.0.2 实测 nan/inf 不可达，故**不加防御
    代码**）：`float()` 遇 `nan`/`inf` 不抛异常，会一路绕过 `dur_unknown` 与
    `dur_ok` 两道判定、直接把报告写成「nan s」。实测 mp4/mkv/flv/mpegts + 极短视频
    （0.067s）的 duration 只有「正常数值」或 `N/A` 两类。
    另有一条同源入口：本函数 `frames / fps`，而 `_fps_to_float('nan')` 返回 nan、
    且 `bool(nan)` 为 True，故 `or 30.0` 兜不住（`or` 只拦假值，nan 是真值）。
    真要防就加 `math.isfinite`。
    注意 `0/0` 这条**确实会出现**：本机切片实测 12 个里 10 个的
    `probe_scalar(stream=avg_frame_rate)` 返回 `'30/1\n0/0'`（**有音轨的才有**，
    无音轨的 002.mp4 只返回 `'30/1'`），即 AGENTS.md 第 5 节记载的同一现象。
    本函数靠两点挡住：`splitlines()[0]` 取首行拿到真正的 `30/1`；即使拿到 `0/0`，
    `_fps_to_float` 返回 0.0 也会被 `or 30.0` 兜住（`30/0` 同理，见函数内 `if b`）。
    """
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
    """调fingerprint 自检并填门禁；自检失败只标 ok=False，不动文件。"""
    # 两侧都取视频流时长，口径一致才谈得上比较（见 _video_duration 注释）
    item.dur_dst = _video_duration(dst)
    if item.dur_dst <= 0:
        item.dur_dst = _probe_fallback_duration(dst)

    speed = _speed(item.params)
    # 自检一律按帧序号等间隔对齐（fingerprint.compare 的做法）。
    # 曾试过按时间戳对齐变速条目，实测距离反而更大（22.7 vs 21.5）——
    # 重采样不可逆，硬对齐会配错帧。详见 fingerprint.py 里的实测记录。
    item.gates = _gates(item, opts, speed)

    try:
        r = compare(src, dst, config.DEDUP_PHASH_SAMPLES)
        item.phash_dist = float(r.get("phash_dist") or 0.0)
        psnr = float(r.get("psnr") or 0.0)
        item.psnr = min(psnr, PSNR_CAP)   # inf 截断，JSON/Excel 才打得开
        item.gates = _gates(item, opts, speed)
    except Exception as e:  # noqa: BLE001 自检失败不算处理失败
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
    """只自检：不编码，只比对 src 与已存在的去重产物。"""
    if not dst.is_file():
        item.skipped = True
        item.error = f"还没有去重产物可比对: {dst.name}"
        item.seconds = time.time() - t0
        return item
    _self_check(item, src, dst, opts)
    item.seconds = time.time() - t0
    return item


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------
def process_batch(srcs: list[Path], opts: DedupOptions,
                  workers: int = 0) -> list[dict]:
    """并发处理（ThreadPoolExecutor），单条失败不影响整批，按输入顺序返回。"""
    srcs = [Path(s) for s in srcs]
    if not srcs:
        return []

    n = workers if workers and workers > 0 else config.DEDUP_WORKERS
    n = max(1, min(n, len(srcs)))

    # 多个不同素材可能映射到同一输出路径（例如不同父目录中均有
    # video/001.mp4）。原先并发编码会抢写同一个 dst，即使 tmp 文件
    # 名不同也可能在 os.replace 时互相覆盖。先检查整批目的路径。
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
        dst = resolve_output_path(s, opts)
        log(f"  [{idx + 1}/{len(srcs)}] {s.name} -> {dst.name}")
        try:
            return process_one(s, dst, opts)
        except Exception as e:  # noqa: BLE001 兜底，正常已在 process_one 里捕获
            log(f"  [异常] {s.name}: {type(e).__name__} {e}")
            return {"src": str(s), "dst": str(dst), "ok": False, "seed": 0,
                    "preset": opts.preset, "params": {}, "phash_dist": 0.0,
                    "psnr": 0.0, "dur_src": 0.0, "dur_dst": 0.0, "seconds": 0.0,
                    "gates": {}, "error": f"{type(e).__name__}: {e}",
                    "at": datetime.now().isoformat(timespec="seconds")}

    # 显式带回索引，绝不用 ex.map 的解包顺序（AGENTS.md 踩坑记录）
    out: list[dict] = [{} for _ in srcs]
    for i, dst in collided.items():
        log(f"  [拒绝覆盖] {srcs[i]} -> {dst}")
        out[i] = _Report(
            src=str(srcs[i]), dst=str(dst), preset=opts.preset,
            error=f"输出路径冲突（多个源文件会写入同一位置）: {dst}",
        ).to_dict()
    remaining = [(i, s) for i, s in enumerate(srcs) if i not in collided]
    if not remaining:
        return out
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
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def write_report(items: list[dict], report_dir: Path = None) -> Path:
    """把报告写成 JSON（utf-8-sig，Excel 直接打开不乱码），返回写入路径。"""
    d = Path(report_dir) if report_dir else Path(config.DEDUP_REPORT)
    d.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = d / f"去重报告_{stamp}.json"

    ok = sum(1 for i in items if i.get("ok"))
    # 「跳过」既不是成功也不是失败（产物已存在），单列统计，否则复盘时会误判
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