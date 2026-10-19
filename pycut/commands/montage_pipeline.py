"""Orchestrate reference analysis, background, optional PIP/subtitles, audio, and intro.

Delegate media operations to core modules. Keep intermediates isolated and publish
only after all required stages succeed; report stage failures to the command layer."""
from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..core.progress import progress
from ..core.utils import log
from ..core.runtime import check_stop


@dataclass
class MontageOptions:
    prepared: dict | None = field(default=None, repr=False)  # Preparation is shared only within this batch of variants.
    step: str = "all"
    source: Path = field(default_factory=lambda: config.MONTAGE_SRC)
    clip_dir: Path | list[Path] = field(default_factory=lambda: config.CLIP_DIRS)
    dedup_dir: Path | list[Path] = field(default_factory=lambda: config.PIP_DIRS)
    out_dir: Path = field(default_factory=lambda: config.MONTAGE_OUT)
    intro_dir: Path | list[Path] | None = None
    seed: int | None = None
    allow_landscape: bool = False
    pip_slot: float = 2.5
    use_pip: bool = True
    pip_continuous: bool = True
    keep_audio: bool = True
    mirror_chance: float = 0.0
    speed_range: tuple | None = None  # Optional source speed interval; None leaves timing unchanged.
    sticker_dir: str = ""          # Empty selects the configured/default sticker pool.
    sticker_chance: float = 0.0
    online_stickers: bool = False  # CLI/TUI download by default; an explicit directory selects local images.
    seam_fade: float = 0.0
    srt: str = str(config.MONTAGE_SUB_PATH)   # Empty disables subtitle burning.
    crf: str = "20"
    fill_ratio: float = config.MONTAGE_TARGET_FILL_RATIO
    noise_db: float = config.MONTAGE_SILENCE_NOISE_DB
    min_silence: float = config.MONTAGE_SILENCE_MIN_DUR
    min_clip: float = config.MONTAGE_CLIP_MIN_DUR
    max_clip: float = config.MONTAGE_CLIP_MAX_DUR
    pip_scale: float = config.MONTAGE_PIP_SCALE
    workers: int = 0
    out_name: str = ""


def run_pipeline(opts: MontageOptions) -> dict:
    """Run a/b/c/all and return summary fields or an error/cancelled result.

    Fields include source_duration, segments, clips_used, clips_reused, pip_count,
    final_duration, final, and base (the retained background for a successful step b)."""
    # Own the background from its creation onward, including errors/cancellation
    # before the final-render block. Only successful --step b keeps its file.
    temporary: list[Path] = []
    try:
        check_stop()
        return _run_pipeline(opts, temporary)
    except InterruptedError as exc:
        log(f"[中断] {exc}")
        return {"error": str(exc), "final": "", "cancelled": True}
    finally:
        for path in temporary:
            _unlink(path)


