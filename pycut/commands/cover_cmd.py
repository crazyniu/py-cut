"""cover 命令：对已产出的切片单独重跑字幕擦除。

独立出来便于只调整去字幕参数时不必重新切片。切片流水线第三步已自动调用，
本命令用于补跑 / 重跑（配合 --force 忽略 .covered.json 标记）。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent import futures
from pathlib import Path

from .. import config
from ..core.ffmpeg import self_check
from ..core.llm import make_llm
from ..core.runtime import install_signal_handlers, reset_stop, STOP
from ..core.subtitle import SAMPLES, WORKERS, collect_clips, process_clip
from ..core.utils import log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "cover", help="字幕擦除：对切片单独检测并擦除字幕（流水线第三步）")
    p.add_argument("--file", nargs="+", help="要处理的切片 mp4 路径（可多个）")
    p.add_argument("--all", action="store_true", help="处理桌面两个切片目录的全部切片")
    p.add_argument("--gateway", default=config.DEFAULT_GATEWAY,
                   choices=list(config.GATEWAYS.keys()),
                   help="起始网关：main=主网关 / backup=备用网关")
    p.add_argument("--no-fallback", action="store_true",
                   help="主网关连续失败时不自动切换备用网关")
    p.add_argument("--llm-url", default=None)
    p.add_argument("--llm-key", default=None)
    p.add_argument("--llm-model", default=None)
    p.add_argument("--samples", type=int, default=SAMPLES, help="每片段抽帧检测数")
    p.add_argument("--workers", type=int, default=WORKERS, help="并发片段数")
    p.add_argument("--dry-run", action="store_true", help="只检测字幕区域，不改文件")
    p.add_argument("--force", action="store_true", help="忽略 .covered 标记重跑")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if not args.file and not args.all:
        sys.exit("[错误] 需要 --file 或 --all")

    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)

    llm = make_llm(args.gateway, args.llm_url, args.llm_key, args.llm_model,
                   use_fallback=not args.no_fallback)
    clips = collect_clips([Path(x) for x in args.file] if args.file else None)
    if not clips:
        log("没有可处理的切片")
        return 1
    log(f"[范围] {len(clips)} 个切片")

    t0 = time.time()
    stat = {"covered": 0, "nosub": 0, "skip": 0, "fail": 0, "dryrun": 0}

    def _do(c: Path) -> str:
        if STOP.flag:
            return "skip"
        try:
            return process_clip(llm, c, force=args.force,
                                dry_run=args.dry_run, samples=args.samples)
        except Exception as e:  # noqa: BLE001 单片段失败不拖垮整批
            log(f"  [异常] {c.name}: {type(e).__name__} {e}")
            return "fail"

    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for s in ex.map(_do, clips):
            stat[s] = stat.get(s, 0) + 1

    log("")
    log("=" * 56)
    log(f"{'擦除完成':<10}{stat['covered']:>6}   "
        f"{'无字幕':<8}{stat['nosub']:>4}   "
        f"{'跳过':<6}{stat['skip']:>4}   "
        f"{'失败':<6}{stat['fail']:>4}")
    note = f"（失败 {llm.failures}）"
    if llm.switched_to:
        note += f"  已切换备用网关 {llm.switched_to}"
    log(f"{'LLM 调用':<10}{llm.calls:>6}   {note}")
    log(f"{'总耗时':<10}{time.time() - t0:>6.1f}s")
    log("=" * 56)
    return 0 if stat["fail"] == 0 else 2
