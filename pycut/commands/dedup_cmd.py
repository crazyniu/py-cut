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
import json
import sys
import time
from pathlib import Path

from .. import config
from ..core.dedup_audio import (AudioMixError, AudioMixOptions,
                                preflight_external_audio, validate_audio_options)
from ..core.dedup import (DedupOptions, process_batch, resolve_output_path,
                          write_report)
from ..core.dedup_profiles import (ExplicitBoolean, ExplicitValue,
                                   ProfileError, apply_account_profile)
from ..core.ffmpeg import self_check
from ..core.runtime import STOP, install_signal_handlers, reset_stop
from ..core.subtitle import collect_clips
from ..core.utils import fmt_dur, log


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "dedup", help="去重：分层扰动 + pHash/PSNR 自检（过平台查重）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("范围")
    g.add_argument("--file", nargs="+", help="要处理的视频/切片路径（可多个）")
    g.add_argument("--all", action="store_true", help="处理切片桶（有人脸/无人脸）的全部切片")
    g.add_argument("--source-stems", nargs="*", help="仅处理指定源视频的编号切片（切片后继任务）")
    g.add_argument("--src-dir", default="",
                   help="视频目录（递归扫描所有 mp4；优先于 --all）")

    g = p.add_argument_group("本地账号预设（不存储凭证）")
    g.add_argument("--profile-file", default="",
                   help="本地 JSON 配置文件（version=1，禁止密钥字段）")
    g.add_argument("--account", default="",
                   help="账号预设别名（须同时指定 --profile-file）")

    g = p.add_argument_group("档位与开关")
    g.add_argument("--preset", action=ExplicitValue,
                   default=config.DEDUP_DEFAULT_PRESET,
                   choices=list(config.DEDUP_PRESETS),
                   help="去重档位：light 轻度 / standard 标准 / aggressive 激进")
    g.add_argument("--seed", type=int, action=ExplicitValue, default=0,
                   help="随机化种子（0=按文件路径自动派生；同 seed 结果可复现）")
    g.add_argument("--mirror", action=ExplicitBoolean, default=False,
                   help="允许水平镜像（有人脸朝向/文字的素材慎开，有人工复审风险）")
    g.add_argument("--speed", action=ExplicitBoolean, default=False, help="允许微变速（音画同步微调）")
    g.add_argument("--vary-bitrate", action=ExplicitBoolean, default=False,
                   help="可选视频码率扰动：按 seed 生成 ABR 目标；默认仍使用 CRF")
    g.add_argument("--ken-burns", action=ExplicitBoolean, default=False,
                   help="可选 Ken Burns 缓慢缩放/平移（保持分辨率、帧率与音轨）")
    g.add_argument("--border", action=ExplicitBoolean, default=False, help="模糊边框填充（改构图）")
    g.add_argument("--badge", action=ExplicitBoolean, default=False, help="右下角标色块")
    g.add_argument("--logo", default="", help="画中画图片路径（贴纸/角标）")
    g.add_argument("--sticker", default="",
                   help="贴纸图片路径（背面半透明 + 边缘羽化的静态干扰层）")
    g.add_argument("--delogo", default="",
                   help="可见水印遮盖区域 x,y,w,h（走 delogo 滤镜）")

    g = p.add_argument_group("可选音轨编辑（自有或授权素材，默认关闭）")
    g.add_argument("--bgm", default="", help="背景音乐路径（短音轨自动循环至视频结束）")
    g.add_argument("--ambience", default="", help="环境音路径（短音轨自动循环）")
    g.add_argument("--voiceover", default="", help="替换原声的配音路径（过短补静音）")
    g.add_argument("--original-gain", type=float, default=1.0,
                   help="原声增益 0～2（替换配音时不能设置）")
    g.add_argument("--bgm-gain", type=float, default=0.25, help="BGM 增益 0～2")
    g.add_argument("--ambience-gain", type=float, default=0.30, help="环境音增益 0～2")
    g.add_argument("--voice-gain", type=float, default=1.0, help="配音增益 0～2")
    g.add_argument("--audio-fade-out", type=float, default=0.0,
                   help="整条混合音轨末尾淡出秒数 0～10")

    g = p.add_argument_group("输出与自检")
    g.add_argument("--out-dir", default="", help="输出目录（默认源目录同级的去重）")
    g.add_argument("--in-place", action="store_true", help="原地覆盖源文件（危险）")
    g.add_argument("--check", action="store_true",
                   help="只自检：与已有去重产物比对 pHash/PSNR，不产出文件")
    g.add_argument("--no-report", action="store_true", help="不写 JSON 报告")
    g.add_argument("--vmaf", action="store_true", help="可选：只自检时额外计算 VMAF（要求视频严格对齐）")
    g.add_argument("--vmaf-min", type=float, default=None,
                   help="VMAF 下限 0~100；须显式开启 --vmaf，不提供则仅报告评分")
    g.add_argument("--phash-min", type=float, action=ExplicitValue,
                   default=0.0,
                   help="pHash 距离下限（0=自动使用当前 preset 的实测门槛；允许小数）")
    g.add_argument("--psnr-min", type=float, action=ExplicitValue,
                   default=config.DEDUP_MIN_PSNR,
                   help="PSNR 下限 dB（低于此值判为画质损失过大）")
    g.add_argument("--workers", type=int, action=ExplicitValue, default=0,
                   help=f"并发数（0=用默认 {config.DEDUP_WORKERS}）")
    g.add_argument("--force", action="store_true", help="覆盖已存在的去重产物")
    p.set_defaults(func=run)


