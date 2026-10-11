#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify.py —— 切片流水线独立质检脚本（位于 tools/，属 pycut 的 QA 工具）

设计原则：不信任 slicer 自己的产物清单作为"真值"，一切标定都从源视频重新推导。
所有检查失败都以清晰 FAIL 输出，不以异常堆栈崩溃退出。

用法：
    python3 tools/verify.py
    python3 tools/verify.py --video "财源滚滚来__.mp4" --samples 5
    python3 tools/verify.py --manifest ~/Desktop/切片_清单/切片清单_xxx.csv
"""

from __future__ import annotations

import os
import sys

# 让本脚本作为独立文件运行时也能 import pycut 包（取项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pycut.config import (  # noqa: E402
    FFMPEG, FFPROBE, SRC_DIR, OUT_FACE, OUT_NOFACE, OUT_MANIFEST as MANIFEST_DIR,
)

import argparse
import codecs
import csv
import io
import json
import random
import re
import subprocess
import tempfile
import unicodedata

# 判定阈值
MAE_PASS = 10.0          # 帧精度 MAE 阈值（静止帧实测 ~1-2，运动帧可达 ~8）
DUR_TOL = 0.15           # 时长允许误差（秒）
MIN_CLIP_BYTES = 1024    # 小于 1KB 视为坏文件
COARSE_SEEK_BACK = 2.0   # 输入侧粗 seek 回退秒数（双 seek）
COMPARE_WIDTH = 320      # MAE 比对时的统一宽度
MIN_CLIP_DUR = 2.0       # 切片最短时长(秒)，低于此值视为不合格（满足"大于两秒"）
MAX_CLIP_DUR = 15.0      # 用户确认的镜头切片上限

# 清单列名候选（不同版本 slicer 可能有细微差异）
COL_VIDEO = ["video", "video_name", "source", "source_video", "src", "文件名", "视频"]
COL_INDEX = ["seg_index", "index", "idx", "seg", "id", "片段序号", "序号"]
COL_START = ["start", "start_s", "start_sec", "begin", "起点", "开始"]
COL_END = ["end", "end_s", "end_sec", "stop", "终点", "结束"]
COL_DECIDE = ["decided_by", "decision_by", "decision", "策略", "判定方式"]

DECIDE_STRICT = "haar_strict"
DECIDE_CLEAN = "haar_clean"
DECIDE_FALLBACK = "haar_fallback"
DECIDE_MIN_RATIO = 0.85  # haar_strict + haar_clean 占比下限


# ---------------------------------------------------------------- 小工具

class Check:
    """单条检查结果。"""

    def __init__(self, name: str, ok: bool, detail: str = "", fatal: bool = False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fatal = fatal


def _err(msg: str):
    print(f"[错误] {msg}", file=sys.stderr)


def pick_col(fieldnames, candidates):
    """按候选列表挑选实际存在的列名（大小写不敏感）。"""
    if not fieldnames:
        return None
    lower = {f.strip().lower(): f for f in fieldnames}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def run(cmd, timeout=120):
    """执行命令，返回 (rc, stdout, stderr)；异常不抛出。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -1, "", str(e)


def probe(path):
    """ffprobe 取流信息。失败返回 None。"""
    rc, out, _ = run([
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=index,codec_type,codec_name,width,height",
        "-of", "json", path,
    ])
    if rc != 0:
        return None
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        return None


def stream_info(info, codec_type):
    if not info or "streams" not in info:
        return None
    for s in info["streams"]:
        if s.get("codec_type") == codec_type:
            return s
    return None


# ---------------------------------------------------------------- 自动发现