def _run_pipeline(opts: MontageOptions, temporary: list[Path]) -> dict:
    from ..core.dedup_presets import derive_seed  # noqa: PLC0415
    from ..core.montage_clips import (concat_clips,  # noqa: PLC0415
                                      pick_clips, scan_library)
    from ..core.montage_pip import (build_pip_cmd,  # noqa: PLC0415
                                    plan_pip_timeline, scan_dedup_pool)
    from ..core.montage_source import (plan_clip_slots,  # noqa: PLC0415
                                       probe_source)

    seed = opts.seed if opts.seed else derive_seed(opts.source)
    log(f"[seed] {seed}")

    tmp = Path(config.MONTAGE_TMP)
    tmp.mkdir(parents=True, exist_ok=True)
    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Stage A: reference profile and timing budget.
    progress("混剪合成", 0, 5, detail="分析爆款时长与原声")
    log("--- 步骤A：提取话术节奏与时长基准 ---")
    prepared = opts.prepared if opts.prepared is not None else {}
    if "source" not in prepared:
        prepared["source"] = probe_source(opts.source, noise_db=opts.noise_db,
                                          min_silence=opts.min_silence)
    prof = prepared["source"]
    check_stop()
    if prof is None:
        log(f"  [失败] 基准视频探测失败: {opts.source}")
        return {"source_duration": 0.0, "segments": 0, "clips_used": 0,
                "final_duration": 0.0, "final": "", "error": "步骤A 失败"}
    log(f"  基准: {prof.duration:.2f}s  {prof.width}x{prof.height}  "
        f"语音段 {len(prof.segments)} 个  有声 {prof.speech_total:.1f}s")

    canvas = config.MONTAGE_CANVAS
    # Retain the complete reference duration.
    target = prof.duration
    if opts.step not in ("a", "b") and opts.keep_audio and not prof.has_audio:
        error = "基准视频无音轨，无法保留原声；未发布成片"
        log(f"  [失败] {error}")
        return {"error": error, "final": ""}
    # Prefer subtitle rhythm for shot budgets; rendering keeps original cue timestamps separately.
    slots: list[tuple[float, float]] = []
    if opts.srt and Path(opts.srt).is_file():
        from ..core.montage_subtitles import parse_srt  # noqa: PLC0415
        try:
            cues = parse_srt(opts.srt)
            if opts.step not in ("a", "b") and not any(
                    c.end > 0 and c.start < target and c.end > c.start for c in cues):
                raise ValueError("字幕为空或没有处于话术时间轴内的有效字幕")
            if cues:
                # Use the subtitle readability floor, not the longer clip-library minimum.
                sub_min = config.MONTAGE_SUB_MIN_READABLE
                slots = _slots_from_cues(cues, target, sub_min)
                log(f"  按字幕切槽: {len(cues)} 条字幕 -> {len(slots)} 个槽，"
                    f"累计 {sum(b - a for a, b in slots):.2f}s（目标 {target:.2f}s）")
        except (ValueError, OSError) as e:
            if opts.step not in ("a", "b"):
                error = f"步骤D 字幕准备失败: {e}"
                log(f"  [失败] {error}")
                return {"error": error, "final": ""}
            log(f"  [提示] 字幕解析失败（{type(e).__name__}），退回语音节奏切槽")
            slots = []

    if not slots:
        slots = plan_clip_slots(prof.segments, target, fill_ratio=1.0,
                                min_slot=opts.min_clip, max_slot=opts.max_clip)
        slot_total = sum(b - a for a, b in slots)
        log(f"  按语音切槽: {len(slots)} 个，累计 {slot_total:.2f}s"
            f"（目标 {target:.2f}s）")
        # Background music can leave gaps in speech detection; fill the uncovered duration.
        if slot_total < target - opts.min_clip:
            need = target - slot_total
            extra = _fill_slots(slots, need, opts.min_clip, opts.max_clip,
                                prof.duration)
            log(f"  语音段只覆盖 {slot_total:.2f}s，缺口 {need:.2f}s "
                f"→ 补 {len(extra)} 个连续槽")
            slots.extend(extra)
        log(f"  最终时间槽 {len(slots)} 个，累计 "
            f"{sum(b - a for a, b in slots):.2f}s（目标 {target:.2f}s）")

    # Pause/tail slots supply duration budget, not reference pictures or black filler frames.
    slots = _complete_slot_budget(slots, target, opts.max_clip)
    log(f"  时长预算 {sum(b - a for a, b in slots):.3f}s / {target:.3f}s")

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

    # Stage B: random background selection and encoding.
    check_stop()
    progress("混剪合成", 1, 5, detail="选片、扰动与背景合成")
    log("--- 步骤B：随机混剪拼接 ---")
    if "stickers" not in prepared:
        prepared["stickers"] = _load_stickers(opts.sticker_dir) if opts.sticker_chance > 0 else []
        if opts.online_stickers and opts.sticker_chance > 0:
            from ..core.online_stickers import download_stickers
            try:
                directory = tempfile.TemporaryDirectory(prefix="stickers_", dir=tmp)
                prepared.setdefault("temporary_dirs", []).append(directory)
                prepared["stickers"] = download_stickers(Path(directory.name))
            except (OSError, ValueError) as exc:
                result["error"] = f"在线贴纸下载失败：{type(exc).__name__}"
                log(f"  [失败] {result['error']}；未发布成片")
                return result
    stickers = prepared["stickers"]
    if opts.sticker_chance > 0 and not stickers:
        directory = opts.sticker_dir or str(config.MONTAGE_STICKER_DIR)
        result["error"] = f"启用了贴纸但目录没有可用图片：{directory}"
        log(f"  [失败] {result['error']}（请检查路径或显式使用 --no-sticker）")
        return result
    if stickers:
        log(f"  贴纸素材 {len(stickers)} 张（叠在片段上：背面半透明 + 边缘羽化）")
    if "background" not in prepared:
        prepared["background"] = scan_library(opts.clip_dir, min_dur=opts.min_clip,
                                               max_dur=opts.max_clip, workers=opts.workers,
                                               allow_landscape=opts.allow_landscape)
    items = prepared["background"]
    check_stop()
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
    # Use seeded per-segment variation choices.
    _apply_variation(picked, seed, opts, stickers)
    reused = sum(1 for p in picked if p.get("reused"))
    if reused:
        result["error"] = "唯一素材不足，拒绝重复使用片段；请增加背景素材"
        log(f"  [失败] {result['error']}")
        return result
    total = sum(p["take"] for p in picked)
    log(f"  选中 {len(picked)} 个片段（复用 {reused}）累计 {total:.2f}s")
    if picked and picked[0].get("shortfall", 0.0) > 1.0 / config.MONTAGE_FPS:
        result["error"] = f"可用片段不足以填满基准时长（缺 {picked[0]['shortfall']:.2f}s）"
        log(f"  [失败] {result['error']}；拒绝产出缩水成片")
        return result

    # Plan the actual PiP coverage before any encoding, not just pool existence.
    frame_tolerance = 1.0 / config.MONTAGE_FPS + 0.01
    timeline = []
    if opts.use_pip and opts.step != "b":
        if "pip" not in prepared:
            prepared["pip"] = scan_dedup_pool(opts.dedup_dir, min_dur=opts.min_clip)
        if not prepared["pip"]:
            result["error"] = f"画中画素材目录没有可用 MP4/MOV/M4V：{opts.dedup_dir}"
            log(f"  [失败] {result['error']}（请检查素材长度/路径或显式使用 --no-pip）")
            return result
        timeline = plan_pip_timeline(prepared["pip"], target, slot=opts.pip_slot,
                                     seed=seed, continuous=opts.pip_continuous)
        # The continuous planner already returns adjacent full shots extending
        # beyond the body. Keep that surplus for a single final trim.
        cov = sum(t["duration"] for t in timeline)
        log(f"  画中画 {len(timeline)} 段，累计 {cov:.1f}s / {target:.1f}s"
            f"（覆盖率 {cov / target * 100:.0f}%"
            f"{'，连续模式' if opts.pip_continuous else ''}）")
        if not timeline or (opts.pip_continuous and target - cov > frame_tolerance):
            result["error"] = (
                f"画中画素材不足：需覆盖 {target:.2f}s，实际 {cov:.2f}s")
            log(f"  [失败] {result['error']}；拒绝输出没有画中画的成片")
            return result

    check_stop()
    fd, location = tempfile.mkstemp(prefix=f"base_{seed}_", suffix=".mp4", dir=tmp)
    os.close(fd)
    base = Path(location)
    temporary.append(base)

    r = concat_clips(picked, base, canvas=canvas, fps=config.MONTAGE_FPS,
                     crf=opts.crf, **({"seam_fade": opts.seam_fade} if opts.seam_fade else {}))
    check_stop()
    if not r.get("ok") or not base.is_file():
        log(f"  [失败] 拼接失败: {r.get('error') or '产物未生成'}")
        result["error"] = "步骤B拼接失败"
        _unlink(base)
        return result
    base_duration = _duration(base)
    if abs(base_duration - target) > frame_tolerance:
        result["error"] = (f"步骤B时长错误：基准 {target:.3f}s，拼接 {base_duration:.3f}s")
        log(f"  [失败] {result['error']}")
        _unlink(base)
        return result
    log(f"  步骤B 产出: {base.name}  {_size(base)}  {base_duration:.3f}s")
    result["base"] = str(base)
    result["clips_used"] = len(picked)
    result["clips_reused"] = reused
    result["stickers"] = sum(1 for p in picked if p.get("sticker_applied"))
    if result["stickers"]:
        log(f"  其中 {result['stickers']}/{len(picked)} 段叠了贴纸")

    if opts.step == "b":
        log("  --step b：停在步骤B 产物")
        result["final"] = str(base)
        result["final_duration"] = target
        temporary.remove(base)
        return result

    final = out_dir / (opts.out_name or f"{opts.source.stem}_1.mp4")
    # Stages C/D/E operate on private files; publish only after all required stages succeed.
    working = final.with_name(
        f"{final.stem}.working_{os.getpid()}_{threading.get_ident():x}.mp4")
    staged = final.with_name(
        f"{final.stem}.stage_{os.getpid()}_{threading.get_ident():x}.mp4")
    _unlink(working)
    subtitles_combined = False
    try:
        if not opts.use_pip:
            log("--- 步骤C：--no-pip，跳过画中画 ---")
            cmd = ["-y", "-i", str(base), "-c", "copy", str(staged)]
            if not _publish_stage([config.FFMPEG] + cmd, staged, working):
                log("  [失败] 无声拷贝失败")
                result["error"] = "步骤C 失败"
                return result
        else:
            progress("混剪合成", 2, 5, detail="合成画中画")
            log("--- 步骤C：画中画叠加 ---")
            pool = prepared["pip"]
            log(f"  画中画素材 {len(pool)} 条")
            # build_pip_cmd already executes and publishes; never execute its returned argv again.
            ass_path = None
            try:
                if opts.srt and Path(opts.srt).is_file():
                    ass_path = _prepare_subtitles(opts.srt, canvas, base_duration)
                check_stop()
                build_pip_cmd(base, timeline, working, canvas=canvas,
                              scale=opts.pip_scale, fps=config.MONTAGE_FPS,
                              crf=opts.crf, seed=seed,
                              **({"ass_path": ass_path} if ass_path is not None else {}))
                subtitles_combined = ass_path is not None
            except (RuntimeError, ValueError, OSError) as exc:
                log(f"  [失败] 画中画叠加失败: {exc}")
                result["error"] = f"步骤C 画中画叠加失败: {exc}"
                return result
            finally:
                if ass_path is not None:
                    _unlink(ass_path)
            result["pip_count"] = len(timeline)

        check_stop()
        if not working.is_file() or working.stat().st_size < 1024:
            log("  [失败] 成片未生成或文件异常")
            result["error"] = "成片未生成或文件异常"
            return result

        # Burn subtitles before audio attachment because subtitle intermediates use -an.
        if subtitles_combined:
            progress("混剪合成", 3, 5, detail="字幕已与画中画合并编码")
            log("--- 步骤D：字幕已在画中画最终编码中完成 ---")
        elif opts.srt and Path(opts.srt).is_file():
            progress("混剪合成", 3, 5, detail="烧录参考字幕")
            log("--- 步骤D：字幕烧录 ---")
            if _burn_subtitles(working, slots, canvas, opts):
                log(f"  烧录后: {_size(working)}  {_duration(working):.2f}s")
            else:
                log("  [失败] 字幕烧录失败，未发布成片")
                result["error"] = "步骤D 字幕烧录失败"
                return result
        elif opts.srt:
            log(f"--- 步骤D：跳过（字幕文件不存在: {opts.srt}）---")

        check_stop()
        # Attach reference audio after all silent picture operations.
        if opts.keep_audio and prof.has_audio:
            progress("混剪合成", 4, 5, detail="恢复爆款原声")
            log("--- 步骤E：贴回基准视频音轨（最后一步）---")
            if _attach_audio(working, opts.source, opts.crf):
                result["audio"] = True
                log(f"  已贴回: {_size(working)}  {_duration(working):.2f}s")
            else:
                log("  [失败] 音轨贴回失败，未发布成片")
                result["error"] = "步骤E 音轨贴回失败"
                return result
        elif not prof.has_audio:
            if opts.keep_audio:
                result["error"] = "基准视频无音轨，无法保留原声；未发布成片"
                log(f"  [失败] {result['error']}")
                return result
            log("--- 步骤E：跳过（基准视频无音轨）---")

        check_stop()
        duration = _duration(working)
        check_stop()
        if (duration <= 0 or not working.is_file() or working.stat().st_size < 1024
                or abs(duration - target) > frame_tolerance):
            result["error"] = (
                f"最终成片时长异常（基准 {target:.3f}s / 实际 {duration:.3f}s），"
                "未覆盖原有成片")
            log(f"  [失败] {result['error']}")
            return result
        intro_report = None
        if opts.intro_dir is not None:
            from ..core.montage_intro import prepend
            log("--- 步骤F：前贴去重、贴纸与拼接（保留字幕） ---")
            try:
                intro_report = prepend(working, opts.intro_dir, seed=seed, canvas=canvas,
                                       stickers=stickers, crf=opts.crf,
                                       keep_audio=bool(result.get("audio")))
                duration = _duration(working)
            except (RuntimeError, ValueError, OSError) as exc:
                result["error"] = f"前贴处理失败：{exc}"
                log(f"  [失败] {result['error']}；未发布成片")
                return result
        check_stop()
        try:
            os.replace(str(working), str(final))
        except OSError as exc:
            result["error"] = f"发布最终成片失败: {exc}"
            log(f"  [失败] {result['error']}")
            return result
        result["final"] = str(final)
        result["final_duration"] = duration
        # Only the verified video is published here. No sidecar JSON is written.
        # Diagnostics, including the actual seed, remain in the TUI run log.
        progress("混剪合成", 5, 5, success=1, detail="成片规格检查与发布完成",
                 output=str(final.parent), file=str(final.resolve()))
        log(f"  成片: {final.resolve()}  {_size(final)}  {duration:.2f}s")

        return result

    finally:
        _unlink(working)
        _unlink(staged)
        result["base"] = ""

