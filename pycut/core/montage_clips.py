"""Scan, select, normalize, and concatenate background clips.

Scan metadata once, filter landscape by default, and deduplicate identical files.
Pick seeded material for duration slots. Encode each selected segment with double
seek, fixed geometry/FPS/SAR/pixel format, and fused effects, then stream-copy concat.
Direct inpoint/outpoint concat or mixed formats can silently truncate output.
Validate duration before atomic publication; never mask missing frames with tpad."""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import threading
import time
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG, FFPROBE
from .utils import log, run_cmd
from .progress import progress
from .probe_cache import cached_probe
from .runtime import check_stop


VIDEO_EXTS = (".mp4", ".mov", ".m4v")

# Read partial signatures only for same-size candidates; confirm collisions with full hashes.
DEDUP_CHUNK = 1 << 20


CRF_DEFAULT = "20"

# Use the same speed preset as downstream effects without changing CRF.
X264_PRESET = "veryfast"


PROBE_TIMEOUT = 60


ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600

# Ignore floating-point summation noise when comparing available duration.
EPS = 1e-6


@dataclass
class ClipItem:
    """Source metadata; duration retains the real source length until selection."""

    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool


def _tmp_root() -> Path:
    """Create and return the configured intermediate-work directory."""
    root = Path(config.MONTAGE_TMP)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"[混剪B] 无法创建临时目录 {root}: {e}")
    return root


def _tmp_path(suffix: str, tag: str) -> Path:
    """Return a process/thread-isolated temporary path."""
    return _tmp_root() / f"{tag}_{os.getpid()}_{threading.get_ident():x}{suffix}"


def _seg_dir(tag: str) -> Path:
    """Return the isolated segment directory for this invocation."""
    return _tmp_root() / f"{tag}_{os.getpid()}_{threading.get_ident():x}"


def fit_filter(canvas: tuple[int, int] | None = None,
               fps: int | None = None) -> str:
    """Fit proportionally inside the canvas and pad centrally; never stretch or crop content."""
    w, h = canvas or config.MONTAGE_CANVAS
    fr = fps or config.MONTAGE_FPS
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fr},format=yuv420p"
    )


def _enc_args(out: Path, fps: int | None, crf: str) -> list[str]:
    """Return shared silent libx264/yuv420p/CFR/faststart encoding arguments.

    Use -fps_mode cfr rather than removed -vsync; retain the veryfast intermediate preset."""
    return [
        "-an",
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-preset", X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps or config.MONTAGE_FPS),
        "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(out),
    ]


