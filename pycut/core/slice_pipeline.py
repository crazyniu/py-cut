"""切片流水线编排：场景分段 -> 人脸分流 -> 字幕擦除 = 最终切片。

这是「切片」功能的完整实现，被 commands/slice_cmd 调用。
混剪（montage）、去重（dedup）、补跑擦除（cover）作为独立命令，各自复用
本模块的 cut_clip / Manifest 等基础能力（见 commands/ 目录）。
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
from concurrent import futures
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

from .. import config
from .faces import FaceStat, analyze_segment, decide_face
from .ffmpeg import FFMPEG, VideoInfo
from .runtime import STOP
from .scene import build_segments, detect_scenes
from .subtitle import process_clip, verified_clip
from .progress import progress
from .utils import fmt_bytes, fmt_dur, log, run_cmd


# ---------------------------------------------------------------------------
# 选项（与 argparse 解耦的纯数据，便于命令层构造 / 单测）
# ---------------------------------------------------------------------------

@dataclass
class SliceOptions:
    force: bool = False
    dry_run: bool = False
    no_llm: bool = False
    no_sub: bool = False
    scene_th: float = config.SCENE_TH
    min_seg: float = config.MIN_SEG
    workers_llm: int = config.WORKERS_LLM
    workers_enc: int = config.WORKERS_ENC


# ---------------------------------------------------------------------------
# 清单（CSV utf-8-sig + JSON 双写）
# ---------------------------------------------------------------------------

class Manifest:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.csv_path = out_dir / f"切片清单_{ts}.csv"
        self.json_path = out_dir / f"切片清单_{ts}.json"
        self.rows: list[dict] = []
        self._lock = threading.Lock()
        self._fh = open(self.csv_path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._fh, fieldnames=config.MANIFEST_COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def add(self, row: dict) -> None:
        with self._lock:
            self.rows.append(row)
            self._writer.writerow({k: row.get(k, "") for k in config.MANIFEST_COLUMNS})
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(self.rows, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 结果聚合
# ---------------------------------------------------------------------------

@dataclass
class VideoResult:
    stem: str
    segs: int = 0
    face: int = 0
    noface: int = 0
    failed: int = 0
    bytes_out: int = 0
    detect_s: float = 0.0
    enc_s: float = 0.0
    sub_s: float = 0.0
    sub_covered: int = 0
    sub_fail: int = 0
    skipped: bool = False


def out_dirs_for(stem: str) -> tuple[Path, Path]:
    return config.OUT_FACE, config.OUT_NOFACE


def video_done(stem: str, segments: int, *, require_sub: bool = False) -> bool:
    """只有预期全部切片齐备时才能续跑跳过。

    旧实现只要任一输出目录非空就跳过：一次中途失败后目录里仅有
    001.mp4，也会把整支视频永远当成已完成。这里按检测出的切点校验
    编号、数量、文件大小以及需要时的字幕检测标记。
    """
    if segments <= 0:
        return False
    clips = []
    for directory in out_dirs_for(stem):
        if directory.is_dir():
            clips.extend(p for p in directory.glob("*.mp4")
                         if re.fullmatch(re.escape(stem) + r"_[1-9][0-9]*\.mp4", p.name))
    expected = {f"{stem}_{idx}.mp4" for idx in range(1, segments + 1)}
    if len(clips) != segments or {p.name for p in clips} != expected:
        return False
    for clip in clips:
        try:
            if clip.stat().st_size < 1024:
                return False
        except OSError:
            return False
        if require_sub and not verified_clip(clip):
            return False
    return True


def list_videos(limit: int | None = None) -> list[Path]:
    """列出源目录全部 mp4（递归子目录）。

    必须走文件系统遍历（macOS 的 `find -iname` 会静默丢文件），这里用 os.walk
    （底层仍是 scandir）。素材常按平台分目录（如 抖音/快手），故递归收集。
    """
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(str(config.SRC_DIR)):
        for fn in filenames:
            if fn.lower().endswith(".mp4"):
                files.append(Path(dirpath) / fn)
    files.sort(key=lambda p: (str(p.parent), p.name))
    if limit is not None:
        files = files[:limit]
    return files


# ---------------------------------------------------------------------------
# 切割（双重 seek）
# ---------------------------------------------------------------------------

def cut_clip(info: VideoInfo, start: float, end: float, out_path: Path) -> tuple[bool, int]:
    """双重 seek 精确切割 -> h264_videotoolbox 硬件编码。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    dur = max(0.05, end - start)
    coarse = max(0.0, start - config.COARSE_BACKOFF)
    fine = start - coarse

    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if coarse > 0:
        args += ["-ss", f"{coarse:.3f}"]
    args += ["-i", str(info.path)]
    args += ["-ss", f"{fine:.3f}", "-t", f"{dur:.3f}"]
    args += ["-map", "0:v:0"]
    if info.has_audio:
        args += ["-map", "0:a:0"]
    args += [
        "-c:v", config.SLICE_ENCODER,
        "-b:v", info.bitrate,
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr",       # ffmpeg 9.0 起 -vsync 已移除
        "-movflags", "+faststart",
    ]
    if info.has_audio:
        args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    args += [str(tmp)]

    rc, _out, err = run_cmd(args, timeout=900)
    if rc != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        log(f"[切割失败] {out_path.name}: {err.strip()[:200] or '输出文件不足 1KB'}")
        return False, 0

    size = tmp.stat().st_size
    # 上次的去字幕标记只对旧视频有效，强制重切后绝不能继续沿用：
    # process_clip 会因为 .covered.json 存在而直接跳过新切片。
    marker = out_path.with_name(out_path.name + ".covered.json")
    try:
        marker.unlink(missing_ok=True)
        os.replace(str(tmp), str(out_path))  # 原子落盘
    except OSError as exc:
        log(f"[切割失败] 无法发布切片或清理旧字幕标记: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, 0
    return True, size


# ---------------------------------------------------------------------------
# 单视频处理
# ---------------------------------------------------------------------------

def process_video(llm: Any, info: VideoInfo, opts: SliceOptions,
                  manifest: Manifest) -> VideoResult:
    res = VideoResult(stem=info.stem)

    t0 = time.time()
    progress("镜头检测", 0, 1, detail="扫描视频镜头；此阶段尚无切片总数")
    cuts = detect_scenes(info.path, opts.scene_th)
    progress("镜头检测", 1, 1, success=1, detail="镜头检测完成")
    segs = build_segments(cuts, info.duration, opts.min_seg, config.MAX_SEG)
    res.detect_s = time.time() - t0
    res.segs = len(segs)
    if not segs:
        res.failed = 1
        log(f"[无段落] {info.stem}：无法生成 {opts.min_seg:g}–{config.MAX_SEG:g}s 切片")
        return res

    # 试跑必须打印切点，即使已存在产物也不能被“续跑”逻辑吞掉。
    if not opts.force and not opts.dry_run and video_done(
        info.stem, len(segs),
        require_sub=bool(not opts.no_sub and llm.enabled),
    ):
        res.skipped = True
        progress("切片编码", len(segs), len(segs), skipped=len(segs), detail="完整切片已存在，本次未重切", output=str(config.OUT_FACE.parent))
        if not opts.no_sub:
            progress("字幕擦除", len(segs), len(segs), skipped=len(segs), detail="已有新版字幕检查标记，跳过重复处理", output=str(config.OUT_FACE.parent))
        log(f"[跳过] {info.stem}（{len(segs)} 个切片完整，已处理）")
        return res

    rot_hint = f"，旋转{info.rotation}°" if info.rotation else ""
    log(f"[切片] {info.stem}: {len(segs)} 段（时长 {fmt_dur(info.duration)}, "
        f"{info.width}x{info.height}{rot_hint}）检测 {res.detect_s:.1f}s")

    if opts.dry_run:
        for i, (s, e) in enumerate(segs, 1):
            log(f"    {i:03d}  {s:7.2f} -> {e:7.2f}  ({e - s:.2f}s)")
        return res

    progress("人脸分流", 0, len(segs), detail="抽帧分析人脸；不识别语义")
    face_dir, noface_dir = out_dirs_for(info.stem)
    face_dir.mkdir(parents=True, exist_ok=True)
    noface_dir.mkdir(parents=True, exist_ok=True)

    # --- 逐段人脸分析 ---
    stats: list[FaceStat] = []
    verdicts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="slice_frames_") as _td:
        tmpdir = Path(_td)
        for seg in segs:
            if STOP.flag:
                break
            st = analyze_segment(info.path, seg, tmpdir)
            stats.append(st)
            verdicts.append(decide_face(st))
            progress("人脸分流", len(stats), len(segs), success=len(stats), detail="人脸分析完成的镜头数量")

    # --- ambiguous 段送 LLM 兜底（as_completed + 显式索引配对，避免错位）---
    amb_idx = [i for i, v in enumerate(verdicts) if v == "ambiguous"]
    if amb_idx and llm.enabled and not opts.no_llm:
        progress("人脸复核", 0, len(amb_idx), detail="LLM 复核不确定的人脸判断")
    llm_results: dict[int, dict] = {}
    if amb_idx and llm.enabled and not opts.no_llm:
        def _ask(i: int):
            fr = stats[i].mid_frame if stats[i].mid_frame is not None else None
            if fr is None:
                return i, None
            return i, llm.ask(fr)

        with futures.ThreadPoolExecutor(max_workers=opts.workers_llm) as ex:
            futs = [ex.submit(_ask, i) for i in amb_idx]
            for fut in futures.as_completed(futs):
                try:
                    i, r = fut.result()
                except Exception as e:  # noqa: BLE001
                    log(f"[LLM] 调用异常: {type(e).__name__}: {e}")
                    continue
                if r is not None:
                    llm_results[i] = r
                progress("人脸复核", len(llm_results), len(amb_idx), success=len(llm_results), detail="复核不确定片段；失败会采用 Haar 结果")

    if amb_idx and llm.enabled and not opts.no_llm:
        progress("人脸复核", len(amb_idx), len(amb_idx), success=len(llm_results),
                 failed=len(amb_idx) - len(llm_results), detail=("复核失败的片段已回退到 Haar 判断" if len(llm_results) < len(amb_idx) else "人脸复核完成"))

    # --- 逐段落盘任务 ---
    enc_tasks = []  # (idx, s, e, out_path, verdict, row)
    for i, seg in enumerate(segs):
        if STOP.flag:
            break
        st = stats[i] if i < len(stats) else FaceStat()
        v = verdicts[i] if i < len(verdicts) else "ambiguous"
        if v == "face":
            decided_by = "haar_strict"
        elif v == "noface":
            decided_by = "haar_clean"
        else:
            decided_by = ""
        llm_face = ""
        llm_conf = ""
        llm_reason = ""

        if v == "ambiguous":
            r = llm_results.get(i)
            if r is not None and "face" in r:
                try:
                    is_face = bool(r["face"])
                except (TypeError, ValueError):
                    is_face = st.loose_frames > 0
                v = "face" if is_face else "noface"
                decided_by = "llm"
                llm_face = str(is_face).lower()
                llm_conf = str(r.get("confidence", ""))
                llm_reason = str(r.get("reason", ""))[:200]
            else:
                v = "face" if st.loose_frames > 0 else "noface"
                decided_by = "haar_fallback"
                llm_reason = ("未送 LLM（--no-llm）" if opts.no_llm
                              else "LLM 不可用/失败，按 loose 兜底")

        out_dir = face_dir if v == "face" else noface_dir
        out_path = out_dir / f"{info.stem}_{i + 1}.mp4"

        row = {
            "video": info.stem,
            "seg_index": i + 1,
            "start": f"{seg[0]:.3f}",
            "end": f"{seg[1]:.3f}",
            "duration": f"{seg[1] - seg[0]:.3f}",
            "verdict": v,
            "decided_by": decided_by,
            "haar_strict_frames": st.strict_frames,
            "haar_loose_frames": st.loose_frames,
            "haar_max_ratio": f"{st.max_ratio:.4f}",
            "llm_face": llm_face,
            "llm_confidence": llm_conf,
            "llm_reason": llm_reason,
            "out_path": "",
            "out_bytes": 0,
        }
        enc_tasks.append((i, seg[0], seg[1], out_path, v, row))

    # --- 并发编码 ---
    t_enc = time.time()

    def _do(t):
        _i, s, e, p, _v, row = t
        ok, size = cut_clip(info, s, e, p)
        row["out_path"] = str(p) if ok else ""
        row["out_bytes"] = size if ok else 0
        return ok, row

    progress("切片编码", 0, len(enc_tasks), detail="按镜头生成 2–15 秒切片")
    done_clips: list[Path] = []
    with futures.ThreadPoolExecutor(max_workers=opts.workers_enc) as ex:
        for future in futures.as_completed([ex.submit(_do, task) for task in enc_tasks]):
            ok, row = future.result()
            if ok:
                if row["verdict"] == "face":
                    res.face += 1
                else:
                    res.noface += 1
                res.bytes_out += int(row["out_bytes"])
                done_clips.append(Path(row["out_path"]))
            else:
                res.failed += 1
            manifest.add(row)
            progress("切片编码", len(done_clips) + res.failed, len(enc_tasks), success=len(done_clips), failed=res.failed, detail="切片已编码；字幕擦除在下一阶段执行")

    # Only remove stale segments of this source after the replacement set succeeds.
    if not res.failed and not STOP.flag:
        current = set(done_clips)
        for directory in (face_dir, noface_dir):
            for old in directory.glob("*.mp4"):
                if re.fullmatch(re.escape(info.stem) + r"_[1-9][0-9]*\.mp4", old.name) and old not in current:
                    old.unlink()
                    old.with_name(old.name + ".covered.json").unlink(missing_ok=True)

    # --- 流水线第三步：字幕擦除（--no-sub 跳过）---
    if not opts.no_sub and llm.enabled and done_clips and not STOP.flag:
        t_sub = time.time()
        log(f"[字幕] 检测并擦除 {len(done_clips)} 个切片 ...")

        def _cov(p: Path) -> str:
            if STOP.flag:
                return "skip"
            try:
                return process_clip(llm, p)
            except Exception as e:  # noqa: BLE001 单片段失败不拖垮整批
                log(f"  [字幕异常] {p.name}: {type(e).__name__} {e}")
                return "fail"

        sub_stat: dict[str, int] = {"covered": 0, "nosub": 0, "skip": 0, "fail": 0}
        progress("字幕擦除", 0, len(done_clips), detail="检测 → 像素修复 → 成品复检")
        with futures.ThreadPoolExecutor(max_workers=opts.workers_llm) as ex2:
            for future in futures.as_completed([ex2.submit(_cov, p) for p in done_clips]):
                s = future.result()
                sub_stat[s] = sub_stat.get(s, 0) + 1
                progress("字幕擦除", sum(sub_stat.values()), len(done_clips), success=sub_stat['covered'] + sub_stat['nosub'], failed=sub_stat['fail'], skipped=sub_stat['skip'], detail=f"复检通过 {sub_stat['covered']} · 未检出字幕 {sub_stat['nosub']} · 已处理跳过 {sub_stat['skip']}", output=str(face_dir.parent))
        res.sub_s = time.time() - t_sub
        res.sub_covered = sub_stat["covered"]
        res.sub_fail = sub_stat["fail"]
        log(f"[字幕] 遮盖 {sub_stat['covered']} / 无字幕 {sub_stat['nosub']} / "
            f"失败 {sub_stat['fail']}，{res.sub_s:.1f}s")

    res.enc_s = time.time() - t_enc
    log(f"[完成] {info.stem}: 有人脸 {res.face} / 无人脸 {res.noface} / 失败 {res.failed}, "
        f"{fmt_bytes(res.bytes_out)}，编码 {res.enc_s:.1f}s")
    return res