def _prepare_subtitles(srt, canvas: tuple[int, int], duration: float) -> Path:
    """Build ASS with original speech timing; the caller owns cleanup."""
    from ..core.montage_subtitles import build_ass, parse_srt, SubtitleCue
    cues = parse_srt(srt)
    if not cues:
        raise ValueError("字幕解析为空")
    # Keep original subtitle timestamps because reference audio and body duration are preserved.
    subs = [SubtitleCue(c.index, max(0.0, c.start), min(duration, c.end), c.text)
            for c in cues if c.end > 0 and c.start < duration]
    if not subs:
        raise ValueError("没有处于话术时间轴内的字幕")
    log(f"  字幕 {len(subs)}/{len(cues)} 条，保留爆款原声时间轴")

    ass_path, style = build_ass(subs, canvas=canvas)
    log(f"  样式: {style.get('font_name', '?')} {style.get('font_size', 0):.0f}px "
        f"BorderStyle={config.MONTAGE_SUB_BORDER_STYLE}")

    return Path(ass_path)


def _burn_subtitles(video: Path, slots: list[tuple[float, float]],
                    canvas: tuple[int, int], opts: MontageOptions) -> bool:
    """Encode subtitles separately when PIP is disabled, using the shared ASS preparation."""
    from ..core.montage_subtitles import build_subtitle_cmd, subtitle_tmp_path
    try:
        ass_path = _prepare_subtitles(opts.srt, canvas, _duration(video))
    except (ValueError, OSError) as exc:
        log(f"  [失败] 字幕准备失败：{exc}")
        return False
    out = video.with_name(video.stem + "_sub.mp4")
    try:
        cmd = build_subtitle_cmd(video, Path(ass_path), out, crf=opts.crf,
                                 fps=config.MONTAGE_FPS)
        if not _run(cmd):
            _unlink(subtitle_tmp_path(out))
            return False
        tmp = subtitle_tmp_path(out)
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            log("  [失败] 字幕烧录产物异常")
            _unlink(tmp)
            return False
        os.replace(str(tmp), str(video))
        return True
    finally:
        # Clean temporary ASS on failure as well as success.
        _unlink(ass_path)