def _pick_srcs(args: argparse.Namespace) -> list[Path]:
    if args.file:
        srcs = [Path(x).expanduser() for x in args.file]
        missing = [s for s in srcs if not s.is_file()]
        if missing:
            sys.exit(f"[错误] 文件不存在: {missing[0]}")
        invalid = [s for s in srcs if s.suffix.lower() != ".mp4"]
        if invalid:
            sys.exit(f"[错误] 仅支持 MP4 视频文件: {invalid[0]}")
        return srcs
    if args.src_dir:
        # 递归扫描指定目录下所有视频（不限切片桶结构，也不要求 --all）
        from ..core.scan import list_videos_recursive  # noqa: PLC0415
        srcs = list_videos_recursive(Path(args.src_dir).expanduser())
        if not srcs:
            sys.exit(f"[错误] {args.src_dir} 下没有可处理的视频")
        return srcs
    if args.all:
        clips = collect_clips(None)
        if not clips:
            sys.exit("[错误] 桌面两个切片目录里没有可处理的切片")
        return clips
    sys.exit("[错误] 需要 --file、--src-dir 或 --all")


def _check_only(srcs: list[Path], args: argparse.Namespace) -> int:
    """只自检模式：与已有去重产物比对，不产出。"""
    from ..core.fingerprint import compare  # noqa: PLC0415 延迟导入，未用到不加载 cv2

    from ..core.quality_vmaf import VMAFError, assess  # noqa: PLC0415

    ok = miss = bad = 0
    checks: list[dict[str, object]] = []
    t0 = time.time()
    output_opts = DedupOptions(out_dir=args.out_dir)
    phash_min = (args.phash_min if args.phash_min > 0
                 else config.DEDUP_MIN_PHASH_BY_PRESET.get(
                     args.preset, config.DEDUP_MIN_PHASH_DIST))
    for s in srcs:
        if STOP.flag:
            log("[中断] 自检已停止，不再检查后续视频（退出码 130）")
            return 130
        out = resolve_output_path(s, output_opts)
        if not out.is_file():
            log(f"  [跳过] {s.name}: 还没有去重产物（{out.name}）")
            miss += 1
            checks.append({"source": str(s), "output": str(out), "status": "missing_output"})
            continue
        try:
            r = compare(s, out, config.DEDUP_PHASH_SAMPLES)
        except RuntimeError as e:
            log(f"  [失败] {s.name}: {e}")
            bad += 1
            checks.append({"source": str(s), "output": str(out), "status": "comparison_error", "error": str(e)})
            continue
        d, q = r.get("phash_dist", 0.0), r.get("psnr", 0.0)
        good = d >= phash_min and q >= args.psnr_min
        entry: dict[str, object] = {"source": str(s), "output": str(out), "phash_dist": d, "psnr": q}
        if args.vmaf:
            try:
                score = assess(s, out)
                entry["vmaf"] = {"mean": score.mean, "frames": score.frames, "model": score.model}
                log(f"  [VMAF] {s.name}: {score.mean:.2f}/100 · {score.frames} 帧 · libvmaf {score.model}")
                if args.vmaf_min is not None:
                    good = good and score.mean >= args.vmaf_min
            except VMAFError as exc:
                entry["vmaf_error"] = str(exc)
                log(f"  [VMAF 失败] {s.name}: {exc}")
                good = False
        entry["status"] = "passed" if good else "failed"
        checks.append(entry)
        ok += good
        bad += (not good)
        log(f"  {'[达标]' if good else '[不达标]'} {s.name}: "
            f"pHash距离 {d:.1f}/64（门槛 {phash_min:g}）  PSNR {q:.1f}dB（门槛 {args.psnr_min}）")
    log("")
    log("=" * 56)
    log(f"{'自检达标':<10}{ok:>6}   {'不达标':<8}{bad:>4}   {'无产物':<8}{miss:>4}")
    log(f"{'总耗时':<10}{time.time() - t0:>6.1f}s")
    log("=" * 56)
    if args.vmaf and not args.no_report:
        report = Path(args.out_dir).expanduser() if args.out_dir else config.DEDUP_OUT
        try:
            report.mkdir(parents=True, exist_ok=True)
            target = report / "vmaf-check.json"
            import tempfile  # noqa: PLC0415
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=report,
                                             prefix=".vmaf-", suffix=".tmp", delete=False) as temp:
                json.dump({"mode": "read_only", "threshold": args.vmaf_min,
                           "checks": checks, "passed": ok, "failed": bad,
                           "missing": miss, "interrupted": bool(STOP.flag)},
                          temp, ensure_ascii=False, indent=2)
                pending = Path(temp.name)
            pending.replace(target)
            log(f"[VMAF 报告] {target}")
        except OSError as exc:
            log(f"[VMAF 报告失败] {exc}")
            return 2
    if STOP.flag:
        log("[中断] 自检未完整完成（退出码 130）")
        return 130
    return 0 if bad == 0 and miss == 0 else 2


