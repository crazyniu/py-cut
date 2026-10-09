"""混剪编排层：把步骤 A/B/C 串成一条流水线，并产出最终成片。

设计原则（沿用 slice_pipeline 的做法）：
  - 编排只做「调度 + 汇总」，具体能力全在 core/montage_*.py 里，不重复实现；
  - 任何单步失败都不让整个命令崩，错误写进返回值由命令层汇总；
  - 中间产物落在 config.MONTAGE_TMP（不进桌面），只有最终成片写 out_dir。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .utils import log


@dataclass
class MontageOptions:
    step: str = "all"
    source: Path = field(default_factory=lambda: config.MONTAGE_SRC)
    clip_dir: Path = field(default_factory=lambda: config.MONTAGE_CLIP_DIR)
    dedup_dir: Path = field(default_factory=lambda: config.MONTAGE_DEDUP_DIR)
    out_dir: Path = field(default_factory=lambda: config.MONTAGE_OUT)
    seed: int | None = None
    allow_landscape: bool = False
    pip_slot: float = 2.5
    use_pip: bool = True
    crf: str = "20"
    fill_ratio: float = config.MONTAGE_TARGET_FILL_RATIO
    noise_db: float = config.MONTAGE_SILENCE_NOISE_DB
    min_silence: float = config.MONTAGE_SILENCE_MIN_DUR
    min_clip: float = config.MONTAGE_CLIP_MIN_DUR
    max_clip: float = config.MONTAGE_CLIP_MAX_DUR
    pip_scale: float = config.MONTAGE_PIP_SCALE
    workers: int = 0


def run_pipeline(opts: MontageOptions) -> dict:
    """按opts.step 跑 a / b / c / all，返回汇总字典。

    返回键：source_duration / segments / clips_used / clips_reused /
            pip_count / final_duration / final / base（步骤B 中间产物路径）
    """
    from .dedup_presets import derive_seed  # noqa: PLC0415 复用已有的 seed 派生
    from .montage_clips import (build_concat_cmd, build_source_clip_cmd,  # noqa: PLC0415
                                pick_clips, scan_library)
    from .montage_pip import (build_pip_cmd, plan_pip_timeline,  # noqa: PLC0415
                              scan_dedup_pool)
    from .montage_source import plan_clip_slots, probe_source  # noqa: PLC0415

    seed = opts.seed if opts.seed else derive_seed(opts.source)
    log(f"[seed] {seed}")

    tmp = Path(config.MONTAGE_TMP)
    tmp.mkdir(parents=True, exist_ok=True)
    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 步骤A：基准 ----------------
    log("--- 步骤A：提取话术节奏与时长基准 ---")
    prof = probe_source(opts.source)
    if prof is None:
        log(f"  [失败] 基准视频探测失败: {opts.source}")
        return {"source_duration": 0.0, "segments": 0, "clips_used": 0,
                "final_duration": 0.0, "final": "", "error": "步骤A 失败"}
    log(f"  基准: {prof.duration:.2f}s  {prof.width}x{prof.height}  "
        f"语音段 {len(prof.segments)} 个  有声 {prof.speech_total:.1f}s")

    # 画布以基准视频实测为准（config 的 MONTAGE_CANVAS 只是默认值）
    canvas = (prof.width, prof.height)
    target = prof.duration * opts.fill_ratio
    slots = plan_clip_slots(prof.segments, target, fill_ratio=opts.fill_ratio,
                            min_slot=opts.min_clip, max_slot=opts.max_clip)
    log(f"  时间槽 {len(slots)} 个，累计 {sum(b - a for a, b in slots):.2f}s"
        f"（目标 {target:.2f}s）")

    result = {
        "source_duration": prof.duration,
        "segments": len(prof.segments),
        "clips_used": 0, "clips_reused": 0,
        "pip_count": 0, "final_duration": 0.0, "final": "",
        "base": "", "seed": seed,
    }
    if opts.step == "a":
        log("  --step a：只提取基准，不产出成片")
        result["final_duration"] = target
        return result
    if not slots:
        log("  [失败] 没能规划出任何时间槽")
        result["error"] = "时间槽为空"
        return result

    # ---------------- 步骤B：随机混剪 ----------------
    log("--- 步骤B：随机混剪拼接 ---")
    items = scan_library(opts.clip_dir, min_dur=opts.min_clip,
                         max_dur=opts.max_clip, workers=opts.workers)
    if opts.allow_landscape:
        log(f"  素材 {len(items)} 条（已允许横屏）")
    else:
        log(f"  素材 {len(items)} 条（竖屏 9:16，横屏已排除）")
    if not items:
        log("  [失败] 没有可用素材")
        result["error"] = "素材库为空"
        return result

    picked = pick_clips(items, slots, seed=seed, min_dur=opts.min_clip,
                        max_dur=opts.max_clip)
    reused = sum(1 for p in picked if p.get("reused"))
    total = sum(p["take"] for p in picked)
    log(f"  选中 {len(picked)} 个片段（复用 {reused}）累计 {total:.2f}s")
    if picked and picked[0].get("shortfall"):
        log(f"  [提示] 素材不足以填满目标，缺口 {picked[0]['shortfall']:.2f}s")

    base = tmp / f"base_{seed}.mp4"
    # 片段库直接拼（不是从基准视频取画面）——基准视频只提供时长与节奏
    cmd, concat_list = build_concat_cmd(picked, base, canvas=canvas,
                                        fps=config.MONTAGE_FPS, crf=opts.crf)
    log(f"  拼接 {len(picked)} 段 -> {base.name}")
    ok = _run(cmd)
    if concat_list:
        _unlink(concat_list)
    if not ok or not base.is_file():
        log("  [失败] 拼接失败")
        result["error"] = "步骤B 拼接失败"
        return result
    log(f"  步骤B 产出: {base.name}  {_size(base)}")
    result["base"] = str(base)
    result["clips_used"] = len(picked)
    result["clips_reused"] = reused

    if opts.step == "b":
        log("  --step b：停在步骤B 产物")
        result["final"] = str(base)
        result["final_duration"] = target
        return result

    # ---------------- 步骤C：画中画叠加 ----------------
    final = out_dir / f"混剪_{opts.source.stem}.mp4"
    if not opts.use_pip:
        log("--- 步骤C：--no-pip，跳过画中画 ---")
        cmd = ["-y", "-i", str(base), "-c", "copy", str(final)]
        if not _run([config.FFMPEG] + cmd):
            log("  [失败] 无声拷贝失败")
            result["error"] = "步骤C 失败"
            return result
    else:
        log("--- 步骤C：画中画叠加 ---")
        pool = scan_dedup_pool(opts.dedup_dir, min_dur=opts.min_clip)
        log(f"  画中画素材 {len(pool)} 条")
        if not pool:
            log("  [提示] 无画中画素材，直接输出步骤B 结果")
            cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-i", str(base),
                   "-c", "copy", str(final)]
            _run(cmd)
        else:
            timeline = plan_pip_timeline(pool, target, slot=opts.pip_slot,
                                         seed=seed)
            cov = sum(t["duration"] for t in timeline)
            log(f"  画中画 {len(timeline)} 段，累计 {cov:.1f}s / {target:.1f}s")
            cmd = build_pip_cmd(base, timeline, final, canvas=canvas,
                                scale=opts.pip_scale, fps=config.MONTAGE_FPS,
                                crf=opts.crf)
            if not _run(cmd):
                log("  [失败] 画中画叠加失败")
                result["error"] = "步骤C 失败"
                return result
            result["pip_count"] = len(timeline)

    if not final.is_file():
        log("  [失败] 成片未生成")
        result["error"] = "成片未生成"
        return result

    result["final"] = str(final)
    result["final_duration"] = _duration(final)
    log(f"  成片: {final.name}  {_size(final)}  {result['final_duration']:.2f}s")
    return result


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> bool:
    """执行 ffmpeg 命令（列表形式，不经 shell）。失败只返回 False。"""
    import subprocess  # noqa: PLC0415
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  [命令失败] {type(e).__name__}: {e}")
        return False
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        log(f"  [命令失败] rc={r.returncode}: {tail[-1] if tail else '(无输出)'}")
        return False
    return True


def _unlink(p) -> None:
    try:
        Path(p).unlink()
    except OSError:
        pass


def _size(p: Path) -> str:
    from .utils import fmt_bytes  # noqa: PLC0415
    return fmt_bytes(Path(p).stat().st_size)


def _duration(p: Path) -> float:
    from .ffmpeg import probe_scalar  # noqa: PLC0415
    try:
        return float(probe_scalar(Path(p), "format=duration"))
    except (TypeError, ValueError):
        return 0.0
