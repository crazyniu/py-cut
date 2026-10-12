"""cover 命令：对已产出的切片单独重跑字幕擦除。

独立出来便于只调整去字幕参数时不必重新切片。切片流水线第三步已自动调用，
本命令用于补跑 / 重跑（配合 --force 忽略 .covered.json 标记）。
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from concurrent import futures
from pathlib import Path

from .. import config
from ..core.ffmpeg import self_check
from ..core.llm import make_llm
from ..core.runtime import install_signal_handlers, reset_stop, STOP
from ..core.subtitle import SAMPLES, WORKERS, collect_clips, process_clip, verified_clip
from ..core.progress import progress, completed_futures
from ..core.utils import log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "cover", help="字幕擦除：对切片单独检测并擦除字幕（流水线第三步）")
    p.add_argument("--file", nargs="+", help="要处理的切片 mp4 路径（可多个）")
    p.add_argument("--all", action="store_true", help="处理默认有人脸／无人脸切片目录的全部切片")
    p.add_argument("--src-dir", default="",
                   help="要擦除字幕的视频目录（递归扫描所有 mp4；留空则用 --all/--file）")
    p.add_argument("--out-dir", default="", help="输出目录（默认源目录同级的字幕查重）")
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


def _pick_clips(args: argparse.Namespace) -> list[Path]:
    """返回去重后的 MP4 列表；显式文件缺失必须报错，不能悄悄跳过。"""
    if args.src_dir:
        from ..core.scan import list_videos_recursive  # noqa: PLC0415
        selected = [
            p for p in list_videos_recursive(Path(args.src_dir).expanduser())
            if p.suffix.lower() == ".mp4"
        ]
    elif args.file:
        selected = [Path(p).expanduser() for p in args.file]
        for file in selected:
            if not file.is_file():
                raise ValueError(f"输入文件不存在或不是普通文件: {file}")
            if file.suffix.lower() != ".mp4":
                raise ValueError(f"字幕擦除只支持 MP4 文件: {file}")
    else:
        selected = collect_clips(None)

    # 同一个文件通过别名或重复参数出现时，不能并行写相同临时文件。
    unique: dict[Path, Path] = {}
    for file in selected:
        unique.setdefault(file.resolve(), file)
    return list(unique.values())


def run(args: argparse.Namespace) -> int:
    if not args.file and not args.all and not args.src_dir:
        log("[错误] 需要 --file、--all 或 --src-dir")
        return 2
    if args.workers < 1 or args.samples < 1:
        log("[错误] --workers 和 --samples 都必须是正整数")
        return 2
    try:
        clips = _pick_clips(args)
    except (ValueError, OSError) as exc:
        log(f"[错误] {exc}")
        return 2
    if not clips:
        log("[错误] 所选范围没有可处理的 MP4 视频")
        return 1

    from ..core.output_paths import flat_output, sibling, source_root, validate_targets
    def destination(c: Path) -> Path:
        root = (Path(args.out_dir).expanduser() if args.out_dir else
                sibling(Path(args.src_dir) if args.src_dir else source_root(c), "字幕查重"))
        return flat_output(c, root)
    try:
        validate_targets([(c, destination(c)) for c in clips])
    except ValueError as exc:
        log(f"[错误] {exc}")
        return 2

    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)
    llm = make_llm(args.gateway, args.llm_url, args.llm_key, args.llm_model,
                   use_fallback=not args.no_fallback)
    log(f"[范围] {len(clips)} 个切片")

    t0 = time.time()
    sub_calls = llm.calls
    stat = {"covered": 0, "nosub": 0, "skip": 0, "fail": 0, "dryrun": 0, "review": 0}

    def _do(c: Path) -> str:
        if STOP.flag:
            return "skip"
        try:
            if args.dry_run:
                return process_clip(llm, c, force=args.force, dry_run=True, samples=args.samples)
            dst = destination(c)
            marker = dst.with_name(dst.name + ".covered.json")
            if dst.is_file() and verified_clip(dst) and not args.force:
                return "skip"
            dst.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".cover-", dir=dst.parent) as tmp:
                staged = Path(tmp) / dst.name
                shutil.copy2(c, staged)
                original_marker = c.with_name(c.name + ".covered.json")
                staged_marker = staged.with_name(staged.name + ".covered.json")
                if verified_clip(c) and not args.force:
                    shutil.copy2(original_marker, staged_marker)
                status = process_clip(llm, staged, force=args.force, samples=args.samples)
                if status == "review" and dst.is_file():
                    log(f"  [待复查] {c.name}: 保留已有输出，未用不确定结果覆盖")
                    return status
                if status in ("covered", "nosub", "skip", "review") and not STOP.flag:
                    staged.replace(dst)
                    if staged_marker.exists():
                        staged_marker.replace(marker)
                    else:
                        marker.unlink(missing_ok=True)
                return status

        except Exception as e:  # noqa: BLE001 单片段失败不拖垮整批
            log(f"  [异常] {c.name}: {type(e).__name__} {e}")
            return "fail"

    def report(waiting=False):
        progress("字幕擦除", sum(stat.values()), len(clips),
                 success=stat['covered'] + stat['nosub'], failed=stat['fail'],
                 skipped=stat['skip'], review=stat['review'],
                 detail=(f"并发 {args.workers} · 多帧合并 · {int(time.time() - t0)}秒 · 请求 {llm.calls - sub_calls} · " + (f"正在识别/覆盖剩余 {len(clips) - sum(stat.values())} 片；识别超时转待复查" if waiting else
                         f"复检通过 {stat['covered']} · 未检出字幕 {stat['nosub']} · 待复查 {stat['review']}")),
                 output=str(destination(clips[0]).parent))
    report()
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for future in completed_futures([ex.submit(_do, c) for c in clips], lambda: report(True)):
            s = future.result()
            stat[s] = stat.get(s, 0) + 1
            report()

    log("")
    log("=" * 56)
    log(f"{'擦除完成':<10}{stat['covered']:>6}   "
        f"{'无字幕':<8}{stat['nosub']:>4}   "
        f"{'跳过':<6}{stat['skip']:>4}   "
        f"{'失败':<6}{stat['fail']:>4}")
    if stat["review"]:
        log(f"[待复查] {stat['review']} 片识别不确定或仍可能有字幕；输出已保留，可继续后续任务")
    if stat["dryrun"]:      # 试跑：只报检测到的区域，不落盘
        log(f"{'试跑命中':<10}{stat['dryrun']:>6}   （--dry-run，未改写文件）")
    note = f"（失败 {llm.failures}）"
    if llm.switched_to:
        note += f"  已切换备用网关 {llm.switched_to}"
    log(f"{'LLM 调用':<10}{llm.calls:>6}   {note}")
    log(f"{'总耗时':<10}{time.time() - t0:>6.1f}s")
    log("=" * 56)
    if STOP.flag:
        log("[中断] 字幕擦除未完成")
        return 130
    return 0 if stat["fail"] == 0 else 2
