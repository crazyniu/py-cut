"""slice 命令：短视频智能切片 + 人脸分流 + 字幕擦除。

这是当前已落地的主要功能。其它功能（混剪/转场/查重）作为独立命令挂载，
但都复用本模块产出的切片桶（桌面 切片_有人脸 / 切片_无人脸）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .. import config
from ..core.ffmpeg import VideoInfo, probe_video, self_check
from ..core.faces import bind_cascades, load_cascades
from ..core.llm import make_llm
from ..core.runtime import install_signal_handlers, reset_stop
from ..core.slice_pipeline import (Manifest, SliceOptions, list_videos,
                                    process_video)
from ..core.utils import fmt_bytes, fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "slice", help="切片：场景分段 → 人脸分流 → 字幕擦除",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--file", default="", help="只处理文件名（或部分匹配）的单个视频，试跑用")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 个视频（0=全部）")
    p.add_argument("--max-depth", type=int, default=None,
                   help="指定扫描深度（0=仅当前目录，TUI 默认 3；留空=CLI 原有无限递归）")
    p.add_argument("--dry-run", action="store_true", help="只做检测与分段，不抽帧/不切割")
    p.add_argument("--force", action="store_true", help="忽略续跑，重做已处理的视频")
    p.add_argument("--no-llm", action="store_true", help="完全不调用 LLM（人脸仅靠 Haar）")
    p.add_argument("--no-sub", action="store_true", help="跳过字幕擦除（流水线第三步）")
    p.add_argument("--scene-th", type=float, default=config.SCENE_TH, help="场景切换阈值")
    p.add_argument("--min-seg", type=float, default=config.MIN_SEG, help="最短段落(秒)")
    p.add_argument("--workers-llm", type=int, default=config.WORKERS_LLM, help="LLM 并发数")
    p.add_argument("--workers-enc", type=int, default=config.WORKERS_ENC, help="编码并发数")
    p.add_argument("--gateway", default=config.DEFAULT_GATEWAY,
                   choices=list(config.GATEWAYS.keys()),
                   help="起始网关：main=主网关 / backup=备用网关")
    p.add_argument("--no-fallback", action="store_true",
                   help="主网关连续失败时不自动切换备用网关")
    p.add_argument("--llm-url", default=None, help="LLM 网关地址（覆盖预设）")
    p.add_argument("--llm-key", default=None, help="LLM API Key（覆盖预设）")
    p.add_argument("--llm-model", default=None, help="LLM 模型名（覆盖预设）")
    g = p.add_argument_group("目录（可选，覆盖默认桌面切片桶）")
    g.add_argument("--src-dir", default="",
                   help="源视频目录（默认 config.SRC_DIR，递归扫描子目录 mp4）")
    g.add_argument("--out-dir", default="",
                   help="切片输出根目录（下建 有人脸/无人脸 子目录，默认桌面）")
    p.set_defaults(func=run)


def _pick_files(args: argparse.Namespace) -> list:
    if args.max_depth is not None:
        if args.max_depth < 0:
            sys.exit("[错误] --max-depth 必须 >= 0")
        from ..core.scan import list_videos_recursive  # noqa: PLC0415
        files = [p for p in list_videos_recursive(config.SRC_DIR, args.max_depth)
                 if p.suffix.lower() == ".mp4"]
    else:
        files = list_videos()
    if args.file:
        key = args.file.lower()
        matched = [f for f in files if key in f.name.lower()]
        if not matched:
            sys.exit(f"[错误] --file 未匹配到任何视频: {args.file}")
        return matched[:1]
    if args.limit and args.limit > 0:
        files = files[:args.limit]
    return files


def run(args: argparse.Namespace) -> int:
    install_signal_handlers()
    reset_stop()

    # 运行时覆盖目录常量（不传参时保持默认桌面切片桶，不影响其它用法）
    if args.src_dir:
        config.SRC_DIR = Path(args.src_dir).expanduser()
    if args.out_dir:
        od = Path(args.out_dir).expanduser()
        config.OUT_FACE = od / "有人脸"
        config.OUT_NOFACE = od / "无人脸"
        config.OUT_MANIFEST = od / "清单"

    t_all = time.time()
    cv2 = self_check()
    cascades = load_cascades(cv2)
    bind_cascades(cascades)

    files = _pick_files(args)
    log(f"[源] {config.SRC_DIR} 找到 {len(files)} 个 mp4（os.scandir）")
    if not files:
        log("[错误] 所选范围内没有可处理的 MP4 视频")
        return 1

    llm = make_llm(args.gateway, args.llm_url, args.llm_key, args.llm_model,
                   enabled=not args.no_llm,
                   use_fallback=not args.no_fallback and not args.no_llm)
    if args.no_llm:
        log("[LLM] 已通过 --no-llm 禁用")

    opts = SliceOptions(
        force=args.force, dry_run=args.dry_run, no_llm=args.no_llm,
        no_sub=args.no_sub, scene_th=args.scene_th, min_seg=args.min_seg,
        workers_llm=args.workers_llm, workers_enc=args.workers_enc)

    manifest = Manifest(config.OUT_MANIFEST)
    log(f"[清单] {manifest.csv_path.name}")

    results = []
    t_detect = t_enc = 0.0
    try:
        for idx, f in enumerate(files, 1):
            if _stopped():
                log("[中断] 停止处理后续视频")
                break
            log(f"--- [{idx}/{len(files)}] {f.name}")
            info: VideoInfo | None = probe_video(f)
            if info is None:
                log(f"[探测失败] {f.name}，跳过")
                results.append(_stub(f))
                continue
            r = process_video(llm, info, opts, manifest)
            t_detect += r.detect_s
            t_enc += r.enc_s
            results.append(r)
    finally:
        manifest.close()

    return _summary(results, llm, manifest, t_detect, t_enc, t_all)


def _stopped() -> bool:
    from ..core.runtime import STOP  # noqa: PLC0415
    return STOP.flag


def _stub(f) -> object:
    from ..core.slice_pipeline import VideoResult  # noqa: PLC0415
    return VideoResult(stem=f.stem, failed=1)


def _summary(results, llm, manifest, t_detect, t_enc, t_all) -> int:
    tot_seg = sum(r.segs for r in results)
    tot_face = sum(r.face for r in results)
    tot_noface = sum(r.noface for r in results)
    tot_fail = sum(r.failed for r in results)
    tot_bytes = sum(r.bytes_out for r in results)
    skipped = sum(1 for r in results if r.skipped)
    decided = tot_face + tot_noface

    log("")
    log("=" * 62)
    log("汇总")
    log("=" * 62)
    log(f"{'视频数':<12}{len(results):>10}  （跳过/已完成 {skipped}）")
    log(f"{'切片总数':<12}{tot_seg:>10}")
    log(f"{'有人脸':<12}{tot_face:>10}  " +
        (f"{tot_face / decided * 100:.1f}%" if decided else "-"))
    log(f"{'无人脸':<12}{tot_noface:>10}  " +
        (f"{tot_noface / decided * 100:.1f}%" if decided else "-"))
    log(f"{'失败':<12}{tot_fail:>10}")
    log(f"{'输出体积':<12}{fmt_bytes(tot_bytes):>10}")
    log(f"{'检测耗时':<12}{fmt_dur(t_detect):>10}")
    log(f"{'编码耗时':<12}{fmt_dur(t_enc):>10}")
    t_sub_all = sum(r.sub_s for r in results)
    sub_cov = sum(r.sub_covered for r in results)
    if t_sub_all > 0 or sub_cov:
        log(f"{'字幕擦除':<12}{sub_cov:>10}  （耗时 {fmt_dur(t_sub_all)}）")
    log(f"{'总耗时':<12}{fmt_dur(time.time() - t_all):>10}")
    llm_note = f"（失败 {llm.failures}）"
    if llm.switched_to:
        llm_note += f"  已切换备用网关 {llm.switched_to}"
    log(f"{'LLM 调用':<12}{llm.calls:>10}  {llm_note}")
    log(f"{'清单':<12}{manifest.csv_path}")
    log("=" * 62)
    # 任何切割 / 字幕失败都不能被误报成成功，避免 TUI 继续执行后续去重。
    sub_failed = sum(r.sub_fail for r in results)
    if tot_fail or sub_failed:
        log(f"[失败] 切片错误 {tot_fail}，字幕处理错误 {sub_failed}")
        return 2
    return 0