def _probe_one(path: Path, *, details: dict | None = None) -> ClipItem | None:
    """Read dimensions, duration, rates, and audio presence with one cached JSON query.

    Return None on invalid metadata/probe failure. Cache only read-only metadata."""
    args = [
        FFPROBE, "-v", "error",
        "-show_entries", "stream=codec_type,width,height,duration,avg_frame_rate:stream_side_data=rotation:stream_tags=rotate",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    rc, out, _err = cached_probe(path, args, PROBE_TIMEOUT, run_cmd)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    if vs is None:
        return None

    def _f(v: object) -> float:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    width = int(_f(vs.get("width")))
    height = int(_f(vs.get("height")))
    # FFmpeg autorotates phone recordings before filtering; classify their displayed canvas.
    rotation = next((side["rotation"] for side in vs.get("side_data_list", [])
                     if "rotation" in side), (vs.get("tags") or {}).get("rotate", 0))
    if round(_f(rotation)) % 180 == 90:
        width, height = height, width
    if width <= 0 or height <= 0:
        return None

    # Probe failures/N/A fall back to container duration; preserve the existing numeric contract.
    dur = _f(vs.get("duration"))
    if dur <= 0:
        dur = _f((data.get("format") or {}).get("duration"))
    if dur <= 0:
        return None

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if details is not None:
        details.update(vs)  # Retain raw video duration for reference timing that must not follow a longer audio stream.
    return ClipItem(path=Path(path), duration=dur, width=width, height=height,
                    has_audio=has_audio)


def _collect_files(root: Path) -> list[Path]:
    """Recursively collect videos with stable ordering and filesystem traversal."""
    from .scan import list_videos_recursive
    return [p for p in list_videos_recursive(root, max_depth=None, raise_on_error=True)
            if p.suffix.lower() in VIDEO_EXTS]


def _content_sig(path: Path) -> str:
    """Hash the first/last _FP_CHUNK bytes for same-size duplicate candidates.

    This is a weak prefilter; only a full content digest can confirm a duplicate."""
    h = hashlib.md5()
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            h.update(f.read(DEDUP_CHUNK))
            if size > 2 * DEDUP_CHUNK:
                f.seek(-DEDUP_CHUNK, os.SEEK_END)
                h.update(f.read(DEDUP_CHUNK))
    except OSError:
        return f"{path.name}:{path.stat().st_size if path.exists() else -1}"
    return h.hexdigest()


def _full_content_sig(path: Path) -> str | None:
    """Hash full contents only for weak-signature candidates; read failures retain the material."""
    h = hashlib.blake2b(digest_size=32)
    try:
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _drop_duplicates(items: list[ClipItem]) -> tuple[list[ClipItem], int]:
    """Return (unique items, removed count) after size, partial-hash, and full-hash grouping.

    Different paths can contain identical footage. Never remove based on a partial hash alone."""
    by_size: dict[int, list[ClipItem]] = {}
    for it in items:
        try:
            by_size.setdefault(it.path.stat().st_size, []).append(it)
        except OSError:
            pass

    out: list[ClipItem] = []
    n_drop = 0
    for size in sorted(by_size):
        group = by_size[size]
        if len(group) == 1:
            out.append(group[0])
            continue
        # A matching partial signature is only a candidate; hash full contents before deduplication.
        seen: dict[str, list[tuple[ClipItem, str | None]]] = {}
        for it in group:
            weak_sig = _content_sig(it.path)
            candidates = seen.setdefault(weak_sig, [])
            if not candidates:
                candidates.append((it, None))
                out.append(it)
                continue
            full_sig = _full_content_sig(it.path)
            duplicate = False
            for index, (prev, prev_full) in enumerate(candidates):
                if prev_full is None:
                    prev_full = _full_content_sig(prev.path)
                    candidates[index] = (prev, prev_full)
                if full_sig is not None and full_sig == prev_full:
                    duplicate = True
                    break
            if duplicate:
                n_drop += 1
            else:
                candidates.append((it, full_sig))
                out.append(it)
    return out, n_drop


def scan_library(root=None, min_dur=None, max_dur=None,
                 workers=None, allow_landscape: bool = False,
                 stats: dict | None = None, *, files=None) -> list[ClipItem]:
    """Return valid clips with stable ordering, metadata, and content deduplication.

    Filter w/h above MONTAGE_MAX_SIDE_RATIO unless allow_landscape is explicit.
    Enforce min_dur here; retain full source duration and apply max_dur at selection.
    Concurrent probes carry explicit indices; skip per-file failures without changing pairing."""
    root = root if root is not None else config.CLIP_DIRS
    lo = float(config.MONTAGE_CLIP_MIN_DUR if min_dur is None else min_dur)
    hi = float(config.MONTAGE_CLIP_MAX_DUR if max_dur is None else max_dur)
    nw = int(workers) if workers else max(4, int(config.MONTAGE_WORKERS))
    ratio_max = float(config.MONTAGE_MAX_SIDE_RATIO)

    files = (_collect_files(root) if files is None else
             sorted({Path(p).resolve() for p in files}, key=str))
    if not files:
        if stats is not None:
            stats.update(files=0, portrait=0, landscape=0, bad=0, short=0, duplicates=0)
        log(f"[混剪B] 片段库里没有视频文件: {root}")
        return []

    log(f"[混剪B] 扫描片段库 {root}")
    log(f"  文件总数 {len(files)}  并发 {nw} 路  "
        f"时长门槛 >={lo:.1f}s（上限 {hi:.1f}s 仅取用时截，不丢素材）")

    t0 = time.time()
    progress("背景素材扫描", 0, len(files), detail="开始读取视频时长与画面规格")
    results: list[ClipItem | None] = [None] * len(files)
    with futures.ThreadPoolExecutor(max_workers=nw) as ex:
        futs = {ex.submit(_probe_one, p): i for i, p in enumerate(files)}
        done = 0
        for fut in futures.as_completed(futs):
            i = futs[fut]                      # Carry explicit indices through asynchronous completion.
            done += 1
            try:
                results[i] = fut.result()
            except Exception as e:             # noqa: BLE001 - isolate invalid library files.
                log(f"  [探测异常] {files[i].name}: {type(e).__name__} {e}")
            if done % 20 == 0 or done == len(files):
                progress("背景素材扫描", done, len(files),
                         detail=f"已探测 {done}/{len(files)} 个素材")
            if done % 100 == 0 or done == len(files):
                log(f"  进度 {done}/{len(files)}")

    items: list[ClipItem] = []
    n_bad = n_land = n_short = 0
    for it in results:
        if it is None:
            n_bad += 1
        elif not allow_landscape and it.width / it.height > ratio_max:
            n_land += 1
        elif it.duration < lo:
            n_short += 1
        else:
            items.append(it)

    progress("背景素材扫描", len(files), len(files),
             detail="元数据探测结束，正在检查重复文件")
    items, n_dup = _drop_duplicates(items)
    if stats is not None:
        stats.update(
            files=len(files),
            portrait=sum(it is not None and it.width / it.height <= ratio_max for it in results),
            landscape=sum(it is not None and it.width / it.height > ratio_max for it in results),
            bad=n_bad, short=n_short, duplicates=n_dup,
        )

    progress("背景素材扫描", len(files), len(files),
             detail=f"素材检查完成，可用 {len(items)} 条")
    log(f"  可用 {len(items)} 条  |  横屏排除 {n_land}  太短排除 {n_short}  "
        f"重复拷贝排除 {n_dup}  探测失败 {n_bad}  |  "
        f"总容量 {sum(i.duration for i in items) / 60:.1f} 分钟"
        f"  |  耗时 {time.time() - t0:.1f}s")
    return items


def _free_offset(dur: float, spans: list[tuple[float, float]],
                 want: float) -> float | None:
    """Return the first deterministic free interval fitting want, or None."""
    cursor = 0.0
    for s, e in sorted(spans):
        if s - cursor >= want - EPS:
            return cursor
        cursor = max(cursor, e)
    if dur - cursor >= want - EPS:
        return cursor
    return None


def pick_clips(items: list[ClipItem], slots: list[tuple[float, float]],
               seed=None, min_dur=None, max_dur=None) -> list[dict]:
    """Allocate seeded clips to duration slots, preferring unused sources.

    Return item, take, offset, slot_index, reused, short, and aggregate target_dur,
    taken_dur, shortfall fields. Reuse only unoccupied intervals after exhausting
    unused sources when allowed. Report shortages; do not manufacture frames."""
    if not items or not slots:
        return []

    lo = float(config.MONTAGE_CLIP_MIN_DUR if min_dur is None else min_dur)
    hi = float(config.MONTAGE_CLIP_MAX_DUR if max_dur is None else max_dur)

    rng = random.Random(seed)  # noqa: S311 - material selection is not a cryptographic operation.

    n = len(items)
    remaining = [float(it.duration) for it in items]
    spans: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    used = [False] * n

    order = list(range(n))
    rng.shuffle(order)                 # Shuffle once and rotate the cursor so selection stays random without starving tail entries.
    cursor = 0

    out: list[dict] = []
    target = 0.0

    for slot_index, pair in enumerate(slots):
        try:
            slot_len = float(pair[1]) - float(pair[0])
        except (TypeError, ValueError, IndexError):
            continue
        if slot_len <= 0:
            continue
        target += slot_len
        want = min(slot_len, hi)

        chosen, take, reused = -1, 0.0, False
        offset = 0.0

        # First pass uses only previously unused sources.
        best_idx, best_take, best_pos = -1, 0.0, cursor
        hit_pos = -1
        for k in range(n):
            pos = (cursor + k) % n
            idx = order[pos]
            if used[idx]:
                continue
            rest = remaining[idx]
            if rest < lo - EPS:
                used[idx] = True
                remaining[idx] = 0.0
                continue
            usable = min(slot_len, rest, hi)
            if usable > best_take:
                best_idx, best_take, best_pos = idx, usable, pos
            if usable >= want - EPS:
                chosen, take, hit_pos = idx, usable, pos
                break
        if chosen < 0 and best_idx >= 0:
            chosen, take, hit_pos = best_idx, best_take, best_pos

        if chosen >= 0:
            offset = rng.uniform(0.0, max(0.0, remaining[chosen] - take))
        else:
            # Only reuse unoccupied source intervals after unused sources are exhausted.
            fb_idx, fb_off, fb_take = -1, 0.0, 0.0
            for idx in order:
                dur = float(items[idx].duration)
                cand = min(want, dur)
                if not used[idx]:
                    off = _free_offset(dur, spans[idx], cand)
                    if off is not None:
                        chosen, offset, take, reused = idx, off, cand, False
                        break
                    continue
                if cand > fb_take:
                    off2 = _free_offset(dur, spans[idx], cand)
                    if off2 is not None:
                        fb_idx, fb_off, fb_take = idx, off2, cand
            if chosen < 0 and fb_idx >= 0:
                chosen, offset, take, reused = fb_idx, fb_off, fb_take, True

        if chosen < 0:
            # Report a shortfall when no free interval fits; never invent source footage.
            out.append({"item": None, "take": 0.0, "offset": 0.0,
                        "slot_index": slot_index, "reused": False, "short": True})
            continue

        remaining[chosen] = max(0.0, remaining[chosen] - take)
        spans[chosen].append((offset, offset + take))
        used[chosen] = True
        out.append({"item": items[chosen], "take": round(take, 3),
                    "offset": round(offset, 3), "slot_index": slot_index,
                    "reused": reused, "short": False})

        if hit_pos >= 0:
            cursor = (hit_pos + 1) % n

    taken = sum(d["take"] for d in out)
    # Fill shortages with unused shots without changing RNG draws on the complete-budget path.
    for idx in order:
        need = target - taken
        # A sub-frame rounding remainder needs no new shot. Encoding such a
        # shot forces at least one frame and would exceed the body's budget.
        if round(taken * config.MONTAGE_FPS) >= round(target * config.MONTAGE_FPS):
            break
        if used[idx] or items[idx].duration < lo - EPS:
            continue
        take = min(need, hi, float(items[idx].duration))
        offset = rng.uniform(0.0, max(0.0, items[idx].duration - take))
        take = round(take, 6)
        offset = min(round(offset, 6), max(0.0, items[idx].duration - take))
        out.append({"item": items[idx], "take": take, "offset": offset,
                    "slot_index": len(slots), "reused": False, "short": False})
        used[idx] = True
        taken += take
    shortfall = max(0.0, target - taken)
    for d in out:
        d["target_dur"] = round(target, 3)
        d["taken_dur"] = round(taken, 3)
        d["shortfall"] = round(shortfall, 3)
    if not out:
        log(f"[混剪B] pick_clips：一个槽都没填上（目标 {target:.2f}s，"
            f"素材全短或全占满）")
    return out


def _write_concat_list(files: list[Path]) -> Path:
    """Write absolute paths with concat-demuxer single-quote escaping."""
    lst = _tmp_path(".txt", "concat")
    quote = "'"
    lines = []
    for f in files:
        safe = str(Path(f).resolve()).replace(quote, quote + "\\" + quote + quote)
        lines.append(f"file {quote}{safe}{quote}")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lst


def _seg_cmd(src: Path, offset: float, take: float, out: Path,
             canvas=None, fps=None, crf: str = CRF_DEFAULT,
             mirror: bool = False, speed: float = 1.0,
             frames: int | None = None, dedup_seed: int | None = None,
             sticker: Path | None = None, sticker_seed: int = 0,
             seam_fade: float = 0.0, first: bool = False, last: bool = False) -> list[str]:
    """Build one double-seek segment encode with fitting, variation, sticker, and fade.

    The caller must reserve take * speed source seconds so acceleration does not
    shorten the requested output interval."""
    off = max(0.0, float(offset))
    coarse = max(0.0, off - float(config.COARSE_BACKOFF))
    factor = float(speed) if abs(speed - 1.0) > 1e-3 else 1.0
    fine = (off - coarse) / factor
    chain = fit_filter(canvas, fps)
    if dedup_seed is not None:
        from .dedup import montage_variation
        _params, variation = montage_variation(*(canvas or config.MONTAGE_CANVAS), dedup_seed)
        # Restore square pixels after geometry effects.
        chain += "," + variation + ",setsar=1"
    # Apply speed before scaling so dropped frames avoid unnecessary processing.
    pre = []
    if abs(speed - 1.0) > 1e-3:
        pre.append(f"setpts=PTS/{float(speed):.5f}")
    if mirror:
        pre.append("hflip")
    if pre:
        chain = ",".join(pre + [chain])
    # Missing source frames must fail duration checks, never be hidden by a frozen tail.
    filters = []
    if seam_fade > 0:
        from .montage_transitions import seam_fade_filters
        duration = frames / float(fps or config.MONTAGE_FPS) if frames is not None else take
        # Output seek follows filtering; shift fades by fine seek or their leading edge is cut away.
        filters = seam_fade_filters(duration, seam_fade, first, last, start=fine)
    inputs = ["-ss", f"{coarse:.3f}", "-i", str(src)]
    if sticker:
        from .montage_sticker import sticker_chain
        rng = random.Random(sticker_seed)
        rng.random()  # Keep sticker selection RNG identical to the prior chance=1 segment path.
        img = rng.choice([Path(sticker)])
        part = sticker_chain(
            img, canvas or config.MONTAGE_CANVAS,
            config.MONTAGE_STICKER_SCALE, config.MONTAGE_STICKER_MARGIN,
            config.MONTAGE_STICKER_OPACITY, pos=rng.choice(["br", "bl", "tl"]),
            fps=fps, video_label="segment", double=True)
        if part is None:
            raise ValueError(f"已指定贴纸但无法读取: {sticker}")
        inputs += ["-i", str(img)]
        graph, label = part
        graph = f"[0:v]{chain}[segment];{graph}"
        if filters:
            graph += f";[{label}]" + ",".join(filters) + "[vout]"
            label = "vout"
        output_filter = ["-filter_complex", graph, "-map", f"[{label}]"]
    else:
        output_filter = ["-vf", ",".join([chain, *filters])]
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-ss", f"{fine:.6f}",
        "-t", f"{max(0.0, float(take)):.6f}",
        *output_filter,
        *([] if frames is None else ["-frames:v", str(frames)]),
        *_enc_args(out, fps, crf),
    ]


