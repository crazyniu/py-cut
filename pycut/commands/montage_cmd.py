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
import os
from dataclasses import replace
import sys
import time
from pathlib import Path

from .. import config
from ..core.ffmpeg import self_check
from ..core.runtime import STOP, install_signal_handlers, reset_stop
from ..core.progress import progress
from ..core.utils import fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "montage", help="混剪：话术基准 → 随机混剪 → 画中画叠加",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("范围与步骤")
    g.add_argument("--step", default="all", choices=["a", "b", "c", "all"],
                   help="只跑某一步：a=提取基准 / b=片段混剪 / c=画中画叠加 / all=三步")
    g.add_argument("--source", default="",
                   help="话术视频；未指定时话术目录内只有一支则自动选择，多支请显式选择")
    g.add_argument("--clip-dir", action="append", default=None,
                   help=f"步骤B 片段库目录（默认 {config.MONTAGE_CLIP_DIR}）")
    g.add_argument("--dedup-dir", action="append", default=None,
                   help=f"步骤C 画中画素材目录（默认 {config.MONTAGE_DEDUP_DIR}）")
    g.add_argument("--out-dir", default="",
                   help="成片输出根目录，每次执行新建一个批次目录，所有视频直接放在该目录内")
    g.add_argument("--batch-dir", default="", help=argparse.SUPPRESS)
    g.add_argument("--intro-dir", action="append", default=None, help="前贴目录（默认读取 .env；空字符串禁用）")
    g.add_argument("--fission", type=int, default=1,
                   help="每支爆款视频裂变生成几支成片（每支随机选片/扰动不同、话术相同）")

    g = p.add_argument_group("混剪参数")
    g.add_argument("--seed", type=int, default=0,
                   help="随机种子（0=按基准视频路径自动派生；同 seed 选片结果可复现）")
    g.add_argument("--allow-landscape", action="store_true",
                   help="允许横屏素材（默认只吃竖屏 9:16，横屏一律排除）")
    g.add_argument("--pip-slot", type=float, default=2.5,
                   help="步骤C 每个画中画显示几秒")
    g.add_argument("--no-pip", action="store_true", help="不叠画中画（只跑步骤A+B）")
    g.add_argument("--srt", default="auto",
                   help="字幕文件（步骤D 烧录）。默认 auto=自动找与基准视频同目录同名的 .srt"
                        "（如 1.mp4 → 1.srt）；也可直接指定 .srt 路径；传空串则不烧字幕")
    g.add_argument("--no-sub", action="store_true", help="不烧字幕")
    g.add_argument("--crf", default="20", help="输出 libx264 CRF（越小越清晰）")
    g.add_argument("--probe", nargs="?", const=1, type=int, default=0,
                   help="只探测素材库规模后退出；可直接使用 --probe（兼容 --probe 1）")

    g = p.add_argument_group("画面扰动")
    g.add_argument("--mirror-chance", type=float, default=0.35,
                   help="片段随机镜像概率（素材含文字/包装时调低）")
    g.add_argument("--speed-max", type=float, default=1.5,
                   help="片段随机加速上限（1=不加速；1.5 表示 1.0~1.5x 随机）")
    g.add_argument("--sticker-dir", default="",
                   help=f"贴纸目录（默认 {config.MONTAGE_STICKER_DIR}）")
    g.add_argument("--sticker-chance", type=float, default=config.MONTAGE_STICKER_CHANCE,
                   help="每个片段叠贴纸的概率")
    g.add_argument("--no-sticker", action="store_true", help="不叠贴纸")
    g.add_argument("--seam-fade", type=float, default=0.2,
                   help="可选段间黑场淡化秒数（0=关闭；不会缩短成片时长）")
    g.add_argument("--pip-gap", action="store_true",
                   help="画中画段间留空档（默认首尾相接、连续显示）")
    g.add_argument("--no-audio", action="store_true",
                   help="不贴回基准视频原声（默认保留，成片必须有话术音轨）")

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
    """一次扫描获取素材规模和横竖屏统计，不重新逐文件启动 ffprobe。"""
    from ..core.montage_clips import scan_library

    log(f"[探测] 片段库: {clip_dir}")
    stats: dict = {}
    items = scan_library(clip_dir, workers=workers, allow_landscape=allow_landscape,
                         stats=stats)
    log(f"  文件总数(含横屏): {stats.get('files', 0)}")
    if allow_landscape:
        log(f"  可用: {len(items)} 条")
    else:
        portrait = stats.get("portrait", 0)
        landscape = stats.get("landscape", 0)
        log(f"  其中竖屏 9:16: {portrait} 条（横屏 {landscape} 条已排除）")
        log(f"  竖屏且时长合规: {len(items)} 条")
    if not items:
        log("  [错误] 没有可用素材")
        return 1

    durs = sorted(i.duration for i in items)
    if durs:
        import statistics
        log(f"  时长: 最短 {durs[0]:.1f}s  中位 {statistics.median(durs):.1f}s  "
            f"最长 {durs[-1]:.1f}s")
        log(f"  总容量: {sum(durs) / 60:.1f} 分钟")
    res = {f"{i.width}x{i.height}" for i in items}
    log(f"  分辨率: {' '.join(sorted(res))}")
    return 0