def run(args: argparse.Namespace) -> int:
    # Extra audio is strictly opt-in. Legacy no-flag requests follow the same
    # existing CLI, encoder, self-check and report path.
    audio_mix = AudioMixOptions(
        bgm=getattr(args, "bgm", ""),
        ambience=getattr(args, "ambience", ""),
        voiceover=getattr(args, "voiceover", ""),
        original_gain=getattr(args, "original_gain", 1.0),
        bgm_gain=getattr(args, "bgm_gain", 0.25),
        ambience_gain=getattr(args, "ambience_gain", 0.30),
        voice_gain=getattr(args, "voice_gain", 1.0),
        fade_out=getattr(args, "audio_fade_out", 0.0),
    )
    try:
        validate_audio_options(audio_mix, check_only=bool(args.check))
    except AudioMixError as exc:
        log(f"[音轨参数错误] {exc}")
        return 2
    if args.vmaf_min is not None and (not args.vmaf or not 0 <= args.vmaf_min <= 100):
        log("[错误] --vmaf-min 必须在 --vmaf 下使用，范围 0~100")
        return 2
    if args.vmaf and not args.check:
        log("[错误] --vmaf 当前仅支持 --check 自检，不执行或覆盖视频")
        return 2
    if args.check and args.in_place:
        log("[错误] --check 不能与 --in-place 合用：原始视频已覆盖后无法做源/产物比较")
        return 2
    # Validate local profile before probing any video, accessing ffmpeg or
    # performing writes. An invalid account must never silently use defaults.
    try:
        profile_snapshot = apply_account_profile(args)
    except ProfileError as exc:
        log(f"[账号预设错误] {exc}")
        return 2
    install_signal_handlers()
    reset_stop()
    self_check(require_src=False)

    if audio_mix.enabled:
        try:
            preflight_external_audio(audio_mix)
        except AudioMixError as exc:
            log(f"[音轨输入错误] {exc}")
            return 2

    srcs = _pick_srcs(args)
    from ..core.output_paths import sibling, validate_targets
    if args.src_dir and not args.out_dir:
        args.out_dir = str(sibling(Path(args.src_dir), "去重"))
    if args.source_stems is not None:
        import re
        srcs = [p for p in srcs if any(re.fullmatch(re.escape(stem) + r"_[1-9][0-9]*", p.stem)
                                      for stem in args.source_stems)]
    try:
        if not args.in_place:
            validate_targets([(p, resolve_output_path(p, DedupOptions(out_dir=args.out_dir))) for p in srcs])
    except ValueError as exc:
        log(f"[错误] {exc}")
        return 2
    if not srcs:
        log("[错误] 所选范围没有可处理的视频")
        return 1
    log(f"[范围] {len(srcs)} 个视频   档位 {args.preset}")
    if profile_snapshot is not None:
        log(f"[账号预设] {profile_snapshot['account']} · version={profile_snapshot['version']}")

    if args.check:
        return _check_only(srcs, args)

    opts = DedupOptions(
        preset=args.preset,
        seed=(args.seed or None),
        mirror=args.mirror,
        speed=args.speed,
        vary_bitrate=args.vary_bitrate,
        ken_burns=args.ken_burns,
        audio_mix=audio_mix,
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

    if profile_snapshot is not None:
        # Preserve effective options and their source for reproducible QA.
        # The whitelist deliberately excludes output paths and credentials.
        for item in items:
            item["account_profile"] = profile_snapshot

    if not args.no_report:
        path = write_report(items, report_dir=Path(args.out_dir) if args.out_dir else None)
        log(f"{'报告':<12}{path}")
    log("=" * 62)
    if STOP.flag:
        log("[中断] 去重未完整完成（退出码 130）")
        return 130
    return 0 if bad == 0 else 2