def _make_continuous(timeline: list[dict], total: float) -> list[dict]:
    """Repack selected PIP intervals end-to-end without stretching or repeating frames.

    Preserve the continuous-mode short tail when it has real millisecond duration."""
    if not timeline:
        return timeline
    out: list[dict] = []
    cur = 0.0
    for t in timeline:
        d = min(float(t["duration"]), total - cur)
        # Continuous mode keeps real short tails instead of applying the gapped-mode minimum.
        if round(d, 3) <= 0:
            break
        t = dict(t)
        t["start"] = round(cur, 3)
        t["duration"] = round(d, 3)
        t["allow_short"] = True
        out.append(t)
        cur += d
        if cur >= total:
            break
    return out


def _attach_audio(video: Path, audio_src: Path, crf: str = "20") -> bool:
    """Atomically attach reference audio with video/audio stream copy when compatible.

    Do not filter, speed up, or extend audio. Trim excess audio to the picture; short
    audio must not shorten video."""
    tmp = video.with_name(video.stem + f".aud_{os.getpid()}{video.suffix}")
    cmd = [
        config.FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-i", str(audio_src),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "copy", "-t", f"{_duration(video):.6f}",
        "-movflags", "+faststart",
        str(tmp),
    ]
    from ..core.utils import run_cmd
    rc, _, error = run_cmd(cmd, timeout=3600)
    if rc and ("Could not find tag for codec" in error
               or "not currently supported in container" in error):
        log("  [音频兼容] 原声音频编码不支持当前封装，仅转码 AAC；不变速、不变声")
        cmd[cmd.index("-c:a") + 1] = "aac"
        rc, _, error = run_cmd(cmd, timeout=3600)
    if rc or not tmp.is_file() or tmp.stat().st_size < 1024:
        log(f"  [音轨失败] {error.strip()[:2000] or '输出缺失'}")
        _unlink(tmp)
        return False
    if abs(_duration(tmp) - _duration(video)) > 1 / config.MONTAGE_FPS:
        log("  [音轨失败] 贴回音轨后视频时长变化，保留原画面")
        _unlink(tmp)
        return False
    os.replace(str(tmp), str(video))
    return True