def _resolve_srt(args: argparse.Namespace, source: Path) -> str:
    """确定字幕文件路径（步骤D 用），返回空串表示不烧字幕。

    优先级：
      1. `--no-sub`  -> 不烧（空串）
      2. `--srt ""`  -> 不烧（空串）
      3. `--srt <路径>` -> 用用户给的路径（不存在则由流水线打日志跳过）
      4. `--srt auto`（默认）-> 与基准视频同目录同名的 .srt：
         `1.mp4 -> 1.srt`、`foo.mov -> foo.srt`。找不到也返回空串并提示。

    设计动机：用户明确要求「字幕和爆款视频同目录同名」，这样批量切不同素材时
    每支视频自动带自己的话术，不必每次手改固定路径（旧的 MONTAGE_SUB_PATH）。
    """
    if args.no_sub:
        return ""
    raw = args.srt
    if raw in ("", None):
        return ""
    if raw != "auto":
        return str(Path(raw).expanduser())
    candidate = source.with_suffix(".srt")
    if candidate.is_file():
        log(f"[字幕] 自动配对: {candidate.name}")
        return str(candidate)
    log(f"[字幕] 未找到同名字幕 {candidate.name}（跳过烧字幕；"
        f"可 --srt 指定路径或把 .srt 放到视频同目录）")
    return ""


