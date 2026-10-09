"""montage 命令：混剪视频（爆款话术 + 片段库随机混剪 + 画中画叠加）。

三步流程（需求见 未命名.txt / docs/MONTAGE.md）：
  步骤A：从爆款视频提取语音节奏与总时长基准（`core/montage_source.py`）
  步骤B：从片段库随机取**竖屏**片段、不重复、填满基准时长、静音（`core/montage_clips.py`）
  步骤C：从去重输出取切片，以画中画叠在右上角、静音（`core/montage_pip.py`）

三者都支持单独跑（`--step a|b|c|all`），默认 `all` 跑完三步。
素材里混有横屏（1920x1080 / 3840x2160），一律排除——只吃竖屏 9:16。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import config
from ..core.ffmpeg import self_check
from ..core.runtime import install_signal_handlers, reset_stop
from ..core.utils import fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "montage", help="混剪：话术基准 → 随机混剪 → 画中画叠加",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("范围与步骤")
    g.add_argument("--step", default="all", choices=["a", "b", "c", "all"],
                   help="只跑某一步：a=提取基准 / b=片段混剪 / c=画中画叠加 / all=三步")
    g.add_argument("--source", default="",
                   help=f"步骤A 基准视频（默认 {config.MONTAGE_SRC}）")
    g.add_argument("--clip-dir", default="",
                   help=f"步骤B 片段库目录（默认 {config.MONTAGE_CLIP_DIR}）")
    g.add_argument("--dedup-dir", default="",
                   help=f"步骤C 画中画素材目录（默认 {config.MONTAGE_DEDUP_DIR}）")
    g.add_argument("--out-dir", default="",
                   help=f"最终成片输出目录（默认 {config.MONTAGE_OUT}）")

    g = p.add_argument_group("混剪参数")
    g.add_argument("--seed", type=int, default=0,
                   help="随机种子（0=按基准视频路径自动派生；同 seed 选片结果可复现）")
    g.add_argument("--allow-landscape", action="store_true",
                   help="允许横屏素材（默认只吃竖屏 9:16，横屏一律排除）")
    g.add_argument("--pip-slot", type=float, default=2.5,
                   help="步骤C 每个画中画显示几秒")
    g.add_argument("--no-pip", action="store_true", help="不叠画中画（只跑步骤A+B）")
    g.add_argument("--crf", default="20", help="输出 libx264 CRF（越小越清晰）")
    g.add_argument("--probe", type=int, default=0,
                   help="只探测素材库规模（片段数/竖屏数/时长分布）后退出，不处理")

    g = p.add_argument_group("调参（一般不用改）")
    g.add_argument("--fill-ratio", type=float, default=config.MONTAGE_TARGET_FILL_RATIO,
                   help="成片目标时长 = 基准时长 × 该系数")
    g.add_argument("--noise-db", type=float, default=config.MONTAGE_SILENCE_NOISE_DB,
                   help="语音切分的静音阈值 dB")
    g.add_argument("--min-silence", type=float, default=config.MONTAGE_SILENCE_MIN_DUR,
                   help="静音最短持续秒数，短于此不算断句")
    g.add_argument("--min-clip", type=float, default=config.MONTAGE_CLIP_MIN_DUR,
                   help="片段最短可用时长（秒）")
    g.add_argument("--max-clip", type=float, default=config.MONTAGE_CLIP_MAX_DUR,
                   help="片段最长可用时长（秒），超长的会被截断")
    g.add_argument("--pip-scale", type=float, default=config.MONTAGE_PIP_SCALE,
                   help="画中画宽度 = 基准宽 × 该比例")
    g.add_argument("--workers", type=int, default=0,
                   help=f"素材探测并发（0=用默认 {config.MONTAGE_WORKERS}）")
    p.set_defaults(func=run)


def _probe_library(clip_dir: Path, allow_landscape: bool, workers: int) -> int:
    """--probe：只统计素材库规模，用来回答「有多少素材可用」。"""
    from ..core.montage_clips import scan_library  # noqa: PLC0415

    log(f"[探测] 片段库: {clip_dir}")
    all_files = [p for p in clip_dir.rglob("*")
                 if p.suffix.lower() in (".mp4", ".mov", ".m4v")]
    log(f"  文件总数(含横屏): {len(all_files)}")

    if allow_landscape:
        items = scan_library(clip_dir, workers=workers)
        log(f"  可用: {len(items)} 条")
    else:
        # 只用竖屏：先按侧边比过滤，再按时长过滤，两次统计便于看清各过滤掉了多少
        items = scan_library(clip_dir, workers=workers)
        portrait = [p for p in all_files if _is_portrait(p)]
        log(f"  其中竖屏 9:16: {len(portrait)} 条（横屏 {len(all_files) - len(portrait)} 条已排除）")
        log(f"  竖屏且时长合规: {len(items)} 条")
        if not items:
            log("  ✗ 没有可用素材")
            return 1

    durs = sorted(i.duration for i in items)
    if durs:
        import statistics  # noqa: PLC0415
        total = sum(durs)
        log(f"  时长: 最短 {durs[0]:.1f}s  中位 {statistics.median(durs):.1f}s  "
            f"最长 {durs[-1]:.1f}s")
        log(f"  总容量: {total / 60:.1f} 分钟")
    res = {f"{i.width}x{i.height}" for i in items}
    log(f"  分辨率: {' '.join(sorted(res))}")
    return 0


def _is_portrait(p: Path) -> bool:
    """按侧边比粗判竖屏（用于 --probe 统计，不做精确探测，快）。"""
    import subprocess  # noqa: PLC0415
    r = subprocess.run(
        [config.FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, timeout=30)
    out = r.stdout.strip().splitlines()
    if not out:
        return False
    parts = [x for x in out[0].split(",") if x.strip()]
    if len(parts) < 2:
        return False
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return h > 0 and (w / h) <= config.MONTAGE_MAX_SIDE_RATIO


def run(args: argparse.Namespace) -> int:
    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)

    source = Path(args.source) if args.source else config.MONTAGE_SRC
    clip_dir = Path(args.clip_dir) if args.clip_dir else config.MONTAGE_CLIP_DIR
    dedup_dir = Path(args.dedup_dir) if args.dedup_dir else config.MONTAGE_DEDUP_DIR
    out_dir = Path(args.out_dir) if args.out_dir else config.MONTAGE_OUT

    if not source.is_file():
        sys.exit(f"[错误] 步骤A 基准视频不存在: {source}")

    if args.probe:
        return _probe_library(clip_dir, args.allow_landscape, args.workers)

    t0 = time.time()
    canvas = config.MONTAGE_CANVAS
    log(f"[目标] 画布 {canvas[0]}x{canvas[1]}  fps {config.MONTAGE_FPS}  "
        f"横屏素材{'允许' if args.allow_landscape else '排除'}")

    from . import montage_pipeline  # noqa: PLC0415

    opts = montage_pipeline.MontageOptions(
        step=args.step,
        source=source,
        clip_dir=clip_dir,
        dedup_dir=dedup_dir,
        out_dir=out_dir,
        seed=(args.seed or None),
        allow_landscape=args.allow_landscape,
        pip_slot=args.pip_slot,
        use_pip=not args.no_pip,
        crf=args.crf,
        fill_ratio=args.fill_ratio,
        noise_db=args.noise_db,
        min_silence=args.min_silence,
        min_clip=args.min_clip,
        max_clip=args.max_clip,
        pip_scale=args.pip_scale,
        workers=args.workers,
    )
    result = montage_pipeline.run_pipeline(opts)

    log("")
    log("=" * 62)
    log("混剪完成")
    log("=" * 62)
    log(f"{'基准时长':<12}{fmt_dur(result['source_duration'])}")
    log(f"{'语音段数':<12}{result['segments']:>10}")
    log(f"{'步骤B 用片段':<12}{result['clips_used']:>10}")
    if result.get("clips_reused"):
        log(f"{'其中复用':<12}{result['clips_reused']:>10}  （素材不足以填满，已截短）")
    if result.get("pip_count"):
        log(f"{'步骤C 画中画':<12}{result['pip_count']:>10}  个")
    log(f"{'成片时长':<12}{fmt_dur(result['final_duration'])}")
    log(f"{'成片路径':<12}{result['final']}")
    log(f"{'总耗时':<12}{fmt_dur(time.time() - t0):>10}")
    log("=" * 62)
    return 0