def _apply_variation(items: list[dict], seed: int, opts: MontageOptions,
                     stickers: list[Path] | None = None) -> None:
    """Mutate entries with seeded mirror, speed, and sticker choices.

    _normalize_segments reserves longer source intervals for speed effects so the
    body duration remains unchanged."""
    import random  # noqa: PLC0415

    rng = random.Random(seed ^ 0x5EED)     # Separate the effect RNG from clip selection.
    lo, hi = (opts.speed_range or (1.0, 1.0))
    n_mirror = n_speed = n_sticker = 0
    if stickers is None:
        stickers = _load_stickers(opts.sticker_dir)
    first_usable = next((it for it in items if it.get("item") is not None), None)
    from ..core.dedup import montage_variation
    from ..core.dedup_presets import derive_seed
    for index, it in enumerate(items):
        if it.get("item") is None:
            continue
        it["dedup_seed"] = derive_seed(seed, "background", index)
        it["dedup_params"], _chain = montage_variation(*config.MONTAGE_CANVAS, it["dedup_seed"])
        if opts.mirror_chance > 0 and rng.random() < opts.mirror_chance:
            it["mirror"] = True
            n_mirror += 1
        if hi > lo + 1e-3:
            # Speed cannot request source frames beyond the available interval.
            take = float(it.get("take") or 0)
            available = max(0.0, float(it["item"].duration) - float(it.get("offset") or 0))
            cap = min(hi, available / take) if take > 0 else 1.0
            if cap > max(1.0, lo) + 1e-3:
                sp = round(rng.uniform(max(1.0, lo), cap), 4)
                it["speed"] = sp
                n_speed += 1
        if stickers and opts.sticker_chance > 0 and rng.random() < opts.sticker_chance:
            it["sticker"] = str(rng.choice(stickers))
            n_sticker += 1
    # When stickers are explicitly enabled, guarantee at least one hit for small batches.
    if n_sticker == 0 and first_usable is not None and stickers and opts.sticker_chance > 0:
        first_usable["sticker"] = str(rng.choice(stickers))
        n_sticker = 1
    if n_mirror or n_speed or n_sticker:
        log(f"  画面扰动: 镜像 {n_mirror} 段 / 加速 {n_speed} 段"
            + (f"（{lo}~{hi}x）/ 贴纸 {n_sticker} 段" if n_sticker else "")
            + f"  共 {len(items)} 段")


