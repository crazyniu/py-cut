"""Plan and render optional picture-in-picture over a normalized silent background.

Scan the pool, plan nonoverlapping intervals, then render through build_pip_cmd.
Continuous multi-shot mode prepares lossless small-window segments sequentially
and overlays one concatenated stream. Gapped mode retains explicit intervals.
Keep real source frames through the body frame budget; never fake coverage with
a frozen tail. Preserve aspect ratio, PTS alignment, feathering, shadow, and
segment-level standard variation. Optional ASS shares the final composition."""
from __future__ import annotations

import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack

from .. import config
from .ffmpeg import FFMPEG, probe_video
from .utils import log, run_cmd
from .progress import progress
from .runtime import check_stop


PIP_SLOT = 2.5
PIP_GAP = 0.6
PIP_GAP_JITTER = 0.2
PIP_LEAD_IN = 0.5
PIP_MIN_SHOT = 0.8


PIP_X264_PRESET = "veryfast"
PIP_CRF_DEFAULT = "20"
# Bound per-input decoder/filter threads; do not multiply CPU-wide pools by input count.
# A single shot can stay inline. Every multi-shot continuous track is prepared
# sequentially: input count alone is not a safe decoder/filter memory budget.
# Even 21 shots have stalled on real material below the previous 32-shot limit.
PIP_INLINE_LIMIT = 1
PIP_DECODER_THREADS = 1
PIP_FILTER_THREADS = 1
PIP_ENCODER_THREADS = min(4, os.cpu_count() or 1)
ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600


TMP_SUFFIX = ".piptmp"