def find_source_videos():
    """列出源目录下的视频文件（递归一层）。"""
    if not os.path.isdir(SRC_DIR):
        return []
    out = []
    for name in sorted(os.listdir(SRC_DIR)):
        if name.startswith("."):
            continue
        p = os.path.join(SRC_DIR, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in (
            ".mp4", ".mov", ".mkv", ".m4v", ".avi", ".flv", ".ts"
        ):
            out.append(p)
    return out


def has_output(stem):
    clips = list_clips(stem)
    return max((os.path.getmtime(path) for _, path, _ in clips), default=None)


def detect_source_video():
    """挑选"有输出目录"且输出目录最近被修改的源视频。"""
    candidates = []
    for p in find_source_videos():
        stem = os.path.splitext(os.path.basename(p))[0]
        m = has_output(stem)
        if m is not None:
            candidates.append((m, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def detect_manifest():
    """挑选清单目录下最新的清单 CSV（切片清单_*.csv）。"""
    if not os.path.isdir(MANIFEST_DIR):
        return None
    found = []
    for name in os.listdir(MANIFEST_DIR):
        if name.startswith("切片清单_") and name.lower().endswith(".csv"):
            found.append(os.path.join(MANIFEST_DIR, name))
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def resolve_video_arg(spec):
    """把 --video 参数解析成绝对路径。"""
    if not spec:
        return detect_source_video()
    if os.path.sep in spec or (os.path.altsep and os.path.altsep in spec):
        return spec if os.path.isabs(spec) else os.path.abspath(spec)
    if os.path.isfile(spec):
        return os.path.abspath(spec)
    p = os.path.join(SRC_DIR, spec)
    return p


# ---------------------------------------------------------------- 清单读取

def load_manifest(path):
    """读取清单，返回 (rows, fieldnames, encoding_report)。失败时返回 (None, None, reason)。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, None, f"无法读取清单: {e}"

    enc = {}
    enc["bytes"] = len(raw)
    enc["has_bom"] = raw.startswith(codecs.BOM_UTF8)
    head3 = raw[:3].hex(" ").upper() if len(raw) >= 3 else "(文件过短)"
    enc["head_hex"] = head3

    try:
        text = raw.decode("utf-8-sig") if enc["has_bom"] else raw.decode("utf-8")
        enc["decode"] = "utf-8-sig" if enc["has_bom"] else "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("gbk", "replace")
        enc["decode"] = "gbk(兜底)"

    enc["replacement"] = text.count("�")
    mojibake = re.findall(r"(?:Ã[\xa0-¿]|å|æ|Â|ä¸­|å¤|è§)", text[:20000])
    enc["mojibake"] = len(mojibake)
    enc["cjk"] = sum(1 for ch in text[:20000] if "一" <= ch <= "鿿")
    enc["text"] = text

    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        rows.append({(k or "").strip(): (v if v is not None else "") for k, v in r.items()})
    return rows, reader.fieldnames or [], enc


# ---------------------------------------------------------------- 帧提取与比对

def extract_first_frame(src, dst):
    rc, _, err = run([
        FFMPEG, "-y", "-v", "error", "-i", src,
        "-frames:v", "1", "-q:v", "2", dst,
    ], timeout=90)
    return rc == 0 and os.path.isfile(dst), err.strip()


def extract_source_frame(src, t, dst):
    """双 seek（输入侧粗 seek + 输出侧精 seek）抓取源视频 t 秒处的帧。"""
    coarse = max(0.0, t - COARSE_SEEK_BACK)
    fine = t - coarse
    rc, _, err = run([
        FFMPEG, "-y", "-v", "error",
        "-ss", f"{coarse:.3f}", "-i", src,
        "-ss", f"{fine:.6f}",
        "-frames:v", "1", "-q:v", "2", dst,
    ], timeout=180)
    return rc == 0 and os.path.isfile(dst), err.strip()


def frame_mae(a_path, b_path, mask_rows=None):
    """返回 (mae, 备注)。失败返回 (None, 原因)。
    mask_rows=(r1,r2)：归一化行区间，比对时排除该行带（字幕遮盖区）。"""
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        return None, f"缺少依赖: {e}"

    ia = cv2.imread(a_path)
    ib = cv2.imread(b_path)
    if ia is None:
        return None, "切片首帧解码失败"
    if ib is None:
        return None, "源视频参考帧解码失败"

    def norm(img):
        h, w = img.shape[:2]
        nw = COMPARE_WIDTH
        nh = max(1, round(h * nw / w))
        return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)

    na, nb = norm(ia), norm(ib)
    if na.shape != nb.shape and na.shape[:2] == nb.shape[:2][::-1]:
        nb = nb.transpose(1, 0, 2).copy()
    if na.shape != nb.shape:
        h = min(na.shape[0], nb.shape[0])
        w = min(na.shape[1], nb.shape[1])
        na = na[:h, :w]
        nb = nb[:h, :w]

    diff = np.abs(na.astype(np.float32) - nb.astype(np.float32))
    note_ext = ""
    if mask_rows:
        h = diff.shape[0]
        r1 = max(0, min(h, int(round(mask_rows[0] * h))))
        r2 = max(r1, min(h, int(round(mask_rows[1] * h))))
        if r2 > r1:
            keep = np.ones(h, dtype=bool)
            keep[r1:r2] = False
            diff = diff[keep]
            note_ext = " (已排除遮盖区)"
    mae = float(diff.mean())
    return mae, f"{na.shape[1]}x{na.shape[0]} 比对窗口{note_ext}"


# ---------------------------------------------------------------- 抽样

def spread_sample(items, n):
    """在列表上做跨越全程的均匀抽样（含首尾），非只取前 n 个。"""
    m = len(items)
    if not items or n <= 0:
        return []
    if m <= n:
        return list(items)
    if n == 1:
        return [random.choice(items)]
    return [items[round(i * (m - 1) / (n - 1))] for i in range(n)]


def list_clips(stem):
    """返回 [(seg_index, abs_path, bucket)]。"""
    clips = []
    for bucket, base in (("有人脸", OUT_FACE), ("无人脸", OUT_NOFACE)):
        d = base
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            m = re.match(r"^" + re.escape(stem) + r"_([1-9][0-9]*)\.mp4$", name, re.IGNORECASE)
            if m:
                clips.append((int(m.group(1)), full, bucket))
    clips.sort(key=lambda x: x[0])
    return clips


def index_clips(clips):
    """seg_index -> path（重复索引只保留第一个）。"""
    out = {}
    for seg, path, bucket in clips:
        out.setdefault(seg, (path, bucket))
    return out


# ---------------------------------------------------------------- 主检查

def main():
    global SRC_DIR, OUT_FACE, OUT_NOFACE, MANIFEST_DIR
    ap = argparse.ArgumentParser(
        prog="verify.py",
        description="视频切片流水线独立质检（不信任 slicer 清单，全部从源视频重新推导）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 tools/verify.py\n"
            "  python3 tools/verify.py --video 财源滚滚来__.mp4 --samples 5\n"
            "  python3 tools/verify.py --manifest ~/Desktop/切片_清单/切片清单_1.csv\n"
        ),
    )
    ap.add_argument("--video", "-v", default=None,
                    help="源视频文件名或完整路径；省略则自动选取最近产出过切片的源视频")
    ap.add_argument("--manifest", "-m", default=None,
                    help="清单 CSV 路径；省略则自动选取最新的 切片清单_*.csv")
    ap.add_argument("--samples", "-n", type=int, default=3,
                    help="抽验切片数量（默认 3，会跨越全片均匀取样）")
    ap.add_argument("--source-dir", default=SRC_DIR, help="源视频根目录")
    ap.add_argument("--out-dir", default=None, help="切片输出根目录（与 slice --out-dir 一致）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    args = ap.parse_args()

    SRC_DIR = args.source_dir
    if args.out_dir:
        OUT_FACE = os.path.join(args.out_dir, "有人脸")
        OUT_NOFACE = os.path.join(args.out_dir, "无人脸")
    random.seed(args.seed if args.seed is not None else 20240921)

    checks = []

    env_ok = True
    for exe in (FFMPEG, FFPROBE):
        exists = os.path.isfile(exe) and os.access(exe, os.X_OK)
        if not exists:
            env_ok = False
            _err(f"找不到可执行文件: {exe}")
    if not env_ok:
        print(f"{'环境自检':<26} {'FAIL':<6} ffmpeg/ffprobe 缺失")
        return 2

    video = resolve_video_arg(args.video)
    if not video or not os.path.isfile(video):
        checks.append(Check("源视频定位", False, f"未找到源视频: {args.video or '(自动检测)'}", fatal=True))
        report(checks, extra=["源目录不可达或无产出：请确认 slicer 已运行"])
        return 2
    from pathlib import Path
    from pycut.core.output_paths import sibling
    output = sibling(Path(video).parent, "切片")
    if args.out_dir:
        output = Path(args.out_dir).expanduser().resolve()
    elif args.manifest:
        output = Path(args.manifest).expanduser().resolve().parent.parent
    OUT_FACE, OUT_NOFACE = str(output / "有人脸"), str(output / "无人脸")
    MANIFEST_DIR = str(output / "清单")
    stem = os.path.splitext(os.path.basename(video))[0]
    print(f"源视频 : {os.path.basename(video)}")
    print(f"视频基名: {stem}")

    src_info = probe(video)
    if not src_info:
        checks.append(Check("源视频探测", False, "ffprobe 无法读取源视频", fatal=True))
        report(checks)
        return 2
    v = stream_info(src_info, "video")
    has_audio_src = stream_info(src_info, "audio") is not None
    src_w = int(v.get("width") or 0)
    src_h = int(v.get("height") or 0)
    src_dur = float(src_info.get("format", {}).get("duration") or 0.0)
    checks.append(Check("源视频探测", True,
                        f"{src_w}x{src_h} / {v.get('codec_name','?')} / "
                        f"{src_dur:.2f}s / 音轨={'有' if has_audio_src else '无'}"))

    mpath = args.manifest or detect_manifest()
    if not mpath or not os.path.isfile(mpath):
        checks.append(Check("清单文件", False, "未找到 切片清单_*.csv", fatal=True))
        report(checks)
        return 2
    rows, fields, enc = load_manifest(mpath)
    if rows is None:
        checks.append(Check("清单读取", False, str(enc), fatal=True))
        report(checks)
        return 2
    print(f"清单   : {os.path.basename(mpath)}  ({len(rows)} 行)")
    print()

    enc_detail = f"BOM={'有' if enc['has_bom'] else '无'} / 列={len(fields)} / 中文字符={enc['cjk']}"
    enc_ok = enc["has_bom"] and enc["replacement"] == 0 and enc["mojibake"] == 0
    if not enc_ok:
        bits = []
        if not enc["has_bom"]:
            bits.append("缺少 UTF-8 BOM")
        if enc["replacement"]:
            bits.append(f"出现 {enc['replacement']} 个替换符 U+FFFD")
        if enc["mojibake"]:
            bits.append(f"疑似乱码 {enc['mojibake']} 处")
        checks.append(Check("清单编码 utf-8-sig", False, enc_detail + " → " + "；".join(bits)))
    else:
        checks.append(Check("清单编码 utf-8-sig", True, enc_detail))
    print(f"清单列 : {fields}")

    c_video = pick_col(fields, COL_VIDEO)
    c_idx = pick_col(fields, COL_INDEX)
    c_start = pick_col(fields, COL_START)
    c_end = pick_col(fields, COL_END)
    c_dec = pick_col(fields, COL_DECIDE)

    missing_cols = [n for n, c in (("video", c_video), ("seg_index", c_idx),
                                   ("start", c_start), ("end", c_end)) if c is None]
    if missing_cols:
        checks.append(Check("清单必需列", False,
                            f"缺少列 {missing_cols}（现有: {fields}）", fatal=True))
        report(checks)
        return 2

    def to_num(s):
        try:
            return float(str(s).strip())
        except (TypeError, ValueError):
            return None

    def row_of_video(r):
        val = str(r.get(c_video, "")).strip()
        if not val:
            return False
        base = os.path.splitext(os.path.basename(val))[0]
        return base == stem or val == stem

    vrows = []
    for i, r in enumerate(rows):
        if not row_of_video(r):
            continue
        seg = r.get(c_idx)
        st = to_num(r.get(c_start))
        en = to_num(r.get(c_end))
        if seg is None or st is None or en is None:
            continue
        try:
            seg_i = int(float(str(seg).strip()))
        except (TypeError, ValueError):
            continue
        vrows.append({"row": i, "seg": seg_i, "start": st, "end": en,
                      "decided_by": str(r.get(c_dec, "")).strip() if c_dec else ""})

    print(f"清单中属于本视频的行数: {len(vrows)}")
    print()

    by_seg = {x["seg"]: x for x in vrows}

    dir_face = OUT_FACE
    dir_noface = OUT_NOFACE
    dirs_exist = [d for d in (dir_face, dir_noface) if os.path.isdir(d)]
    if not dirs_exist:
        checks.append(Check("输出目录", False,
                            f"两个目录均不存在: 切片_有人脸/{stem}、切片_无人脸/{stem}", fatal=True))
        report(checks)
        return 2
    checks.append(Check("输出目录", True, f"命中 {len(dirs_exist)}/2 个目录"))

    clips = list_clips(stem)
    clips_by_idx = index_clips(clips)

    n_face = sum(1 for _, _, b in clips if b == "有人脸")
    n_noface = sum(1 for _, _, b in clips if b == "无人脸")
    n_total = len(clips)
    count_ok = (n_total == len(vrows))
    detail = f"切片 {n_total}（有人脸 {n_face} / 无人脸 {n_noface}） vs 清单 {len(vrows)}"
    unmatched = sorted(set(clips_by_idx) - set(by_seg))
    missing_files = sorted(set(by_seg) - set(clips_by_idx))
    if unmatched:
        detail += f"；无清单对应: {unmatched[:8]}"
    if missing_files:
        detail += f"；缺文件: {missing_files[:8]}"
    checks.append(Check("数量完整性", count_ok and not unmatched and not missing_files, detail))

    bad_files = []
    tmp_files = []
    for d in (dir_face, dir_noface):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            full = os.path.join(d, name)
            if not os.path.isfile(full):
                continue
            if name.endswith(".covered.json"):
                continue
            if name.startswith(".tmp_") or "tmp_" in name:
                tmp_files.append(name)
            if os.path.getsize(full) < MIN_CLIP_BYTES:
                bad_files.append(f"{name}({os.path.getsize(full)}B)")
    clean_ok = not bad_files and not tmp_files
    clean_detail = "无异常文件"
    if tmp_files:
        clean_detail = f"残留 .tmp_ {len(tmp_files)} 个: {tmp_files[:5]}"
    if bad_files:
        clean_detail += ("；" if clean_detail != "无异常文件" else "") + \
                        f"过短/空文件 {len(bad_files)} 个: {bad_files[:5]}"
    checks.append(Check("无坏文件/残留", clean_ok, clean_detail))

    too_short = []
    too_long = []
    for idx, cpath, _ in clips:
        info = probe(cpath)
        if not info:
            too_short.append(f"{idx:03d}(探测失败)")
            continue
        dur = float(info.get("format", {}).get("duration") or 0.0)
        if dur < MIN_CLIP_DUR - 0.01:
            too_short.append(f"{idx:03d}({dur:.2f}s)")
        if dur > MAX_CLIP_DUR + 0.01:
            too_long.append(f"{idx:03d}({dur:.2f}s)")
    min_ok = not too_short
    min_detail = (f"全部切片 ≥ {MIN_CLIP_DUR:.1f}s（共 {len(clips)} 个）" if min_ok
                 else f"{len(too_short)} 个 < {MIN_CLIP_DUR:.1f}s: "
                      + "，".join(too_short[:12]))
    checks.append(Check(f"最小片段时长≥{MIN_CLIP_DUR:.1f}s", min_ok, min_detail))
    checks.append(Check(f"最大片段时长≤{MAX_CLIP_DUR:.1f}s", not too_long,
                        "全部切片符合上限" if not too_long else "，".join(too_long)))

    sample_keys = spread_sample(sorted(clips_by_idx.keys()), max(1, args.samples))
    print(f"抽验 {len(sample_keys)} 个切片: {[f'{k:03d}' for k in sample_keys]}")
    print()

    tmpdir = tempfile.mkdtemp(prefix="verify_frames_")
    n_frame = n_dur = n_geo = 0
    frame_detail, dur_detail, geo_detail = [], [], []

    for seg in sample_keys:
        cpath, bucket = clips_by_idx[seg]
        meta = by_seg.get(seg)
        tag = f"{seg:03d}"

        if meta is None:
            frame_detail.append(f"{tag}: 清单无此行")
            dur_detail.append(f"{tag}: 清单无此行")
            geo_detail.append(f"{tag}: 清单无此行")
            continue

        info = probe(cpath)
        if not info:
            geo_detail.append(f"{tag}: ffprobe 失败")
            dur_detail.append(f"{tag}: ffprobe 失败")
            frame_detail.append(f"{tag}: ffprobe 失败")
            continue

        cv_ = stream_info(info, "video")
        ca_ = stream_info(info, "audio")
        got_dur = float(info.get("format", {}).get("duration") or 0.0)
        want_dur = meta["end"] - meta["start"]

        dd = got_dur - want_dur
        dur_ok = abs(dd) < DUR_TOL
        n_dur += dur_ok
        dur_detail.append(f"{tag}:{got_dur:.3f}/{want_dur:.3f} Δ={dd:+.3f}s{'✓' if dur_ok else '✗'}")

        gw = int(cv_.get("width") or 0)
        gh = int(cv_.get("height") or 0)
        codec_ok = (cv_.get("codec_name") == "h264")
        geo_ok = (gw == src_w and gh == src_h)
        audio_ok = (ca_ is not None) == has_audio_src
        okg_pass = geo_ok and codec_ok and audio_ok
        n_geo += okg_pass
        geo_detail.append(
            f"{tag}:{gw}x{gh}{'✓' if geo_ok else f'≠{src_w}x{src_h}'}"
            f"/{cv_.get('codec_name','?')}{'✓' if codec_ok else '✗'}"
            f"/音轨与源{'一致✓' if audio_ok else '不一致✗'}")

        t0 = max(0.0, meta["start"] + 0.001)
        f_clip = os.path.join(tmpdir, f"{tag}_clip.jpg")
        f_src = os.path.join(tmpdir, f"{tag}_src.jpg")
        ok1, e1 = extract_first_frame(cpath, f_clip)
        ok2, e2 = extract_source_frame(video, t0, f_src)
        if not (ok1 and ok2):
            frame_detail.append(f"{tag}: 取帧失败 {e1 or e2}")
            continue
        mask_rows = None
        mk = __import__("pathlib").Path(str(cpath) + ".covered.json")
        if mk.exists():
            try:
                md = json.loads(mk.read_text(encoding="utf-8"))
                if md.get("subtitle") and md.get("region"):
                    _, ry, _, rh = md["region"]
                    ch = int(cv_.get("height") or 0)
                    if ch > 0 and rh > 0:
                        mask_rows = (ry / ch, (ry + rh) / ch)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        mae, note = frame_mae(f_clip, f_src, mask_rows)
        if mae is None:
            frame_detail.append(f"{tag}: {note}")
            continue
        mae_ok = mae < MAE_PASS
        n_frame += mae_ok
        frame_detail.append(f"{tag}(@{meta['start']:.2f}s):MAE={mae:.2f}"
                            f"{'✓' if mae_ok else f' ≥{MAE_PASS}'}")
        try:
            os.remove(f_clip)
            os.remove(f_src)
        except OSError:
            pass

    try:
        os.rmdir(tmpdir)
    except OSError:
        pass

    nsamp = max(1, len(sample_keys))
    checks.append(Check("抽验-帧精度 MAE", n_frame == nsamp,
                        f"通过 {n_frame}/{nsamp}｜" + ("，".join(frame_detail) or "无样本")))
    checks.append(Check("抽验-时长一致", n_dur == nsamp,
                        f"通过 {n_dur}/{nsamp}｜" + ("，".join(dur_detail) or "无样本")))
    checks.append(Check("抽验-画质/编码/音轨", n_geo == nsamp,
                        f"通过 {n_geo}/{nsamp}｜" + ("，".join(geo_detail) or "无样本")))

    if c_dec is None:
        checks.append(Check("决策分布", False, f"清单缺少 decided_by 列（现有: {fields}）"))
    else:
        from collections import Counter
        cnt = Counter(x["decided_by"] or "(空)" for x in vrows) if vrows else Counter()
        total = sum(cnt.values()) or 1
        strict = cnt.get(DECIDE_STRICT, 0)
        clean = cnt.get(DECIDE_CLEAN, 0)
        fb = cnt.get(DECIDE_FALLBACK, 0)
        ratio = (strict + clean) / total
        llm_used = any((x["decided_by"] or "").startswith("llm") for x in vrows)
        if llm_used:
            dist_ok = (fb == 0)
            mode_note = f"LLM 复核模式｜fallback={fb}(应为0)"
        else:
            dist_ok = True
            mode_note = f"纯 Haar 模式(--no-llm)｜fallback={fb} 为设计内"
        parts = "，".join(f"{k}={v}({v*100/total:.1f}%)" for k, v in cnt.most_common())
        checks.append(Check("决策分布", dist_ok,
                            f"{mode_note}｜strict+clean={ratio*100:.1f}% 仅供参考｜{parts or '无数据'}"))

    return report(checks)


def report(checks, extra=None):
    """打印结果表并给出总结；返回退出码。"""
    print()
    print("=" * 96)
    print(f"{'检查项':<26}{'结果':<8}详情")
    print("-" * 96)
    for c in checks:
        flag = "PASS" if c.ok else "FAIL"
        pad = max(0, 26 - display_width(c.name))
        print(f"{c.name}{' ' * pad}{flag:<8}{c.detail}")
    print("-" * 96)

    failed = [c for c in checks if not c.ok]
    total = len(checks)
    passed = total - len(failed)

    print(f"通过 {passed}/{total}")
    if extra:
        for e in extra:
            print(f"提示: {e}")
    if failed:
        print()
        print(f"总体判定: ❌ FAIL —— 未通过项: {', '.join(c.name for c in failed)}")
        print("=" * 96)
        return 1
    print("总体判定: ✅ PASS —— 全部检查通过")
    print("=" * 96)
    return 0


def display_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _err("已中断")
        sys.exit(130)
    except Exception as e:  # 兜底：绝不给用户看堆栈
        _err(f"未预期的错误: {type(e).__name__}: {e}")
        sys.exit(3)