def _load_stickers(sticker_dir: str) -> list[Path]:
    """Load PNG/JPG/JPEG/WebP sticker images."""
    d = Path(sticker_dir) if sticker_dir else config.MONTAGE_STICKER_DIR
    if not d.is_dir():
        return []
    return [p for p in sorted(d.rglob("*"))
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]


def _complete_slot_budget(slots: list[tuple[float, float]], target: float,
                          max_clip: float) -> list[tuple[float, float]]:
    """Fill missing pause/tail duration with bounded slots in order.

    Only slot lengths are consumed; these positions never select reference pictures."""
    from math import ceil
    covered = sum(max(0.0, b - a) for a, b in slots)
    missing = target - covered
    if missing <= 0.5 / config.MONTAGE_FPS:
        return slots
    cap = max(1.0 / config.MONTAGE_FPS, float(max_clip))
    n = max(1, ceil(missing / cap))
    anchor = max((b for _a, b in slots), default=0.0)
    out = list(slots)
    for i in range(n):
        chunk = missing / n
        end = anchor + chunk if i < n - 1 else anchor + missing - (n - 1) * chunk
        out.append((anchor, end))
        anchor = end
    return out


def _slots_from_cues(cues, target: float, min_slot: float) -> list[tuple[float, float]]:
    """Build duration slots from subtitle cues, merging short intervals to avoid flashes.

    Subtitle rendering separately preserves every original timestamp."""
    spans: list[list[float]] = []
    for c in cues:
        s, e = float(c.start), float(c.end)
        if e <= s:
            continue
        if spans and (e - s) < min_slot:
            spans[-1][1] = max(spans[-1][1], e)
            continue
        spans.append([s, e])

    slots: list[tuple[float, float]] = []
    acc = 0.0
    for s, e in spans:
        dur = e - s
        if acc + dur > target:
            e = s + (target - acc)
            if e - s < min_slot:
                break
        slots.append((s, e))
        acc += e - s
        if acc >= target:
            break
    return slots