def _normalize_segments(items: list[dict], canvas, fps, crf: str,
                        seam_fade: float = 0.0) -> list[Path]:
    """Encode each segment once with bounded parallelism and input-order output.

    Trim unavailable source tails instead of freezing frames. Quantize cumulative
    frame budgets; downstream checks reject insufficient material."""
    check_stop()
    segdir = _seg_dir("seg")
    try:
        shutil.rmtree(segdir, ignore_errors=True)
        segdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建分段目录 {segdir}: {e}") from e

    jobs = []
    planned_seconds = 0.0
    assigned_frames = 0
    frame_rate = float(fps or config.MONTAGE_FPS)
    for i, d in enumerate(items):
        it = d.get("item")
        if it is None or d.get("take", 0.0) <= 0:
            continue
        seg = segdir / f"{i:04d}.mp4"
        speed = float(d.get("speed") or 1.0)
        multiplier = speed if abs(speed - 1.0) > 1e-3 else 1.0
        wanted = float(d["take"]) * multiplier
        available = max(0.0, float(it.duration) - float(d.get("offset") or 0.0))
        # Compare real source availability; formatted decimal rounding must not erode each frame budget.
        src_take = min(wanted, available)
        if src_take <= 0:
            raise RuntimeError(f"分段 {i + 1}/{len(items)} 源视频没有剩余时长: {it.path}")
        if available < wanted - EPS:
            d["take"] = available / multiplier

        planned_seconds += float(d["take"])
        cumulative_frames = round(planned_seconds * frame_rate)
        frames = max(1, cumulative_frames - assigned_frames)
        assigned_frames = cumulative_frames
        timeout = min(ENC_TIMEOUT_MAX,
                      max(ENC_TIMEOUT_MIN, int(src_take * ENC_TIMEOUT_PER_SEC)))
        cmd = _seg_cmd(
            it.path, d["offset"], src_take, seg, canvas, fps, crf,
            mirror=bool(d.get("mirror")), speed=speed, frames=frames,
            **({"dedup_seed": d["dedup_seed"]} if "dedup_seed" in d else {}),
            sticker=Path(d["sticker"]) if d.get("sticker") else None,
            sticker_seed=d.get("dedup_seed", i * 7919 + 13),
            seam_fade=seam_fade if len(items) > 1 else 0.0,
            first=i == 0, last=i == len(items) - 1)
        jobs.append((i, d, seg, src_take, speed, frames, timeout, cmd))

    def encode(job):
        check_stop()
        i, d, seg, src_take, speed, frames, timeout, cmd = job
        rc, _o, err = run_cmd(cmd, timeout=timeout)
        check_stop()
        if rc != 0 or not seg.is_file() or seg.stat().st_size < 1024:
            msg = err.strip()[:4000] or "输出缺失或文件过小"
            raise RuntimeError(
                f"分段 {i + 1}/{len(items)} 截取失败: {d['item'].path}; "
                f"offset={d['offset']:.3f}s take={src_take:.3f}s "
                f"speed={speed:.3f} frames={frames} canvas={canvas} "
                f"fps={frame_rate:g} rc={rc}\n{msg}")
        return i, seg

    # Use at most two encoders for larger jobs; wait for running encoders before cleaning files.
    segments: dict[int, Path] = {}
    finished = 0
    total_jobs = len(jobs)
    progress("背景片段编码", 0, total_jobs,
             detail=f"已规划 {total_jobs} 段，正在执行 FFmpeg 编码（最多2路）")
    if len(jobs) < 6:
        for job in jobs:
            index, path = encode(job)
            segments[index] = path
            finished += 1
            progress("背景片段编码", finished, total_jobs,
                     detail=f"第 {finished}/{total_jobs} 段编码完成，继续处理下一段")
    else:
        with futures.ThreadPoolExecutor(max_workers=2) as executor:
            pending = {executor.submit(encode, job): job[0] for job in jobs}
            first_error = None
            for future in futures.as_completed(pending):
                try:
                    index, path = future.result()
                    segments[index] = path
                    finished += 1
                    progress("背景片段编码", finished, total_jobs,
                             detail=f"已完成 {finished}/{total_jobs} 段（最多2路并行）")
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    for waiting in pending:
                        waiting.cancel()
            if first_error is not None:
                raise first_error

    for i, d, *_ in jobs:
        if d.get("sticker"):
            d["sticker_applied"] = True
    return [segments[job[0]] for job in jobs]


