"""slice 命令：短视频智能切片 + 人脸分流 + 字幕擦除。

这是当前已落地的主要功能。其它功能（混剪/转场/查重）作为独立命令挂载，
但都复用本模块产出的切片桶（源目录同级 切片/有人脸 / 切片/无人脸）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata
from pathlib import Path

from .. import config
from ..core.ffmpeg import VideoInfo, probe_video, self_check
from ..core.faces import bind_cascades, load_cascades
from ..core.llm import make_llm
from ..core.runtime import install_signal_handlers, reset_stop
from ..core.slice_pipeline import (Manifest, SliceOptions, list_videos,
                                    process_video)
from ..core.progress import progress
from ..core.utils import fmt_bytes, fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "slice", help="切片：场景分段 → 人脸分流 → 字幕擦除",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--file", default="",
                   help="指定单支 MP4：完整/相对路径精确选择，纯文件名按关键词匹配")
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
    g = p.add_argument_group("目录（可选，覆盖默认同级切片目录）")
    g.add_argument("--src-dir", default="",
                   help="源视频目录（默认 config.SRC_DIR，递归扫描子目录 mp4）")
    g.add_argument("--out-dir", default="",
                   help="切片输出根目录（下建 有人脸/无人脸 子目录，默认源目录同级的切片）")
    p.set_defaults(func=run)


def _is_explicit_file(value: str) -> bool:
    """路径形态与历史文件名关键词区分；`./name.mp4` 可消除歧义。"""
    return "/" in value or value.startswith("~")


def _pick_files(args: argparse.Namespace) -> list[Path]:
    if args.max_depth is not None and args.max_depth < 0:
        sys.exit("[错误] --max-depth 必须 >= 0")

    # 显式路径只处理这一支，不扫描默认素材目录，也不退回模糊匹配，
    # 否则输入 /other/missing.mp4 可能误处理 SRC_DIR 内的同名文件。
    if args.file and _is_explicit_file(args.file):
        try:
            path = Path(args.file).expanduser()
        except RuntimeError as exc:
            sys.exit(f"[错误] 无法解析 --file 路径: {exc}")
        if not path.is_file():
            sys.exit(f"[错误] --file 文件不存在或不是普通文件: {path}")
        if path.suffix.lower() != ".mp4":
            sys.exit(f"[错误] --file 只支持 MP4 视频: {path}")
        return [path.resolve()]

    if args.max_depth is not None:
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


def _colliding_stems(files: list[Path]) -> list[list[Path]]:
    """不同输入绝不能在同名输出目录互相覆盖（含 macOS 大小写不敏感目录）。"""
    by_stem: dict[str, list[Path]] = {}
    for path in files:
        name = unicodedata.normalize("NFC", path.stem).casefold()
        by_stem.setdefault(name, []).append(path)
    return [group for group in by_stem.values() if len(group) > 1]


def run(args: argparse.Namespace) -> int:
    install_signal_handlers()
    reset_stop()

    # 输出到源目录同级；显式 --out-dir 优先。
    if args.src_dir:
        config.SRC_DIR = Path(args.src_dir).expanduser()
    from ..core.output_paths import sibling
    source_dir = (Path(args.file).expanduser().parent
                  if args.file and _is_explicit_file(args.file) else config.SRC_DIR)
    od = (Path(args.out_dir).expanduser() if args.out_dir else
          config.CLIP_DIR if os.environ.get("PYCUT_CLIP_DIR") else sibling(source_dir, "切片"))
    config.OUT_FACE = od / "有人脸"
    config.OUT_NOFACE = od / "无人脸"
    config.OUT_MANIFEST = od / "清单"

    t_all = time.time()
    # 直接指定单文件时不要求默认 SRC_DIR 存在；其他 ffmpeg/cv2/磁盘自检照常执行。
    cv2 = (self_check(require_src=False)
           if args.file and _is_explicit_file(args.file) else self_check())
    cascades = load_cascades(cv2)
    bind_cascades(cascades)

    files = _pick_files(args)
    log(f"[源] {config.SRC_DIR} 找到 {len(files)} 个 mp4（os.scandir）")
    if not files:
        log("[错误] 所选范围内没有可处理的 MP4 视频")
        return 1

    collisions = _colliding_stems(files)
    if collisions:
        log("[错误] 以下源视频文件名重复，都会写入相同的切片输出目录：")
        for group in collisions[:5]:
            for path in group:
                log(f"  {path}")
        log("为防止不同视频切片互相覆盖，本次任务未开始。请先调整重复源文件名。")
        return 2

    llm = make_llm(args.gateway, args.llm_url, args.llm_key, args.llm_model,
                   enabled=not args.no_llm,
                   use_fallback=not args.no_fallback and not args.no_llm)
    if args.no_llm:
        log("[LLM] 已通过 --no-llm 禁用")

    if not args.no_sub and not args.dry_run and not llm.enabled:
        log("[错误] 已选择字幕擦除，但 LLM 未启用或未配置；请配置网关，或明确关闭字幕擦除")
        return 2

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
            progress("源视频", idx - 1, len(files), success=sum(not (r.failed or r.sub_fail) for r in results), failed=sum(bool(r.failed or r.sub_fail) for r in results), review=sum(r.sub_review for r in results), detail=f"识别并发 {args.workers_llm} · 编码并发 {args.workers_enc} · 检测并处理当前视频")
            log(f"--- [{idx}/{len(files)}] {f.name}")
            info: VideoInfo | None = probe_video(f)
            if info is None:
                log(f"[探测失败] {f.name}，跳过")
                results.append(_stub(f))
                continue
            try:
                r = process_video(llm, info, opts, manifest)
            except Exception as exc:  # noqa: BLE001 单视频失败不拖垮其他素材
                log(f"[处理异常] {f.name}: {type(exc).__name__}: {exc}")
                results.append(_stub(f))
                continue
            t_detect += r.detect_s
            t_enc += r.enc_s
            results.append(r)
            progress("源视频", idx, len(files), success=sum(not (r.failed or r.sub_fail) for r in results), failed=sum(bool(r.failed or r.sub_fail) for r in results), review=sum(r.sub_review for r in results), detail=("字幕擦除失败，此批切片尚未完成" if r.sub_fail else "切片流程已处理的源视频"), output=str(config.OUT_FACE.parent))
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
    # 任何切割、字幕失败或用户中断都不能误报成功，以免继续后续去重。
    if _stopped():
        log("[中断] 任务未完整完成，退出码 130")
        return 130
    review = sum(r.sub_review for r in results)
    if review:
        log(f"[待复查] {review} 片字幕识别不确定或可能残留；切片已保留，继续后续任务")
    sub_failed = sum(r.sub_fail for r in results)
    if tot_fail or sub_failed:
        log(f"[失败] 切片错误 {tot_fail}，字幕处理错误 {sub_failed}")
        return 2
    return 0