def _fill_slots(slots: list[tuple[float, float]], need: float,
                min_slot: float, max_slot: float,
                src_duration: float) -> list[tuple[float, float]]:
    """Fill remaining rhythm budget with bounded short slots.

    Background music can prevent silence detection from covering the whole reference.
    Use unoccupied reference intervals as timing budgets without reusing reference pictures."""
    if need <= 0 or max_slot <= 0:
        return []
    used = sorted(slots)
    free: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in used:
        if s > cur:
            free.append((cur, s))
        cur = max(cur, e)
    if cur < src_duration:
        free.append((cur, src_duration))
    free = [(s, e) for s, e in free if e - s >= min_slot]

    # Bound filler slots by max_slot while retaining a fragmented rhythm.
    seg_cap = min(max_slot, max(min_slot, need / 8.0))
    out: list[tuple[float, float]] = []
    left = need
    for fs, fe in free:
        avail = fe - fs
        if avail < min_slot or left <= 0:
            continue
        n = max(1, int(round(avail / seg_cap)))
        seg = avail / n
        while seg > max_slot:
            n += 1
            seg = avail / n
        for i in range(n):
            if left <= 0:
                break
            length = min(seg, left)
            if length < min_slot:
                break
            out.append((fs + i * seg, fs + i * seg + length))
            left -= length
    if out:
        log(f"[步骤A] 连续补槽 {len(out)} 个（每段约 {seg_cap:.1f}s），累计 "
            f"{sum(e - s for s, e in out):.2f}s"
            + (f"，仍缺 {left:.2f}s" if left > 0.05 else ""))
    return out


def _publish_stage(cmd: list[str], staged: Path, final: Path) -> bool:
    """Validate a temporary output before atomic publication; failure preserves prior output."""
    try:
        if not _run(cmd):
            return False
        if not staged.is_file() or staged.stat().st_size < 1024:
            log("  [失败] 阶段产物不存在或不足 1KB，保留原成片")
            return False
        os.replace(str(staged), str(final))
        return True
    except OSError as exc:
        log(f"  [失败] 发布成片失败：{exc}")
        return False
    finally:
        _unlink(staged)


def _run(cmd: list[str]) -> bool:
    """Execute an FFmpeg argument list without a shell; return False on failure."""
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
    from ..core.utils import fmt_bytes  # noqa: PLC0415
    return fmt_bytes(Path(p).stat().st_size)


def _duration(p: Path) -> float:
    from ..core.dedup import _video_duration
    return _video_duration(Path(p))