def build_concat_cmd(items: list[dict], out: Path, canvas=None, fps=None,
                     crf: str = "20", seam_fade: float = 0.0) -> tuple[list[str], Path]:
    """Encode normalized segments now, then return (concat argv, list path).

    This builder has side effects: stage one runs FFmpeg. Stage two only stream-copies
    compatible silent segments to a temporary output. The caller executes that argv
    and owns validation/publication. Use process/thread-isolated work paths."""
    out = Path(out)
    canvas = canvas or config.MONTAGE_CANVAS
    fps = fps or config.MONTAGE_FPS

    segs = _normalize_segments(items, canvas, fps, str(crf), seam_fade=seam_fade)
    # Require every selected segment; missing encodes must not silently publish a shortened body.
    if len(segs) != len(items):
        log(f"[混剪B] 分段截取不完整：期望 {len(items)} 段，"
            f"实际 {len(segs)} 段；已取消拼接")
        return [], Path()

    lst = _write_concat_list(segs)
    tmp = out.with_name(f"{out.stem}.tmp_{os.getpid()}{out.suffix}")
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-map", "0:v:0", "-an", "-c:v", "copy",
        "-map_metadata", "-1", "-movflags", "+faststart", str(tmp),
    ]
    return cmd, lst


def concat_clips(items: list[dict], out: Path, canvas=None, fps=None,
                 crf: str = CRF_DEFAULT, seam_fade: float = 0.0) -> dict:
    """Normalize, concatenate, validate, and atomically publish; return a result dict.

    Clean segment/list files on every path. Convert ordinary failures to ok=False."""
    out = Path(out)
    t0 = time.time()
    res: dict = {"ok": False, "out": str(out), "segments": 0, "duration": 0.0,
                 "seconds": 0.0, "error": ""}
    lst = Path()
    tmp = out.with_name(f"{out.stem}.tmp_{os.getpid()}{out.suffix}")
    try:
        check_stop()
        usable = [d for d in items
                  if d.get("item") is not None and d.get("take", 0.0) > 0]
        res["segments"] = len(usable)
        if not usable:
            res["error"] = "没有可拼接的片段（所有槽位都没取到素材，shortfall>0）"
            return res

        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            res["error"] = f"输出目录创建失败: {e}"
            return res

        cmd, lst = build_concat_cmd(usable, out, canvas, fps, str(crf),
                                    seam_fade=seam_fade)
        if not cmd or not lst.is_file():
            res["error"] = "分段截取不完整，已取消拼接（详见分段日志）"
            return res

        expect = sum(float(d["take"]) for d in usable)
        timeout = min(ENC_TIMEOUT_MAX,
                      max(ENC_TIMEOUT_MIN, int(expect * ENC_TIMEOUT_PER_SEC)))
        log(f"[混剪B] 拼接 {len(usable)} 段 -> 目标 {expect:.2f}s -> {out}")
        rc, _o, err = run_cmd(cmd, timeout=timeout)
        check_stop()
        if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            msg = err.strip().splitlines()[-1] if err.strip() else "无输出"
            res["error"] = f"ffmpeg 失败 rc={rc}: {msg}"
            log(f"  [拼接失败] {res['error']}")
            return res

        # Validate concat duration with per-segment rounding/muxing allowance before publication.
        dur = _probe_duration(tmp)
        fr = float(fps or config.MONTAGE_FPS)
        tolerance = 0.15 + len(usable) / max(fr, 1.0)
        if dur <= 0 or abs(dur - expect) > tolerance:
            res["error"] = (
                f"拼接时长异常：目标 {expect:.2f}s，"
                f"实际 {dur:.2f}s，允许偏差 {tolerance:.2f}s"
            )
            log(f"  [拼接失败] {res['error']}")
            return res

        check_stop()
        os.replace(str(tmp), str(out))
        res.update(ok=True, duration=dur, seconds=round(time.time() - t0, 2))
        log(f"  [已产出] {out.name}  {dur:.2f}s  用时 {time.time() - t0:.1f}s")
        return res
    except Exception as e:  # noqa: BLE001 - convert ordinary encode failures into task results.
        res["error"] = f"{type(e).__name__}: {e}"
        log(f"  [异常] 拼接失败: {res['error']}")
        return res
    finally:
        for p in (tmp, lst):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        shutil.rmtree(_seg_dir("seg"), ignore_errors=True)


def _probe_duration(path: Path) -> float:
    """Return output duration in seconds, or zero when unavailable."""
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        timeout=60)
    if rc != 0:
        return 0.0
    try:
        return float(out.strip())
    except ValueError:
        return 0.0
