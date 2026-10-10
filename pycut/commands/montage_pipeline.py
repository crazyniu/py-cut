"""混剪编排层：把步骤 A/B/C 串成一条流水线，并产出最终成片。

设计原则（沿用 slice_pipeline 的做法）：
  - 编排只做「调度 + 汇总」，具体能力全在 core/montage_*.py 里，不重复实现；
  - 任何单步失败都不让整个命令崩，错误写进返回值由命令层汇总；
  - 中间产物落在 config.MONTAGE_TMP（不进桌面），只有最终成片写 out_dir。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..core.utils import log


@dataclass
class MontageOptions:
    step: str = "all"
    source: Path = field(default_factory=lambda: config.MONTAGE_SRC)
    clip_dir: Path = field(default_factory=lambda: config.MONTAGE_CLIP_DIR)
    dedup_dir: Path = field(default_factory=lambda: config.MONTAGE_DEDUP_DIR)
    out_dir: Path = field(default_factory=lambda: config.MONTAGE_OUT)
    seed: int | None = None
    allow_landscape: bool = False
    pip_slot: float = 2.5
    use_pip: bool = True
    pip_continuous: bool = True   # 画中画首尾相接（不设空档）
    keep_audio: bool = True   # 贴回基准视频原声（成片必须有话术音轨）
    mirror_chance: float = 0.0     # 片段随机镜像概率（0=不镜像）
    speed_range: tuple | None = None  # 片段随机加速区间，如 (1.2, 1.5)；None=不加速
    sticker_dir: str = ""          # 贴纸目录；空=自动探测 config.MONTAGE_STICKER_DIR
    sticker_chance: float = 0.0    # 每个片段叠贴纸的概率
    srt: str = str(config.MONTAGE_SUB_PATH)   # 字幕文件；空串=不烧字幕
    crf: str = "20"
    fill_ratio: float = config.MONTAGE_TARGET_FILL_RATIO
    noise_db: float = config.MONTAGE_SILENCE_NOISE_DB
    min_silence: float = config.MONTAGE_SILENCE_MIN_DUR
    min_clip: float = config.MONTAGE_CLIP_MIN_DUR
    max_clip: float = config.MONTAGE_CLIP_MAX_DUR
    pip_scale: float = config.MONTAGE_PIP_SCALE
    workers: int = 0
    out_name: str = ""   # 覆盖默认成片名（批量裂变时给每支唯一名）；空=用 混剪_{stem}.mp4


def run_pipeline(opts: MontageOptions) -> dict:
    """按opts.step 跑 a / b / c / all，返回汇总字典。

    返回键：source_duration / segments / clips_used / clips_reused /
            pip_count / final_duration / final / base（步骤B 中间产物路径）
    """
    from ..core.dedup_presets import derive_seed  # noqa: PLC0415 复用已有的 seed 派生
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

    # ---------------- 步骤A：基准 ----------------
    log("--- 步骤A：提取话术节奏与时长基准 ---")
    prof = probe_source(opts.source)
    if prof is None:
        log(f"  [失败] 基准视频探测失败: {opts.source}")
        return {"source_duration": 0.0, "segments": 0, "clips_used": 0,
                "final_duration": 0.0, "final": "", "error": "步骤A 失败"}
    log(f"  基准: {prof.duration:.2f}s  {prof.width}x{prof.height}  "
        f"语音段 {len(prof.segments)} 个  有声 {prof.speech_total:.1f}s")

    # 画布以基准视频实测为准（config的 MONTAGE_CANVAS 只是默认值）
    canvas = (prof.width, prof.height)
    target = prof.duration * opts.fill_ratio
    # 有字幕时，**以字幕为节奏骨架**切槽——字幕条数（56）远多于语音段（7），
    # 一槽一字幕才能把话术完整带上；否则 42 条字幕会因槽不够而丢失。
    # 无字幕时才退回「语音段 + 连续补槽」。
    slots: list[tuple[float, float]] = []
    if opts.srt and Path(opts.srt).is_file():
        from ..core.montage_subtitles import parse_srt  # noqa: PLC0415
        try:
            cues = parse_srt(opts.srt)
            if cues:
                # 关键：用**字幕自己的**最小可读时长（0.8s），不是片段的 min_clip(1.2s)。
                # 实测 56 条字幕里 27 条短于 1.2s，按1.2 卡会丢一半；
                # 0.8s 下只剩 8 条过短（86% 可用）。
                sub_min = config.MONTAGE_SUB_MIN_READABLE
                slots = _slots_from_cues(cues, target, sub_min)
                log(f"  按字幕切槽: {len(cues)} 条字幕 -> {len(slots)} 个槽，"
                    f"累计 {sum(b - a for a, b in slots):.2f}s（目标 {target:.2f}s）")
        except Exception as e:  # noqa: BLE001 字幕解析失败不该中断混剪
            log(f"  [提示] 字幕解析失败（{type(e).__name__}），退回语音节奏切槽")
            slots = []

    if not slots:
        slots = plan_clip_slots(prof.segments, target, fill_ratio=opts.fill_ratio,
                                min_slot=opts.min_clip, max_slot=opts.max_clip)
        slot_total = sum(b - a for a, b in slots)
        log(f"  按语音切槽: {len(slots)} 个，累计 {slot_total:.2f}s"
            f"（目标 {target:.2f}s）")
        # 基准视频带持续背景音乐，silencedetect 只能切出有限几段（实测 -20dB 只有 7 段
        # 共 43s），槽合计会远小于目标。这里按「剩余量均分成整段」补齐。
        if slot_total < target - opts.min_clip:
            need = target - slot_total
            extra = _fill_slots(slots, need, opts.min_clip, opts.max_clip,
                                prof.duration)
            log(f"  语音段只覆盖 {slot_total:.2f}s，缺口 {need:.2f}s "
                f"→ 补 {len(extra)} 个连续槽")
            slots.extend(extra)
        log(f"  最终时间槽 {len(slots)} 个，累计 "
            f"{sum(b - a for a, b in slots):.2f}s（目标 {target:.2f}s）")

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

    # ---------------- 步骤B：随机混剪 ----------------
    log("--- 步骤B：随机混剪拼接 ---")
    stickers = _load_stickers(opts.sticker_dir)
    if stickers:
        log(f"  贴纸素材 {len(stickers)} 张（叠在片段上：背面半透明 + 边缘羽化）")
    items = scan_library(opts.clip_dir, min_dur=opts.min_clip,
                         max_dur=opts.max_clip, workers=opts.workers,
                         allow_landscape=opts.allow_landscape)
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
    # 画面扰动：按 seed 逐段决定镜像 / 加速 / 贴纸（同一 seed 结果可复现）
    _apply_variation(picked, seed, opts)
    reused = sum(1 for p in picked if p.get("reused"))
    total = sum(p["take"] for p in picked)
    log(f"  选中 {len(picked)} 个片段（复用 {reused}）累计 {total:.2f}s")
    if picked and picked[0].get("shortfall"):
        log(f"  [提示] 素材不足以填满目标，缺口 {picked[0]['shortfall']:.2f}s")

    base = tmp / f"base_{seed}.mp4"
    # 用 montage_clips.concat_clips（内部完成「分段截取 → concat → 原子替换」两阶段）
    r = concat_clips(picked, base, canvas=canvas, fps=config.MONTAGE_FPS, crf=opts.crf)
    if not r.get("ok") or not base.is_file():
        log(f"  [失败] 拼接失败: {r.get('error') or '产物未生成'}")
        result["error"] = "步骤B拼接失败"
        return result
    log(f"  步骤B 产出: {base.name}  {_size(base)}  {r.get('duration', 0):.2f}s")
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
        return result

    # ---------------- 步骤C：画中画叠加 ----------------
    final = out_dir / (opts.out_name or f"混剪_{opts.source.stem}.mp4")
    # C/D/E 均写入独立工作文件，全部成功才发布最终成片。
    working = final.with_name(
        f"{final.stem}.working_{os.getpid()}_{threading.get_ident():x}.mp4")
    staged = final.with_name(
        f"{final.stem}.stage_{os.getpid()}_{threading.get_ident():x}.mp4")
    _unlink(working)
    try:
        if not opts.use_pip:
            log("--- 步骤C：--no-pip，跳过画中画 ---")
            cmd = ["-y", "-i", str(base), "-c", "copy", str(staged)]
            if not _publish_stage([config.FFMPEG] + cmd, staged, working):
                log("  [失败] 无声拷贝失败")
                result["error"] = "步骤C 失败"
                return result
        else:
            log("--- 步骤C：画中画叠加 ---")
            pool = scan_dedup_pool(opts.dedup_dir, min_dur=opts.min_clip)
            log(f"  画中画素材 {len(pool)} 条")
            if not pool:
                log("  [提示] 无画中画素材，直接输出步骤B 结果")
                cmd = [config.FFMPEG, "-y", "-loglevel", "error", "-i", str(base),
                       "-c", "copy", str(staged)]
                if not _publish_stage(cmd, staged, working):
                    log("  [失败] 无画中画素材时的成片拷贝失败")
                    result["error"] = "步骤C 失败"
                    return result
            else:
                timeline = plan_pip_timeline(pool, target, slot=opts.pip_slot,
                                             seed=seed)
                # 用户反馈「画中画不连续」：上游 plan_pip_timeline 每段之间固定留
                # ~0.6s 空档，实测 19% 的时间画面上没有画中画。连续模式下重排时间轴，
                # 让画中画**首尾相接**（gap=0），符合「全程都有」的观感。
                if opts.pip_continuous:
                    timeline = _make_continuous(timeline, target)
                cov = sum(t["duration"] for t in timeline)
                log(f"  画中画 {len(timeline)} 段，累计 {cov:.1f}s / {target:.1f}s"
                    f"（覆盖率 {cov / target * 100:.0f}%"
                    f"{'，连续模式' if opts.pip_continuous else ''}）")
                cmd = build_pip_cmd(base, timeline, staged, canvas=canvas,
                                    scale=opts.pip_scale, fps=config.MONTAGE_FPS,
                                    crf=opts.crf)
                if not _publish_stage(cmd, staged, working):
                    log("  [失败] 画中画叠加失败")
                    result["error"] = "步骤C 失败"
                    return result
                result["pip_count"] = len(timeline)

        if not working.is_file() or working.stat().st_size < 1024:
            log("  [失败] 成片未生成或文件异常")
            result["error"] = "成片未生成或文件异常"
            return result

        # 以下操作仅修改工作文件，已存在的最终成片保持不变。

        # ---------------- 步骤D：字幕烧录（可选） ----------------
        # 顺序很关键：字幕烧录用 `-an`，所以**必须放在贴音轨之前**，
        # 否则刚贴上的基准音轨会被字幕这一步的 -an 抹掉（实测踩过：成片又变静音）。
        if opts.srt and Path(opts.srt).is_file():
            log("--- 步骤D：字幕烧录 ---")
            if _burn_subtitles(working, slots, canvas, opts):
                log(f"  烧录后: {_size(working)}  {_duration(working):.2f}s")
            else:
                log("  [失败] 字幕烧录失败，未发布成片")
                result["error"] = "步骤D 字幕烧录失败"
                return result
        elif opts.srt:
            log(f"--- 步骤D：跳过（字幕文件不存在: {opts.srt}）---")

        # ---------------- 步骤E：贴回基准视频的原始音轨（放最后） ----------------
        # 步骤B/C/D 都用 -an（片段自带的声音是杂音，且字幕烧录会抹掉音轨），
        # 但**基准视频的话术音频必须保留**——用户要求成片时长与话术对齐，
        # 静音成片没法用。这里最后把基准音频贴回去，并按视频长度截断。
        if opts.keep_audio and prof.has_audio:
            log("--- 步骤E：贴回基准视频音轨（最后一步）---")
            if _attach_audio(working, opts.source, opts.crf):
                result["audio"] = True
                log(f"  已贴回: {_size(working)}  {_duration(working):.2f}s")
            else:
                log("  [失败] 音轨贴回失败，未发布成片")
                result["error"] = "步骤E 音轨贴回失败"
                return result
        elif not prof.has_audio:
            log("--- 步骤E：跳过（基准视频无音轨）---")

        duration = _duration(working)
        if duration <= 0 or not working.is_file() or working.stat().st_size < 1024:
            result["error"] = "最终工作产物无效，未覆盖原有成片"
            log(f"  [失败] {result['error']}")
            return result
        try:
            os.replace(str(working), str(final))
        except OSError as exc:
            result["error"] = f"发布最终成片失败: {exc}"
            log(f"  [失败] {result['error']}")
            return result
        result["final"] = str(final)
        result["final_duration"] = duration
        log(f"  成片: {final.name}  {_size(final)}  {duration:.2f}s")

        return result

    finally:
        _unlink(working)

def _burn_subtitles(video: Path, slots: list[tuple[float, float]],
                    canvas: tuple[int, int], opts: MontageOptions) -> bool:
    """把字幕按槽序烧录进成片（字幕在底部，与右上角画中画不冲突）。"""
    from ..core.montage_subtitles import (assign_cues_to_slots,  # noqa: PLC0415
                                          build_ass, build_subtitle_cmd,
                                          parse_srt, subtitle_tmp_path)
    import os  # noqa: PLC0415

    cues = parse_srt(opts.srt)
    if not cues:
        log("  [失败] 字幕解析为空")
        return False
    subs = assign_cues_to_slots(cues, slots, _duration(video))
    if not subs:
        log("  [失败] 没有可用的字幕-槽位对应")
        return False
    log(f"  字幕 {len(cues)} 条 -> 匹配 {len(subs)} 条"
        f"（槽 {len(slots)} 个，{len(slots) - len(subs)} 个过短未配）")

    ass_path, style = build_ass(subs, canvas=canvas)
    log(f"  样式: {style.get('font_name', '?')} {style.get('font_size', 0):.0f}px "
        f"BorderStyle={config.MONTAGE_SUB_BORDER_STYLE}")

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
        os.replace(str(tmp), str(video))   # 原子替换回原成片路径
        return True
    finally:
        # 烧录失败也不能把 ASS 中间产物一直留在磁盘上。
        _unlink(ass_path)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _make_continuous(timeline: list[dict], total: float) -> list[dict]:
    """把画中画时间轴重排为「首尾相接」，消除段间空档。

    上游每段之间留 ~0.6s 空档，实测导致 19% 的时间没有画中画（用户反馈不连续）。
    这里保留每段的时长与素材选择，只把 start 依次紧贴上一段的结束时刻；
    放不下的尾巴（素材用完了）就自然结束，不会拉伸最后一帧。
    """
    if not timeline:
        return timeline
    out: list[dict] = []
    cur = 0.0
    for t in timeline:
        d = float(t["duration"])
        if cur + d > total:
            d = total - cur
            if d < 0.3:            # 尾巴太短不值得单独一段
                break
        t = dict(t)
        t["start"] = round(cur, 3)
        t["duration"] = round(d, 3)
        out.append(t)
        cur += d
        if cur >= total - 0.05:
            break
    return out


def _attach_audio(video: Path, audio_src: Path, crf: str = "20") -> bool:
    """把基准视频的音轨贴回无声的混剪成片（原子替换）。

    只重新编码视频（因为要接新音频流不能 -c copy）；音频用 aac 128k。
    时长以视频为准（`-shortest` 防止音频比画面长导致末尾黑屏）。
    """
    tmp = video.with_name(video.stem + f".aud_{os.getpid()}{video.suffix}")
    cmd = [
        config.FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),        # 无声画面
        "-i", str(audio_src),   # 基准原声
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",             # 以最短流为准，避免音频更长导致末尾黑屏
        "-movflags", "+faststart",
        str(tmp),
    ]
    ok = _run(cmd)
    if not ok or not tmp.is_file() or tmp.stat().st_size < 1024:
        _unlink(tmp)
        return False
    os.replace(str(tmp), str(video))
    return True


def _apply_variation(items: list[dict], seed: int, opts: MontageOptions) -> None:
    """按 seed 给每个片段打上镜像 / 加速 / 贴纸标记（原地修改 items）。

    加速的副作用由`_normalize_segments` 处理：它会把 `take` 乘以 speed，
    从源素材取更久再压缩，保证成片总时长不被加速拉短。
    """
    import random  # noqa: PLC0415

    rng = random.Random(seed ^ 0x5EED)     # 与选片用不同流，避免耦合
    lo, hi = (opts.speed_range or (1.0, 1.0))
    n_mirror = n_speed = n_sticker = 0
    stickers = _load_stickers(opts.sticker_dir)
    for it in items:
        if it.get("item") is None:
            continue
        if opts.mirror_chance > 0 and rng.random() < opts.mirror_chance:
            it["mirror"] = True
            n_mirror += 1
        if hi > lo + 1e-3:
            sp = round(rng.uniform(lo, hi), 4)
            it["speed"] = sp
            n_speed += 1
        if stickers and opts.sticker_chance > 0 and rng.random() < opts.sticker_chance:
            it["sticker"] = str(rng.choice(stickers))
            n_sticker += 1
    if n_mirror or n_speed or n_sticker:
        log(f"  画面扰动: 镜像 {n_mirror} 段 / 加速 {n_speed} 段"
            + (f"（{lo}~{hi}x）/ 贴纸 {n_sticker} 段" if n_sticker else "")
            + f"  共 {len(items)} 段")


def _load_stickers(sticker_dir: str) -> list[Path]:
    """加载贴纸素材目录（png/jpg/jpeg/webp）。"""
    d = Path(sticker_dir) if sticker_dir else config.MONTAGE_STICKER_DIR
    if not d.is_dir():
        return []
    return [p for p in sorted(d.rglob("*"))
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]


def _slots_from_cues(cues, target: float, min_slot: float) -> list[tuple[float, float]]:
    """按字幕条数切时间槽：一条字幕 = 一个槽，槽长 = 该字幕时长。

    这样成片节奏与话术**逐句对齐**，且不会丢字幕（槽数 = 字幕条数）。
    过短的字幕（< min_slot）并入下一条，避免闪一下看不清。
    """
    spans: list[list[float]] = []
    for c in cues:
        s, e = float(c.start), float(c.end)
        if e <= s:
            continue
        if spans and (e - s) < min_slot:
            spans[-1][1] = max(spans[-1][1], e)   # 太短就并入上一句
            continue
        spans.append([s, e])

    slots: list[tuple[float, float]] = []
    acc = 0.0
    for s, e in spans:
        dur = e - s
        if acc + dur > target:
            e = s + (target - acc)                 # 超出目标就截断
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
    """为剩余时长补齐连续时间槽（取自基准视频的连续区间）。

    背景：爆款视频常带持续背景音乐，silencedetect 切不出足够的语音段
    （实测 -32dB 时整条只切出 1 段 8s，目标却是 75.8s）。
    这里把缺口切成**接近 mix 的节奏**的碎槽——混剪观感的关键是「碎片感」，
    一段 13 秒的素材看起来像没剪，所以这里的上限按 min(max_slot, 补齐均分) 控制。
    时间位置从基准视频里**未被语音段占用**的区间顺序取，避免与已有槽重叠。
    """
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

    # 补齐段的粒度：不超过 max_slot，且让段数足够多（碎节奏）。
    # 目标每段 ≈ 4s，但至少要能填满 need。
    seg_cap = min(max_slot, max(min_slot, need / 8.0))
    out: list[tuple[float, float]] = []
    left = need
    for fs, fe in free:
        avail = fe - fs
        if avail < min_slot or left <= 0:
            continue
        n = max(1, int(round(avail / seg_cap)))
        seg = avail / n
        while seg > max_slot:          # 段长超上限就多分几段
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
    """校验临时文件后原子发布，执行失败绝不覆盖已有最终成片。"""
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
    """执行 ffmpeg 命令（列表形式，不经 shell）。失败只返回 False。"""
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
    from ..core.ffmpeg import probe_scalar  # noqa: PLC0415
    try:
        return float(probe_scalar(Path(p), "format=duration"))
    except (TypeError, ValueError):
        return 0.0