def _is_portrait(p: Path) -> bool:
    """按侧边比粗判竖屏（用于 --probe 统计，不做精确探测，快）。"""
    import subprocess  # noqa: PLC0415
    try:
        r = subprocess.run(
            [config.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
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
    prepared = {}
    try:
        return _run(args, prepared)
    finally:
        for directory in prepared.get("temporary_dirs", []):
            directory.cleanup()


def _run(args: argparse.Namespace, prepared: dict) -> int:
    import math
    if not math.isfinite(args.seam_fade) or args.seam_fade < 0:
        log("[错误] --seam-fade 必须为有限的非负秒数")
        return 2
    if args.fission < 1:
        log("[错误] --fission 必须为正整数")
        return 2
    if not math.isfinite(args.fill_ratio) or abs(args.fill_ratio - 1.0) > 1e-9:
        log("[错误] 成片必须与基准视频等长：--fill-ratio 只能设为 1.0")
        return 2
    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)

    if not args.sticker_dir and os.environ.get("PYCUT_STICKER_DIR"):
        args.sticker_dir = str(config.MONTAGE_STICKER_DIR)
    source = Path(args.source).expanduser() if args.source else config.MONTAGE_SRC
    from ..settings import directories
    try:
        clip_dir = directories(args.clip_dir) if args.clip_dir is not None else config.CLIP_DIRS
        dedup_dir = directories(args.dedup_dir) if args.dedup_dir is not None else config.PIP_DIRS
        intro_dir = directories(args.intro_dir) if args.intro_dir is not None else config.INTRO_DIRS
    except ValueError as exc:
        log(f"[错误] 目录配置无效：{exc}")
        return 2
    clip_dir = clip_dir[0] if len(clip_dir) == 1 else clip_dir
    dedup_dir = dedup_dir[0] if len(dedup_dir) == 1 else dedup_dir
    from ..core.output_paths import montage_output, montage_batch, montage_stem

    # --probe 只检查片段库，不需要基准视频、字幕或已有成片。
    if args.probe:
        if STOP.flag:
            log("[中断] 混剪素材探测已取消（退出码 130）")
            return 130
        try:
            code = _probe_library(clip_dir, args.allow_landscape, args.workers)
        except (ValueError, OSError) as exc:
            log(f"[错误] {exc}")
            return 2
        if STOP.flag:
            log("[中断] 混剪素材探测未完整完成（退出码 130）")
            return 130
        return code

    if not args.source:
        from ..core.scan import list_videos_recursive
        try:
            candidates = [p for p in list_videos_recursive(config.REFERENCE_DIR, max_depth=None, raise_on_error=True)
                          if p.parent.resolve() != config.REFERENCE_DIR.resolve()]
        except (ValueError, OSError) as exc:
            log(f"[错误] 无法读取话术目录：{exc}")
            return 2
        if len(candidates) != 1:
            log(f"[错误] 话术目录有 {len(candidates)} 个视频，请通过 TUI 或 --source 明确选择")
            return 2
        source = candidates[0]
    if not source.is_file():
        sys.exit(f"[错误] 步骤A 基准视频不存在: {source}")
    if (not args.no_sub and args.srt not in ("", None, "auto")
            and not Path(args.srt).expanduser().is_file()):
        log(f"[错误] 指定字幕文件不存在: {args.srt}（请确认路径或用 --no-sub）")
        return 2

    # 字幕配对：默认 auto=与基准视频同目录同名 .srt（1.mp4 -> 1.srt）。
    # 用户显式传 --srt 路径时优先用；传空串（--srt ""）或 --no-sub 则不烧字幕。
    srt = _resolve_srt(args, source)

    try:
        source.resolve().relative_to(config.REFERENCE_DIR.resolve())
        reference_root = config.REFERENCE_DIR
    except ValueError:
        reference_root = source.parent
    output_root = (Path(args.out_dir).expanduser().resolve() if args.out_dir
                   else montage_output(reference_root))
    out_dir = Path(args.batch_dir).expanduser().resolve() if args.batch_dir else montage_batch(output_root)
    if out_dir.parent != output_root:
        log("[错误] 批次目录必须直接位于成片输出根目录下")
        return 2
    try:
        output_stem = montage_stem(source, out_dir, args.fission)
    except OSError as exc:
        log(f"[错误] 无法读取成片输出目录：{exc}")
        return 2

    t0 = time.time()
    canvas = config.MONTAGE_CANVAS
    log(f"[目标] 画布 {canvas[0]}x{canvas[1]}  fps {config.MONTAGE_FPS}  "
        f"横屏素材{'允许' if args.allow_landscape else '排除'}")

    from . import montage_pipeline  # noqa: PLC0415

    intro_dir = intro_dir[0] if len(intro_dir) == 1 else intro_dir
    opts = montage_pipeline.MontageOptions(
        prepared=prepared,
        out_name=f"{output_stem}_1.mp4",
        step=args.step,
        intro_dir=intro_dir or None,
        source=source,
        clip_dir=clip_dir,
        dedup_dir=dedup_dir,
        out_dir=out_dir,
        seed=(args.seed or None),
        allow_landscape=args.allow_landscape,
        pip_slot=args.pip_slot,
        use_pip=not args.no_pip and bool(dedup_dir),
        pip_continuous=not args.pip_gap,
        keep_audio=not args.no_audio,
        mirror_chance=max(0.0, min(1.0, args.mirror_chance)),
        speed_range=(1.0, max(1.0, args.speed_max)),
        sticker_dir=args.sticker_dir,
        online_stickers=not args.sticker_dir and not args.no_sticker,
        seam_fade=args.seam_fade,
        sticker_chance=0.0 if args.no_sticker else max(0.0, min(1.0, args.sticker_chance)),
        srt=srt,
        crf=args.crf,
        fill_ratio=args.fill_ratio,
        noise_db=args.noise_db,
        min_silence=args.min_silence,
        min_clip=args.min_clip,
        max_clip=args.max_clip,
        pip_scale=args.pip_scale,
        workers=args.workers,
    )
    if opts.intro_dir is not None and opts.step in ("all", "c"):
        from ..core.scan import list_videos_recursive
        try:
            if not list_videos_recursive(opts.intro_dir, raise_on_error=True):
                log(f"[跳过] 前贴目录没有视频，直接输出正文：{opts.intro_dir}")
        except OSError as exc:
            log(f"[错误] 无法读取前贴目录：{exc}")
            return 2
    if STOP.flag:
        log("[中断] 已取消混剪，未启动处理（退出码 130）")
        return 130
    progress_stage = "基准分析" if args.step == "a" else "裂变生成"
    progress_total = 1 if args.step == "a" else args.fission
    progress(progress_stage, 0, progress_total, detail="分析参考素材" if args.step == "a" else "每条成片独立选片与扰动")
    try:
        result = montage_pipeline.run_pipeline(opts)
    except (ValueError, OSError) as exc:
        result = {"error": str(exc)}
    if STOP.flag:
        log("[中断] 混剪任务已取消，不执行后续裂变（退出码 130）")
        return 130
    # 除仅提取基准的 step=a 外，任何“无成片路径”的结果都不能报告成功。
    # run_pipeline 负责实际产物完整性检查；这里防止异常结果字典漏报 error。
    if result.get("error") or (args.step != "a" and not result.get("final")):
        reason = result.get("error") or "处理结果缺少成片路径"
        progress(progress_stage, 1, progress_total, failed=1, detail="当前任务失败，停止后续处理")
        log(f"[失败] 混剪未完成：{reason}")
        return 2

    progress(progress_stage, 1, progress_total, success=1, detail="基准分析完成" if args.step == "a" else "首条成片已生成", output="" if args.step == "a" else str(out_dir))

    # ---------------- 批量裂变 ----------------
    # --fission N 表示「每支爆款视频出 N 支不同成片」。首支已由上面那次跑出
    # （复用 opts.seed），第 2..N 支换个 seed + 换个输出名再跑，随机选片/扰动不同
    # 但话术与节奏不变。seed 派生：用户显式给 seed 时在其上累加，否则按源+序号。
    fission = max(1, int(getattr(args, "fission", 1) or 1))
    failed_fissions = 0
    if fission > 1 and args.step != "a":
        from ..core.dedup_presets import derive_seed  # noqa: PLC0415
        base_seed = args.seed if args.seed else derive_seed(source)
        for i in range(2, fission + 1):
            if STOP.flag:
                log("[中断] 已停止后续裂变（退出码 130）")
                return 130
            log("")
            log(f"--- 裂变 {i}/{fission}（同一爆款视频，换 seed 重混）---")
            vopts = replace(opts, seed=base_seed + i * 10007,
                            out_name=f"{output_stem}_{i}.mp4")
            r = montage_pipeline.run_pipeline(vopts)
            if STOP.flag:
                log(f"[中断] 裂变 {i}/{fission} 未完整完成，不再继续（退出码 130）")
                return 130
            if r.get("error") or not r.get("final"):
                failed_fissions += 1
                log(f"  [失败] 裂变 {i}/{fission}: {r.get('error') or '无产物'}")
            else:
                log(f"  裂变 {i} 产出: {r['final']}")
            progress("裂变生成", i, fission, success=i - failed_fissions, failed=failed_fissions, detail="独立成片已生成并检查", output=str(out_dir))

    if STOP.flag:
        log("[中断] 混剪已取消（退出码 130）")
        return 130

    log("")
    log("=" * 62)
    if failed_fissions:
        log(f"混剪部分失败：{failed_fissions}/{fission} 支失败")
    else:
        log(f"混剪完成（裂变 {fission} 支）" if fission > 1 else "混剪完成")
    log("=" * 62)
    log(f"{'基准时长':<12}{fmt_dur(result['source_duration'])}")
    log(f"{'语音段数':<12}{result['segments']:>10}")
    log(f"{'步骤B 用片段':<12}{result['clips_used']:>10}")
    if result.get("clips_reused"):
        log(f"{'其中复用':<12}{result['clips_reused']:>10}")
    if result.get("pip_count"):
        log(f"{'步骤C 画中画':<12}{result['pip_count']:>10}  个")
    log(f"{'成片时长':<12}{fmt_dur(result['final_duration'])}")
    log(f"{'成片路径':<12}{result['final']}")
    log(f"{'总耗时':<12}{fmt_dur(time.time() - t0):>10}")
    log("=" * 62)
    if STOP.flag:
        log("[中断] 混剪已取消（退出码 130）")
        return 130
    if args.step != "a":
        log(f"输出目录：{Path(result['final']).resolve().parent}")
    return 2 if failed_fissions else 0