def _encoded_frames(path: Path) -> int | None:
    """Read FFmpeg's own -progress file; partial writes do not count as frames."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frames = re.findall(r"(?m)^frame=\s*(\d+)[ \t]*$", data)
    return int(frames[-1]) if frames else None


def _run_with_activity(cmd: list[str], timeout: int, *, interval: float = 10.0,
                       total_frames: int | None = None) -> tuple[int, str, str]:
    """Read *encoded frames* while retaining the existing run_cmd timeout/errors.

    FFmpeg -progress writes a transient file in system temp. The monitor reads
    official frame counts instead of guessing progress from wall time or file
    size. Failure to set up telemetry only disables the counter, not encoding.
    """
    check_stop()
    finished = threading.Event()
    started = time.monotonic()
    progress_file: Path | None = None
    actual_cmd = cmd
    total = int(total_frames or 0)
    if total > 0 and os.environ.get("PYCUT_PROGRESS") == "1":
        try:
            fd, location = tempfile.mkstemp(prefix="pycut_ffmpeg_", suffix=".progress")
            os.close(fd)
            progress_file = Path(location)
            # Global FFmpeg progress flags; do not change any video/audio filter.
            actual_cmd = [*cmd[:-1], "-stats_period", "1",
                          "-progress", str(progress_file), cmd[-1]]
            progress("画中画编码", 0, total, detail="FFmpeg 已启动，等待真实编码帧")
        except OSError as exc:
            log(f"  [画中画进度] 无法创建监控文件，保留等待心跳：{type(exc).__name__}")
    last_frame = 0

    def report() -> None:
        nonlocal last_frame
        last_log = started
        while not finished.wait(min(0.6, max(0.01, interval / 2))):
            if progress_file is not None:
                measured = _encoded_frames(progress_file)
                if measured is not None and measured > last_frame:
                    # 100% is only emitted after FFmpeg and the output are verified.
                    last_frame = min(max(0, measured), total - 1)
                    progress("画中画编码", last_frame, total,
                             detail=f"已编码 {last_frame}/{total} 帧")
            elapsed = time.monotonic() - started
            if elapsed - (last_log - started) >= interval:
                status = (f" · 已编码 {last_frame}/{total} 帧"
                          if progress_file is not None else " · 编码器尚未返回")
                log(f"  [画中画] FFmpeg 正在合成画面 · 已等待 {int(elapsed)}秒"
                    f"{status}（未完成整片校验）")
                last_log = time.monotonic()

    reporter = threading.Thread(target=report, name="pycut-pip-activity", daemon=True)
    reporter.start()
    try:
        rc, out, err = run_cmd(actual_cmd, timeout=timeout)
        check_stop()
        if rc != 0 and progress_file is not None:
            measured = _encoded_frames(progress_file)
            if measured is not None:
                err = f"{err}\n最后编码进度 {measured}/{total} 帧"
        return rc, out, err
    finally:
        finished.set()
        reporter.join(timeout=0.6)
        if progress_file is not None:
            measured = _encoded_frames(progress_file)
            if measured is not None and min(measured, total - 1) > last_frame:
                progress("画中画编码", min(measured, total - 1), total,
                         detail=f"FFmpeg 已编码 {min(measured, total - 1)}/{total} 帧，待校验")
            try:
                progress_file.unlink(missing_ok=True)
            except OSError:
                pass
        log(f"  [画中画] FFmpeg 调用结束 · 用时 {time.monotonic() - started:.1f}秒")

@dataclass
class PipItem:
    """Probed picture-in-picture source metadata."""
    path: Path
    duration: float


def _iter_mp4(root: Path):
    """Collect MP4/MOV/M4V across roots; propagate directory access errors."""
    from .scan import list_videos_recursive
    from .montage_clips import VIDEO_EXTS
    yield from (p for p in list_videos_recursive(root, max_depth=None, raise_on_error=True)
                if p.suffix.lower() in VIDEO_EXTS)


def _probe_duration(path: Path) -> float:
    """Reuse the background metadata query/cache instead of a separate duration probe."""
    from .montage_clips import _probe_one
    item = _probe_one(path)
    return item.duration if item is not None else 0.0


def scan_dedup_pool(root=None, min_dur=None) -> list[PipItem]:
    """Return valid pool items sorted by path for seeded reproducibility.

    Skip individual probe failures and clips below min_dur; surface directory errors."""
    base = root if root is not None else config.PIP_DIRS
    lo = float(min_dur) if min_dur else float(config.MONTAGE_CLIP_MIN_DUR)

    items: list[PipItem] = []
    skipped = 0
    import time
    started = time.monotonic()
    paths = sorted(_iter_mp4(base), key=lambda p: str(p))
    log(f"  [画中画素材] 读取 {len(paths)} 个视频的元数据（未改动文件复用缓存）")
    progress("画中画素材扫描", 0, len(paths), detail="读取画中画时长与规格")
    durations = {}
    with ThreadPoolExecutor(max_workers=max(4, config.MONTAGE_WORKERS)) as executor:
        jobs = {executor.submit(_probe_duration, path): path for path in paths}
        for count, future in enumerate(as_completed(jobs), 1):
            durations[jobs[future]] = future.result()
            if count % 20 == 0 or count == len(paths):
                progress("画中画素材扫描", count, len(paths),
                         detail=f"已探测 {count}/{len(paths)} 条画中画")
    # Restore source order after concurrent probes so seeded planning stays reproducible.
    for path in paths:
        dur = durations[path]
        if dur < lo:
            skipped += 1
            continue
        items.append(PipItem(path=path, duration=dur))

    from .montage_clips import _drop_duplicates
    progress("画中画素材扫描", len(paths), len(paths),
             detail="元数据读取结束，正在检查重复文件")
    items, duplicate_count = _drop_duplicates(items)
    progress("画中画素材扫描", len(paths), len(paths),
             detail=f"画中画扫描完成，可用 {len(items)} 条")
    if duplicate_count:
        log(f"  [画中画去重] 排除 {duplicate_count} 条相同内容")

    log(f"  [画中画素材] {base}  可用 {len(items)} 条 / 跳过(短于 {lo:g}s) {skipped} 条")
    log(f"  [画中画素材] 元数据与重复文件检查耗时 {time.monotonic() - started:.2f}s")
    return items


def plan_pip_timeline(pool: list[PipItem], total_duration: float,
                      slot=None, seed=None, continuous: bool = False) -> list[dict]:
    """Return seeded, nonoverlapping picture-in-picture entries.

    Each entry has item, start, duration, offset, and reached_end. Never reuse a source
    or extend offset + duration beyond it. Continuous mode reserves complete shots
    past the body for rounding slack; gapped mode preserves gap. Mark insufficient
    material only when it cannot cover the required body duration."""
    items = [p for p in (pool or []) if p is not None]
    total = float(total_duration)
    if not items or total <= 0:
        return []

    slot_len = float(slot) if slot else PIP_SLOT
    # Continuous PiP is prepared a shot longer, then cut once at composition.
    # No tiny final source interval has to hit the body endpoint exactly.
    goal = total + slot_len if continuous else total
    rng = random.Random(seed)

    order = list(items)
    rng.shuffle(order)

    used: set[int] = set()
    timeline: list[dict] = []
    t = 0.0 if continuous else min(PIP_LEAD_IN, total)
    reached_end = False

    while True:
        rest = goal - t

        picked = None
        for idx, it in enumerate(order):
            if idx in used or it.duration < PIP_MIN_SHOT:
                continue
            required = slot_len
            if continuous:
                # Extend the current shot if later unused capacity cannot cover the remaining body.
                remaining_capacity = sum(candidate.duration for j, candidate in enumerate(order)
                                         if j not in used and j != idx
                                         and candidate.duration >= PIP_MIN_SHOT)
                required = max(required, rest - remaining_capacity)
            dur = min(required, it.duration)
            if dur < PIP_MIN_SHOT:
                continue
            picked = (idx, it, dur)
            break

        if picked is None:
            reached_end = True
            break
        if rest < (0.01 if continuous else PIP_MIN_SHOT):
            # Continuous mode reserves complete shots; final composition trims to the body budget.
            break

        idx, it, dur = picked
        if not continuous:
            dur = min(dur, rest)
        # Sample offsets within real source bounds, avoiding transitions where possible.
        offset = rng.uniform(0.0, it.duration - dur) if it.duration > dur else 0.0

        used.add(idx)
        # Clamp again after rounding so offset + duration never exceeds available source frames.
        dur_r = round(dur, 3)
        off_r = round(offset, 3)
        dur_r = min(dur_r, it.duration - off_r)

        timeline.append({
            "item": it,
            "start": round(t, 3),
            "duration": dur_r,
            "offset": off_r,
            "reached_end": False,
            "allow_short": continuous and dur < PIP_MIN_SHOT,
        })

        gap = 0.0 if continuous else rng.uniform(PIP_GAP - PIP_GAP_JITTER, PIP_GAP + PIP_GAP_JITTER)
        t += dur_r + gap
        if t >= goal - (0.01 if continuous else PIP_MIN_SHOT):
            break

    covered = sum(e["duration"] for e in timeline)
    # Exhausting only the optional surplus is not a shortage of body footage.
    insufficient = reached_end and covered < total - 1 / config.MONTAGE_FPS
    if timeline and insufficient:
        timeline[-1]["reached_end"] = True
    log(f"  [画中画时间轴] {len(timeline)} 段/ 覆盖 {covered:.2f}s "
        f"/ 成片 {total:.2f}s"
        + ("（素材不足以铺满，已提前收尾）" if insufficient else ""))
    return timeline


def _even(v: float, lo: int = 2) -> int:
    """Round down to an even dimension, bounded below by lo."""
    n = int(v)
    n -= n % 2
    return max(lo, n)


def _ms(v: float) -> str:
    """Format seconds at millisecond precision by truncation, never rounding upward.

    Rounding can push a requested interval past the source end and lose real tail frames."""
    ms = int(v * 1000.0)
    if ms < 0:
        ms = 0
    return f"{ms // 1000}.{ms % 1000:03d}"


def _style_small_pip(parts: list[str], source: str, tag: str,
                     width: int, height: int, fps: int, frames: int,
                     start: float, feather: int, shadow: int,
                     shadow_alpha: float) -> str:
    """Add a feathered PiP with a blurred shadow, entirely at small-window size.

    Feather/shadow geometry is constant for a batch. Render each static mask
    *once* with FFmpeg and use a finite one-frame loop for the segment's
    frame budget. Never clone or extend the actual PiP video frames.
    The only additional overlay operates on the (small) padded shadow canvas.
    """
    frames = max(1, int(frames))
    pts = f"setpts=N/({fps}*TB)+{_ms(start)}/TB"
    fade = (f"(min(1\\,min(X\\,W-1-X)/{feather})"
            f"+min(1\\,min(Y\\,H-1-Y)/{feather}))/2")
    duration = 1.0 / fps
    parts.append(
        f"color=c=white:s={width}x{height}:r={fps}:d={duration:.9f},"
        f"format=gray,geq=lum='255*{fade}',"
        f"loop=loop={frames - 1}:size=1:start=0,{pts}[mask{tag}]"
    )
    parts.append(
        f"color=c=black@0:s={width + 2 * shadow}x{height + 2 * shadow}:"
        f"r={fps}:d={duration:.9f},format=rgba,"
        f"drawbox=x={shadow}:y={shadow}:w={width}:h={height}:"
        f"color=black@{shadow_alpha:.2f}:t=fill:replace=1,"
        f"boxblur=lr=1:lp=0:cr=1:cp=0:ar={shadow}:ap=1,"
        f"loop=loop={frames - 1}:size=1:start=0,{pts}[shadow{tag}]"
    )
    parts.append(f"[{source}]format=rgba[raw{tag}]")
    parts.append(
        f"[raw{tag}][mask{tag}]"
        f"alphamerge=shortest=1:repeatlast=0[feather{tag}]"
    )
    parts.append(
        f"[shadow{tag}][feather{tag}]overlay=x={shadow}:y={shadow}:"
        f"format=auto:shortest=1:eof_action=endall:repeatlast=0[styled{tag}]"
    )
    return f"styled{tag}"


def _clamp_entries(timeline: list[dict]) -> list[dict]:
    """Clamp requested intervals to each source duration and return executable inputs.

    The FFmpeg -t limit provides an additional bound; do not request unavailable frames."""
    out: list[dict] = []
    for entry in (timeline or []):
        item = entry.get("item")
        if item is None:
            continue
        offset = max(0.0, float(entry.get("offset") or 0.0))
        want = float(entry.get("duration") or 0.0)
        avail = float(item.duration) - offset
        dur = min(want, avail)
        if dur < PIP_MIN_SHOT and not entry.get("allow_short"):
            continue
        if dur <= 0:
            continue
        out.append({
            "path": Path(item.path),
            "start": max(0.0, float(entry.get("start") or 0.0)),
            "offset": offset,
            "duration": dur,
        })
    return out


def _tmp_path(out: Path) -> Path:
    """Return an isolated temporary output path."""
    return out.with_name(f"{out.stem}.{os.getpid()}{TMP_SUFFIX}{out.suffix}")


def _ffmpeg_error(stderr: str) -> str:
    """Retain the first cause and trailing context from FFmpeg diagnostics."""
    lines = stderr.strip().splitlines()
    if not lines:
        return "无 FFmpeg 错误输出"
    if len(lines) > 32:
        lines = lines[:24] + [f"… 中间省略 {len(lines) - 32} 行 …"] + lines[-8:]
    return "\n" + "\n".join(lines)


def _encode_cmd(base: Path, tmp: Path, fcomplex: str,
                crf: str, fps: int) -> list[str]:
    return [
        FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-filter_complex_threads", str(PIP_FILTER_THREADS),
        "-reinit_filter", "0",
        "-threads", str(PIP_DECODER_THREADS), "-i", str(base),
        "-filter_complex", fcomplex,
        "-map", "[vout]",
        "-an",
        "-c:v", "libx264", "-threads", str(PIP_ENCODER_THREADS), "-preset", PIP_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps), "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(tmp),
    ]


def build_pip_cmd(base: Path, timeline: list[dict], out: Path,
                  canvas=None, scale=None, margin=None, fps=None,
                  crf: str = PIP_CRF_DEFAULT, seed: int | None = None,
                  ass_path: Path | None = None) -> list[str]:
    """Render and atomically publish picture-in-picture; return the argv already executed.

    canvas/fps define output geometry/timing; scale/margin are canvas fractions and
    crf controls libx264. An empty timeline can copy the base, with re-encode fallback.
    This function executes FFmpeg; callers must not execute the returned argv again."""
    check_stop()
    base = Path(base)
    out = Path(out)
    fps = int(fps or config.MONTAGE_FPS)

    info = probe_video(base)
    if info is None:
        raise ValueError(f"读不出基准视频信息（文件不存在或不是可解码视频）: {base}")

    if canvas:
        W, H = int(canvas[0]), int(canvas[1])
    else:
        W, H = int(info.width), int(info.height)
    if (W, H) != (int(info.width), int(info.height)):
        log(f"  [画中画] 目标画布 {W}x{H} 与 base {info.width}x{info.height} 不一致，"
            f"将按目标画布缩放")

    scale_r = float(scale if scale is not None else config.MONTAGE_PIP_SCALE)
    margin_r = float(margin if margin is not None else config.MONTAGE_PIP_MARGIN)
    pip_w = _even(W * scale_r)
    pip_w = min(pip_w, _even(W))
    margin_x = _even(W * margin_r, lo=0)
    margin_y = _even(H * (float(margin) if margin is not None
                          else config.MONTAGE_PIP_TOP_MARGIN), lo=0)
    pip_h = _even(pip_w * 16 / 9)

    subtitle_filter = "null"
    if ass_path is not None:
        from .montage_subtitles import _escape_filter_path
        if not Path(ass_path).is_file():
            raise ValueError(f"字幕文件不存在: {ass_path}")
        subtitle_filter = f"subtitles=filename='{_escape_filter_path(ass_path)}'"
    entries = _clamp_entries(timeline)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(out)

    if not entries:
        log(f"  [画中画] 时间轴为空，直接转存 base: {base.name}")
        try:
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
                   "-i", str(base), "-map", "0:v:0", "-an",
                   "-c:v", "copy", "-movflags", "+faststart", str(tmp)]
            if ass_path is not None:
                cmd = _encode_cmd(base, tmp,
                                  f"[0:v]scale={W}:{H}:flags=bicubic,setsar=1[bg];[bg]{subtitle_filter}[vout]",
                                  crf, fps)
            rc, _out, err = run_cmd(cmd, timeout=ENC_TIMEOUT_MAX)
            if ass_path is not None and (rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024):
                raise RuntimeError(f"画面与字幕合成失败 rc={rc}: {_ffmpeg_error(err)}")
            if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
                log(f"  [画中画] -c copy 转存失败（rc={rc}），回退重编码")
                # Remove partial copy output before the re-encode fallback.
                if tmp.is_file():
                    tmp.unlink()
                cmd = _encode_cmd(
                    base, tmp,
                    f"[0:v]scale={W}:{H}:flags=bicubic,setsar=1[bg];[bg]null[vout]",
                    crf, fps)
                rc, _out, err = run_cmd(cmd, timeout=ENC_TIMEOUT_MAX)
                if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
                    raise RuntimeError(
                        f"转存 base 失败 rc={rc}: "
                        f"{_ffmpeg_error(err)}")
            check_stop()
            os.replace(str(tmp), str(out))
            return cmd
        finally:
            # Empty-timeline failure needs the same temporary cleanup as normal composition.
            if tmp.is_file():
                tmp.unlink()

    # B already normalizes size/pixel format/FPS. Its copied H.264 segments
    # can still switch colour tags (709/601); FFmpeg's default reinit would
    # reset the entire graph, including the other input's N and static masks.
    # Preserve the continuous PiP timeline across those metadata changes.
    cmd: list[str] = [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
                      "-filter_complex_threads", str(PIP_FILTER_THREADS),
                      "-reinit_filter", "0",
                      "-threads", str(PIP_DECODER_THREADS), "-i", str(base)]
    base_inputs = list(cmd)
    for e in entries:
        cmd += ["-ss", _ms(e["offset"]), "-t", _ms(e["duration"]),
                "-threads", str(PIP_DECODER_THREADS), "-i", str(e["path"])]

    # Avoid resampling an already matching background canvas.
    head = "setsar=1"
    if (W, H) != (int(info.width), int(info.height)):
        head = f"scale={W}:{H}:flags=bicubic,setsar=1"

    parts = [f"[0:v]{head}[bg]"]
    cur = "bg"
    # Prepare lossless small windows sequentially, then overlay one stream onto the main frame.
    budget = max(1, round(info.duration * fps))
    continuous = abs(entries[0]["start"]) < .001 and all(
        abs(right["start"] - (left["start"] + left["duration"])) < .002
        for left, right in zip(entries, entries[1:])) and (
            entries[-1]["start"] + entries[-1]["duration"] >= info.duration - 1 / fps - .002)
    rng = random.Random(seed)
    if seed is not None:
        # Retain standard segment variation on small windows without per-segment full-canvas effects.
        from .dedup import montage_variation
        from .dedup_presets import derive_seed
        log(f"  [画中画去重] standard 几何/像素扰动已启用 · {len(entries)} 段（片段独立 seed）")
    # Historical PiP feather/shadow values restored; draw them on the PiP-sized
    # stream, never as extra full-screen overlays for every input segment.
    feather = max(6, int(pip_w * config.MONTAGE_PIP_FEATHER))
    shadow = max(2, int(pip_w * config.MONTAGE_PIP_SHADOW))
    shadow_alpha = float(config.MONTAGE_PIP_SHADOW_ALPHA)
    labels = []
    prepared_segments = []
    track_tail_start = 0
    for i, e in enumerate(entries):
        # Standard variation does not mirror; this independent 30% flip cannot cancel a second hflip.
        mirror = "hflip" if seed is not None and rng.random() < .3 else ""
        spatial = (f"scale={pip_w}:{pip_h}:force_original_aspect_ratio=increase,"
                   f"crop={pip_w}:{pip_h},setsar=1")
        if seed is not None:
            # Reuse the shared effects builder without altering segment timing, FPS, or audio.
            _params, variation = montage_variation(
                pip_w, pip_h, derive_seed(seed, "pip", i))
            if variation:
                spatial += f",{variation},setsar=1"
        if mirror:
            spatial += f",{mirror}"
        spatial += ","
        if continuous:
            # Quantize cumulative boundaries; independent segment rounding would accumulate drift.
            start_frame = round(e["start"] * fps)
            end_frame = round((e["start"] + e["duration"]) * fps)
            frames = end_frame - start_frame
            if frames <= 0:
                continue
            chain = (f"fps={fps}:start_time=0:eof_action=pass,{spatial}"
                     f"trim=end_frame={frames},setpts=N/({fps}*TB)")
            prepared_segments.append((e, chain, frames))
            parts.append(f"[{i + 1}:v]{chain}[pi{i}]")
            labels.append(f"[pi{i}]")
        else:
            start = e["start"]
            end = start + max(0.0, e["duration"] - 0.001)
            parts.append(f"[{i + 1}:v]{spatial}setpts=PTS-STARTPTS+{_ms(start)}/TB[pi{i}]")
            styled = _style_small_pip(
                parts, f"pi{i}", f"seg{i}", pip_w, pip_h, fps,
                max(1, round(e["duration"] * fps) + 1), start,
                feather, shadow, shadow_alpha)
            # The padded shadow extends beyond the unchanged PiP picture
            # boundaries. Compensate its padding so the picture stays aligned.
            parts.append(
                f"[{cur}][{styled}]"
                f"overlay=x=W-w-{margin_x}+{shadow}:y={margin_y}-{shadow}"
                f":enable='between(t,{_ms(start)},{_ms(end)})'"
                f":eof_action=pass:repeatlast=0:shortest=0[v{i}]"
            )
            cur = f"v{i}"
    if continuous:
        # Mark the real tail on the visible stream; a separate consumer can buffer the whole track.
        # Background duration alone does not prove PIP coverage; never freeze-pad missing tail frames.
        parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[piptrack]")
        track_tail_start = len(parts)
        parts.append(f"[piptrack]metadata=mode=add:key=pycut.complete:value=1:"
                     f"enable='eq(n,{budget - 1})',"
                     f"metadata=mode=print:key=pycut.complete:file='pipe\\:2':"
                     f"enable='eq(n,{budget - 1})'[pipvisible]")
        styled = _style_small_pip(
            parts, "pipvisible", "merged", pip_w, pip_h, fps,
            budget, 0.0, feather, shadow, shadow_alpha)
        # This is still the sole full-size overlay in the continuous mode.
        parts.append(
            f"[bg][{styled}]"
            f"overlay=x=W-w-{margin_x}+{shadow}:y={margin_y}-{shadow}"
            f":eof_action=pass:repeatlast=0:shortest=0[vmerged]")
        cur = "vmerged"
    parts.append(f"[{cur}]{subtitle_filter}[vout]")
    fcomplex = ";".join(parts)

    log(f"  [画中画] {len(entries)} 段/ 画中画 {pip_w}x{pip_h}"
        f" / 右上角留白 x={margin_x} y={margin_y}"
        f" / 羽化 {feather}px / 阴影 {shadow}px")

    input_end = len(cmd)
    cmd += [
        "-filter_complex", fcomplex,
        "-map", "[vout]",
        "-an",
        "-c:v", "libx264", "-threads", str(PIP_ENCODER_THREADS), "-preset", PIP_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps), "-fps_mode", "cfr",
        # Enforce the body frame budget despite framesync/CFR adding a secondary-stream tail frame.
        "-frames:v", str(max(1, round(info.duration * fps))),
        "-movflags", "+faststart",
        str(tmp),
    ]

    timeout = min(ENC_TIMEOUT_MAX,
                  max(ENC_TIMEOUT_MIN, int(info.duration * ENC_TIMEOUT_PER_SEC)))
    try:
        with ExitStack() as resources:
            if continuous and len(entries) > PIP_INLINE_LIMIT:
                from .montage_pip_track import prepare_track
                directory = Path(resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="pycut_pip_track_")))
                log(f"  [画中画] {len(entries)} 段采用逐段无损小窗预处理，"
                    "同时仅解码 1 条原素材；最终仅叠加 2 路视频")
                track = prepare_track(prepared_segments, directory, fps=fps,
                                      ffmpeg=FFMPEG, run=_run_with_activity,
                                      timeout=timeout, until_frames=budget + 1)
                final_parts = [parts[0], f"[1:v]setpts=N/({fps}*TB)[piptrack]",
                               *parts[track_tail_start:]]
                cmd = [*base_inputs, "-f", "concat", "-safe", "1",
                       "-threads", str(PIP_DECODER_THREADS), "-i", str(track),
                       *cmd[input_end:]]
                cmd[cmd.index("-filter_complex") + 1] = ";".join(final_parts)
            rc, _out, err = _run_with_activity(cmd, timeout=timeout, total_frames=budget)
        if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            raise RuntimeError(
                f"画中画叠加失败 rc={rc}: "
                f"{_ffmpeg_error(err)}")
        if continuous and "pycut.complete=1" not in err.splitlines():
            raise RuntimeError("画中画真实帧不足，拒绝克隆尾帧或发布缺失画中画的成片")
        check_stop()
        os.replace(str(tmp), str(out))
        progress("画中画编码", budget, budget, success=1,
                 detail="编码完成，画中画末帧校验与发布成功")
    finally:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass
    return cmd
