"""dedup 命令：视频去重（过平台查重）。

对已有切片/视频做分层扰动（容器元数据 → 几何 → 像素 → 时间 → 结构 → 音频），
降低被平台判为「重复内容」的概率。需求与技术方案见 docs/DEDUP.md。

流水线：
  参数随机化(seed) → 构造滤镜链 → ffmpeg 重编码 → 自检(pHash 距离 + PSNR) → JSON 报告

自检不通过（改动不足 / 画质损失过大）时文件仍保留，只是报告里 ok=False，
由使用者决定是否采用——去重没有「绝对通过」，只能量化「改了多少、损失多少」。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import config
from ..core.dedup import (DedupOptions, default_output_path, process_batch,
                          write_report)
from ..core.ffmpeg import self_check
from ..core.runtime import install_signal_handlers, reset_stop
from ..core.subtitle import collect_clips
from ..core.utils import fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "dedup", help="去重：分层扰动 + pHash/PSNR 自检（过平台查重）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("范围")
    g.add_argument("--file", nargs="+", help="要处理的视频/切片路径（可多个）")
    g.add_argument("--all", action="store_true", help="处理桌面两个切片目录的全部切片")

    g = p.add_argument_group("档位与开关")
    g.add_argument("--preset", default=config.DEDUP_DEFAULT_PRESET,
                   choices=list(config.DEDUP_PRESETS),
                   help="去重档位：light 轻度 / standard 标准 / aggressive 激进")
    g.add_argument("--seed", type=int, default=0,
                   help="随机化种子（0=按文件路径自动派生；同 seed 结果可复现）")
    g.add_argument("--mirror", action="store_true",
                   help="允许水平镜像（有人脸朝向/文字的素材慎开，有人工复审风险）")
    g.add_argument("--speed", action="store_true", help="允许微变速（音画同步微调）")
    g.add_argument("--border", action="store_true", help="模糊边框填充（改构图）")
    g.add_argument("--badge", action="store_true", help="右下角标色块")
    g.add_argument("--logo", default="", help="画中画图片路径（贴纸/角标）")
    g.add_argument("--sticker", default="",
                   help="贴纸图片路径（背面半透明 + 边缘羽化的静态干扰层）")
    g.add_argument("--delogo", default="",
                   help="可见水印遮盖区域 x,y,w,h（走 delogo 滤镜）")

    g = p.add_argument_group("输出与自检")
    g.add_argument("--out-dir", default="", help="输出目录（默认 ~/Desktop/去重_输出）")
    g.add_argument("--in-place", action="store_true", help="原地覆盖源文件（危险）")
    g.add_argument("--check", action="store_true",
                   help="只自检：与已有去重产物比对 pHash/PSNR，不产出文件")
    g.add_argument("--no-report", action="store_true", help="不写 JSON 报告")
    g.add_argument("--phash-min", type=int, default=config.DEDUP_MIN_PHASH_DIST,
                   help="pHash 平均汉明距离下限（低于此值判为改动不足）")
    g.add_argument("--psnr-min", type=float, default=config.DEDUP_MIN_PSNR,
                   help="PSNR 下限 dB（低于此值判为画质损失过大）")
    g.add_argument("--workers", type=int, default=0,
                   help=f"并发数（0=用默认 {config.DEDUP_WORKERS}）")
    g.add_argument("--force", action="store_true", help="覆盖已存在的去重产物")
    p.set_defaults(func=run)


def _pick_srcs(args: argparse.Namespace) -> list[Path]:
    if args.file:
        srcs = [Path(x) for x in args.file]
        missing = [s for s in srcs if not s.is_file()]
        if missing:
            sys.exit(f"[错误] 文件不存在: {missing[0]}")
        return srcs
    if args.all:
        clips = collect_clips(None)
        if not clips:
            sys.exit("[错误] 桌面两个切片目录里没有可处理的切片")
        return clips
    sys.exit("[错误] 需要 --file 或 --all")


def _check_only(srcs: list[Path], args: argparse.Namespace) -> int:
    """只自检模式：与已有去重产物比对，不产出。"""
    from ..core.fingerprint import compare  # noqa: PLC0415 延迟导入，未用到不加载 cv2

    ok = miss = bad = 0
    t0 = time.time()
    for s in srcs:
        out = default_output_path(s)
        if not out.is_file():
            log(f"  [跳过] {s.name}: 还没有去重产物（{out.name}）")
            miss += 1
            continue
        try:
            r = compare(s, out, config.DEDUP_PHASH_SAMPLES)
        except RuntimeError as e:
            log(f"  [失败] {s.name}: {e}")
            bad += 1
            continue
        d, q = r.get("phash_dist", 0.0), r.get("psnr", 0.0)
        good = d >= args.phash_min and q >= args.psnr_min
        ok += good
        bad += (not good)
        log(f"  {'[达标]' if good else '[不达标]'} {s.name}: "
            f"pHash距离 {d:.1f}/64（门槛 {args.phash_min}）  PSNR {q:.1f}dB（门槛 {args.psnr_min}）")
    log("")
    log("=" * 56)
    log(f"{'自检达标':<10}{ok:>6}   {'不达标':<8}{bad:>4}   {'无产物':<8}{miss:>4}")
    log(f"{'总耗时':<10}{time.time() - t0:>6.1f}s")
    log("=" * 56)
    return 0 if bad == 0 else 2


def run(args: argparse.Namespace) -> int:
    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)

    srcs = _pick_srcs(args)
    log(f"[范围] {len(srcs)} 个视频   档位 {args.preset}")

    if args.check:
        return _check_only(srcs, args)

    opts = DedupOptions(
        preset=args.preset,
        seed=(args.seed or None),
        mirror=args.mirror,
        speed=args.speed,
        border=args.border,
        badge=args.badge,
        logo=args.logo,
        delogo=args.delogo,
        sticker=args.sticker,
        out_dir=args.out_dir,
        in_place=args.in_place,
        phash_min=args.phash_min,
        psnr_min=args.psnr_min,
        force=args.force,
    )

    if args.in_place:
        log("[警告] --in-place 会覆盖源文件，务必确认已备份")

    t0 = time.time()
    items = process_batch(srcs, opts, workers=args.workers)

    ok = sum(1 for i in items if i.get("ok"))
    bad = sum(1 for i in items if not i.get("ok") and not i.get("skipped"))
    skipped = sum(1 for i in items if i.get("skipped"))
    dists = [i["phash_dist"] for i in items if isinstance(i.get("phash_dist"), (int, float))]
    psnrs = [i["psnr"] for i in items if isinstance(i.get("psnr"), (int, float))]
    dur = sum(i.get("seconds", 0.0) for i in items)
    out_bytes = sum((Path(i["dst"]).stat().st_size for i in items
                     if i.get("dst") and Path(i["dst"]).is_file()), 0)

    log("")
    log("=" * 62)
    log("去重汇总")
    log("=" * 62)
    log(f"{'视频数':<12}{len(items):>10}")
    log(f"{'达标':<12}{ok:>10}")
    log(f"{'不达标':<12}{bad:>10}  （改动不足或画面疑似损坏，文件仍保留）")
    if skipped:
        log(f"{'跳过':<12}{skipped:>10}  （产物已存在，--force 可覆盖）")
    if dists:
        gate = config.DEDUP_MIN_PHASH_BY_PRESET.get(args.preset,
                                                   config.DEDUP_MIN_PHASH_DIST)
        log(f"{'pHash 均值':<12}{sum(dists) / len(dists):>10.1f}  "
            f"（{args.preset} 档门槛 {gate:g}/64）")
    if psnrs:
        log(f"{'PSNR 均值':<12}{sum(psnrs) / len(psnrs):>10.1f}  "
            f"（灾难下限 {args.psnr_min:g}dB；几何改动掉到 20dB 属正常）")
    log(f"{'输出体积':<12}{out_bytes / 1024 / 1024:>9.1f}MB")
    log(f"{'处理耗时':<12}{fmt_dur(time.time() - t0):>10}  （视频总长 {fmt_dur(dur)}）")

    if not args.no_report:
        path = write_report(items)
        log(f"{'报告':<12}{path}")
    log("=" * 62)
    return 0 if bad == 0 else 2
