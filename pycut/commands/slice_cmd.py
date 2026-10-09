"""slice 命令：短视频智能切片 + 人脸分流 + 字幕擦除。

这是当前已落地的主要功能。其它功能（混剪/转场/查重）作为独立命令挂载，
但都复用本模块产出的切片桶（桌面 切片_有人脸 / 切片_无人脸）。
"""
from __future__ import annotations

import argparse
import sys
import time

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
    p.add_argument("--dry-run", action="store_true", help="只做检测与分段，不抽帧/不切割")
    p.add_argument("--force", action="store_true", help="忽略续跑，重做已处理的视频")
    p.add_argument("--no-llm", action="store_true", help="完全不调用 LLM（人脸仅靠 Haar）")
    p.add_argument("--no-sub", action="store_true", help="跳过字幕擦除（流水线第三步）")
    p.add_argument("--scene-th", type=float, default=config.SCENE_TH, help="场景切换阈值")
    p.add_argument("--min-seg", type=float, default=config.MIN_SEG, help="最短段落(秒)")
    p.add_argument("--workers-llm", type=int, default=config.WORKERS_LLM, help="LLM 并发数")
    p.add_argument("--workers-enc", type=int, default=config.WORKERS_ENC, help="编码并发数")
    p.add_argument("--gateway", default=config.DEFAULT_GATEWAY,
                   choices=list(config.GATEWAYS.keys()), help="LLM 网关预设")
    p.add_argument("--llm-url", default=None, help="LLM 网关地址（覆盖预设）")
    p.add_argument("--llm-key", default=None, help="LLM API Key（覆盖预设）")
    p.add_argument("--llm-model", default=None, help="LLM 模型名（覆盖预设）")
    p.set_defaults(func=run)


def _pick_files(args: argparse.Namespace) -> list:
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

    t_all = time.time()
    cv2 = self_check()
    cascades = load_cascades(cv2)
    bind_cascades(cascades)

    files = _pick_files(args)
    log(f"[源] {config.SRC_DIR} 找到 {len(files)} 个 mp4（os.scandir）")

    llm = make_llm(args.gateway, args.llm_url, args.llm_key, args.llm_model,
                   enabled=not args.no_llm)
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
    log(f"{'LLM 调用':<12}{llm.calls:>10}  （失败 {llm.failures}）")
    log(f"{'清单':<12}{manifest.csv_path}")
    log("=" * 62)
    return 0
