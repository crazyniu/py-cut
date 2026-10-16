"""混剪步骤B：从片段库随机挑**竖屏**片段、不重复、填满目标时长、拼成单条静音视频。

对应需求 docs/MONTAGE.md 步骤B。流程三步：
    1. `scan_library`      递归探测片段库，**只留竖屏**（横屏 1920x1080/3840x2160 一律排除）
    2. `pick_clips`        按步骤A 给出的时间槽随机分配素材，同一素材不重复
    3. `concat_clips`      拼成 720x1276 静音视频（`build_concat_cmd` 内部先跑完阶段1 截取，只返回阶段2 命令）

**为什么不用 concat demuxer 的 inpoint/outpoint**（实测，本机 ffmpeg 9.0.2）：
取 3 段（3.0s + 2.5s + 3.83s，混 720x1280 与 544x960）直接写 concat 列表：
    file 'a.mp4' / inpoint 0.5 / outpoint 3.5 / duration 3.0 …
ffmpeg **rc=0 不报错**，产物时长只有 **3.73s**（期望 9.33s）。
再测「全文件不裁剪、混分辨率直接拼」：期望 15.85s，产物只有 **6.47s**，
日志里是 `Reconfiguring filter graph because video parameters changed to … 544x960`
—— 分辨率一变滤镜图重建，其后的内容被丢弃。
**rc=0 但产物静默短一大截**是最坏的一类错误，所以本模块改成两阶段：
先把每段双 seek 精确截取并统一规格（分辨率/帧率/SAR/pix_fmt 全一致），**再** concat。
实测两阶段结果 9.333s / 720x1276 / 无音轨，与 filter_complex 单次编码一致；
选两阶段是因为每段可单独校验规格与时长。注意阶段2 是**再编码一次**（`_enc_args`
的 libx264 + CRF），**不是** `-c copy`：段与段的分辨率/SAR/像素格式已统一，但拼接
本身仍过一次编码器。

拼接命令一律**列表形式**传参（素材名含中文/空格/括号，实测有 `财源滚滚来__ (1)`），
绝不 shell 拼串；写临时文件 + `os.replace` 原子替换，单个文件失败不外抛。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import time
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG, FFPROBE
from .utils import log, run_cmd

# 素材库可识别的扩展名（与 montage_cmd 的 --probe 保持一致）
VIDEO_EXTS = (".mp4", ".mov", ".m4v")

# 内容指纹取首尾各这么多字节（md5）。只对「文件大小相同」的素材才读盘比，
# 唯一大小的直接跳过 —— 实测 509 个文件里只有 214 个需要读，1.3s 跑完。
DEDUP_CHUNK = 1 << 20

# 片段截取/拼接默认 CRF（libx264 软件编码）
CRF_DEFAULT = "20"

# 中间编码使用与贴纸、画中画和字幕一致的速度档；CRF 保持不变。
X264_PRESET = "veryfast"

# 单文件 ffprobe 超时：读容器头很快，60s 足够，异常文件不拖死整批
PROBE_TIMEOUT = 60

# 编码超时按时长估算（libx264 软编 720x1276 实测约 8x 实时富余）
ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600

# 时长比较容差（秒）：浮点累加误差不该让「刚好够」被判成「不够」
EPS = 1e-6


@dataclass
class ClipItem:
    """片段库素材的探测结果。`duration` 是**素材真实总时长**，不因 max_dur 被截断。"""

    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool


# ---------------------------------------------------------------------------
# 临时路径（config.MONTAGE_TMP 下，文件名带 pid）
# ---------------------------------------------------------------------------
def _tmp_root() -> Path:
    """中间产物根目录，不进桌面；不存在则建。"""
    root = Path(config.MONTAGE_TMP)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"[混剪B] 无法创建临时目录 {root}: {e}")
    return root


def _tmp_path(suffix: str, tag: str) -> Path:
    """带 pid 的中间文件路径（同一批里多次调用不会互相覆盖）。"""
    return _tmp_root() / f"{tag}_{os.getpid()}{suffix}"


def _seg_dir(tag: str) -> Path:
    """分段目录（每批一次）。"""
    return _tmp_root() / f"{tag}_{os.getpid()}"


# ---------------------------------------------------------------------------
# 滤镜链：竖屏素材适配画布（等比 + 补边，绝不拉伸变形）
# ---------------------------------------------------------------------------
def fit_filter(canvas: tuple[int, int] | None = None,
               fps: int | None = None) -> str:
    """生成「适配画布」滤镜链。

    `force_original_aspect_ratio=decrease` 先等比缩到画布内，`pad` 再居中补边。
    实测补边极小：720x1280 -> 718x1276（左右各 1px）、544x960 -> 720x1270（上下各 3px），
    肉眼不可见。这是「不裁内容 + 不变形」的唯一解：直接拉伸会变形，crop 会切内容。
    """
    w, h = canvas or config.MONTAGE_CANVAS
    fr = fps or config.MONTAGE_FPS
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fr},format=yuv420p"
    )


def _enc_args(out: Path, fps: int | None, crf: str) -> list[str]:
    """统一编码/封装参数。

    `-an` 静音（音轨由步骤C 统一叠）；libx264 + yuv420p + faststart；
    `-fps_mode cfr` 而非 `-vsync`（ffmpeg 9.0 已移除 -vsync）；
    `-preset veryfast` 用于中间编码；
    不加 `-threads`（AGENTS.md：scene 检测下反而更慢）。
    """
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


# ---------------------------------------------------------------------------
# 步骤B-1：扫描片段库
# ---------------------------------------------------------------------------
def _probe_one(path: Path) -> ClipItem | None:
    """ffprobe 单文件，**一次调用**同时取视频流尺寸/时长与「有没有音轨」。

    库里 509 个文件，ffprobe 串行要几分钟，能少一次调用就少一次。
    失败返回 None（调用方跳过），不抛异常。
    """
    args = [
        FFPROBE, "-v", "error",
        "-show_entries", "stream=codec_type,width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    rc, out, _err = run_cmd(args, timeout=PROBE_TIMEOUT)
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
    if width <= 0 or height <= 0:
        return None

    # 本机 ffprobe 9.0.2 的 stream.duration 只有「有效数值 / N/A / 空(rc=1)」三种形态，
    # 从不输出 nan/inf（AGENTS.md 实测记录），所以回退 format.duration 后一条
    # ValueError 兜底就够，不加 math.isfinite。
    dur = _f(vs.get("duration"))
    if dur <= 0:
        dur = _f((data.get("format") or {}).get("duration"))
    if dur <= 0:
        return None

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return ClipItem(path=Path(path), duration=dur, width=width, height=height,
                    has_audio=has_audio)


def _collect_files(root: Path) -> list[Path]:
    """递归收集视频文件。

    必须走 `os.walk`（macOS `find -iname "*.mp4"` 会静默丢文件，AGENTS.md 记录过）。
    排序固定 => 配不错位，且同 seed 的选片结果可复现。
    """
    from .scan import list_videos_recursive
    return [p for p in list_videos_recursive(root, max_depth=None, raise_on_error=True)
            if p.suffix.lower() in VIDEO_EXTS]


def _content_sig(path: Path) -> str:
    """文件内容指纹：首尾各 1MB 的 md5（只对大小相同的素材调用）。

    不整文件哈希：整文件 md5 要读几百 MB，而实测重复件是**整份拷贝**
    （大小、首尾内容全一致），首尾 2MB 足够区分，且只花 1.3s。
    """
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
    """完整文件内容摘要；仅读取弱指纹碰撞候选，读取失败不删除素材。"""
    h = hashlib.blake2b(digest_size=32)
    try:
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _drop_duplicates(items: list[ClipItem]) -> tuple[list[ClipItem], int]:
    """去掉**内容完全相同**的重复素材（同一素材在库里存了多份）。

    用户的硬要求是「不重复」，而实测片段库有 57 组同名文件、
    内容逐字节一致（`片段手机录制/灭鼠颗粒素材/` 与 `灭鼠颗粒素材/` 是同一批 50 个文件，
    另有 `_hyroxrapkv-...` 这类散落重复）。按路径去重**发现不了**它们——
    `pick_clips` 拿到的是两个不同 Path，会当成两段不同素材，
    结果成片里同一镜头出现两次。

    先按文件大小分组，只有大小相同的才读内容比指纹（省 IO）。
    返回 (去重后列表, 去掉的条数)。
    """
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
        # 首尾指纹只用于筛选候选：视频可以头尾相同而中间不同，
        # 不能凭采样 MD5 宣称“字节完全一致”。只有弱指纹命中时
        # 才流式读取全文件计算摘要，避免每条视频都做昂贵的全量 IO。
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
                 workers=None, allow_landscape: bool = False) -> list[ClipItem]:
    """递归扫描片段库，默认只返回竖屏且时长合规的素材。

    当 allow_landscape=True 时不执行横竖屏过滤（会由 fit_filter
    按比例缩放并补边），以兑现 CLI 的 --allow-landscape 开关。

    - 竖屏判定：`w / h <= config.MONTAGE_MAX_SIDE_RATIO`（1.40）。
      1920x1080 与 3840x2160 都是 1.78，稳稳排除；720x1280=0.562、544x960=0.567 通过。
    - 时长过滤：只卡下界 `min_dur`；`max_dur` **不在这里截断**——
      超长素材的 `ClipItem.duration` 原样保留真实总时长，取用时才截。
      这样 34s 的长片段不会被当废料丢掉，短片段才有同等机会被选中。
    - 并发探测：`workers` 默认 `max(4, config.MONTAGE_WORKERS)`
      （ffprobe 是 IO 等待型，纯等 IO，多开明显更快；实测 509 个文件 8 路 4.9s、串行 37.2s）。
      **并发结果按输入顺序显式回填索引**，绝不用 `ex.map` 的解包顺序
      （AGENTS.md 记录过会错位导致判定张冠李戴）。
    - **内容去重**：同一素材在库里存了多份时只保留一份（见 `_drop_duplicates`）。
    - 单文件探测失败只跳过，不抛异常。

    实测（/Volumes/Lexar/DevStorage/剪辑/片段，509 个文件）：
      横屏 1920x1080 280 条 + 3840x2160 44 条 = **324 条排除**；
      竖屏 720x1280 182 条 + 544x960 3 条 = 185 条通过；
      其中 57 组是逐字节相同的重复拷贝 -> **最终可用 150 条**（总容量约 13 分钟）。
    """
    root = root if root is not None else config.CLIP_DIRS
    lo = float(config.MONTAGE_CLIP_MIN_DUR if min_dur is None else min_dur)
    hi = float(config.MONTAGE_CLIP_MAX_DUR if max_dur is None else max_dur)
    nw = int(workers) if workers else max(4, int(config.MONTAGE_WORKERS))
    ratio_max = float(config.MONTAGE_MAX_SIDE_RATIO)

    files = _collect_files(root)
    if not files:
        log(f"[混剪B] 片段库里没有视频文件: {root}")
        return []

    log(f"[混剪B] 扫描片段库 {root}")
    log(f"  文件总数 {len(files)}  并发 {nw} 路  "
        f"时长门槛 >={lo:.1f}s（上限 {hi:.1f}s 仅取用时截，不丢素材）")

    t0 = time.time()
    results: list[ClipItem | None] = [None] * len(files)
    with futures.ThreadPoolExecutor(max_workers=nw) as ex:
        futs = {ex.submit(_probe_one, p): i for i, p in enumerate(files)}
        done = 0
        for fut in futures.as_completed(futs):
            i = futs[fut]                      # 显式带回索引，不用解包顺序
            done += 1
            try:
                results[i] = fut.result()
            except Exception as e:             # noqa: BLE001 单文件失败不影响整批
                log(f"  [探测异常] {files[i].name}: {type(e).__name__} {e}")
            if done % 100 == 0 or done == len(files):
                log(f"  进度 {done}/{len(files)}")

    items: list[ClipItem] = []
    n_bad = n_land = n_short = 0
    for it in results:
        if it is None:
            n_bad += 1
        elif not allow_landscape and it.width / it.height > ratio_max:
            n_land += 1                        # 默认排除横屏，显式允许则保留
        elif it.duration < lo:
            n_short += 1
        else:
            items.append(it)

    items, n_dup = _drop_duplicates(items)

    log(f"  可用 {len(items)} 条  |  横屏排除 {n_land}  太短排除 {n_short}  "
        f"重复拷贝排除 {n_dup}  探测失败 {n_bad}  |  "
        f"总容量 {sum(i.duration for i in items) / 60:.1f} 分钟"
        f"  |  耗时 {time.time() - t0:.1f}s")
    return items


# ---------------------------------------------------------------------------
# 步骤B-2：按时间槽挑片段
# ---------------------------------------------------------------------------
def _free_offset(dur: float, spans: list[tuple[float, float]],
                 want: float) -> float | None:
    """在「已占用区间」之外找一段长度 >= want 的空隙起点；找不到返回 None。

    按已占区间排序后顺序扫描，第一段够长的空隙即返回（确定性、可复现）。
    """
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
    """按时间槽挑片段，**同一素材不得重复使用**（用户硬要求）。

    每个返回项::

        {"item": ClipItem | None, "take": float,   # 实际取用时长（<= 槽长且 <= 素材剩余）
         "offset": float,                          # 从素材第几秒开始取
         "slot_index": int,
         "reused": bool,                           # True = 素材池用尽，同素材二次取用
         "short": bool,                            # True = 这个槽没取到素材（留缺口）
         "target_dur": float,                      # 全部槽的目标总时长
         "taken_dur": float,                       # 实际取用总时长
         "shortfall": float}                       # target_dur - taken_dur（>0 即填不满）

    规则：
    - `random.Random(seed)` 保证可复现（不用全局 random）。seed=None 时用系统随机源。
    - 遍历 slots 依次分配，**第一轮只用还没取用过的素材**（用户硬要求），
      并优先挑「剩余 >= 槽长」的，让 `take == 槽长`，拼接后节奏不错位；
      没有正好够的就取剩余最大的那个，允许缺口。
    - 素材本身短于 `min_dur` 时跳过。
    - 素材池全部取用尽后仍有槽位：允许**同一素材被再次取用**（落在不重叠的空隙上），
      并标 `"reused": True` 供调用方知晓/报告。
    - 素材总量不足以填满所有槽：`shortfall > 0`，由调用方决定补黑帧还是缩短。
    """
    if not items or not slots:
        return []

    lo = float(config.MONTAGE_CLIP_MIN_DUR if min_dur is None else min_dur)
    hi = float(config.MONTAGE_CLIP_MAX_DUR if max_dur is None else max_dur)

    rng = random.Random(seed)  # noqa: S311 选片不是密码学用途

    n = len(items)
    remaining = [float(it.duration) for it in items]
    spans: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    used = [False] * n          # 本次调用里是否已经取用过（第一轮只考虑没用过的）

    order = list(range(n))
    rng.shuffle(order)                 # 一次性洗牌 + 游标轮转 => 随机且不漏尾
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
        want = min(slot_len, hi)       # 这个槽最多能取多少（受 max_dur 压顶）

        chosen, take, reused = -1, 0.0, False
        offset = 0.0

        # --- 第一轮：只用**还没取用过**的素材（用户硬要求：不重复）---
        best_idx, best_take, best_pos = -1, 0.0, cursor
        hit_pos = -1
        for k in range(n):
            pos = (cursor + k) % n
            idx = order[pos]
            if used[idx]:
                continue
            rest = remaining[idx]
            if rest < lo - EPS:                # 素材本身太短，取不出 min_dur
                used[idx] = True
                remaining[idx] = 0.0
                continue
            usable = min(slot_len, rest, hi)
            if usable > best_take:
                best_idx, best_take, best_pos = idx, usable, pos
            if usable >= want - EPS:            # 正好够 => take == 槽长
                chosen, take, hit_pos = idx, usable, pos
                break
        if chosen < 0 and best_idx >= 0:        # 没有正好够的，取最大的（允许缺口）
            chosen, take, hit_pos = best_idx, best_take, best_pos

        if chosen >= 0:
            offset = rng.uniform(0.0, max(0.0, remaining[chosen] - take))
        else:
            # --- 第二轮：素材池取用尽，允许复用同一素材（落在未占用的空隙上）---
            fb_idx, fb_off, fb_take = -1, 0.0, 0.0
            for idx in order:
                dur = float(items[idx].duration)
                cand = min(want, dur)
                if not used[idx]:
                    # 未取用过的素材优先（这不算复用）
                    off = _free_offset(dur, spans[idx], cand)
                    if off is not None:
                        chosen, offset, take, reused = idx, off, cand, False
                        break
                    continue
                if cand > fb_take:               # 退而求其次：已用过的素材再切一段
                    off2 = _free_offset(dur, spans[idx], cand)
                    if off2 is not None:
                        fb_idx, fb_off, fb_take = idx, off2, cand
            if chosen < 0 and fb_idx >= 0:
                chosen, offset, take, reused = fb_idx, fb_off, fb_take, True

        if chosen < 0:
            # 一个可用区间都凑不出（素材太短 / 已被占满）——留缺口，
            # shortfall 会体现出来，由调用方决定补黑帧还是缩短。
            out.append({"item": None, "take": 0.0, "offset": 0.0,
                        "slot_index": slot_index, "reused": False, "short": True})
            continue

        remaining[chosen] = max(0.0, remaining[chosen] - take)
        spans[chosen].append((offset, offset + take))
        used[chosen] = True
        out.append({"item": items[chosen], "take": round(take, 3),
                    "offset": round(offset, 3), "slot_index": slot_index,
                    "reused": reused, "short": False})
        # 游标挪到刚用掉这个的下一个，保证一轮下来不会老盯着同一批素材
        if hit_pos >= 0:
            cursor = (hit_pos + 1) % n

    taken = sum(d["take"] for d in out)
    shortfall = max(0.0, target - taken)
    for d in out:
        d["target_dur"] = round(target, 3)
        d["taken_dur"] = round(taken, 3)
        d["shortfall"] = round(shortfall, 3)
    if not out:
        log(f"[混剪B] pick_clips：一个槽都没填上（目标 {target:.2f}s，"
            f"素材全短或全占满）")
    return out


# ---------------------------------------------------------------------------
# 步骤B-3：拼接
# ---------------------------------------------------------------------------
def _write_concat_list(files: list[Path]) -> Path:
    """写 concat demuxer 列表。

    路径取绝对路径并用**单引号**包裹：素材名含中文/空格/括号，单引号内部只需把
    自身 `'` 转义成 `'\\''`（concat 解析器的转义规则与 shell 一致）。
    """
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
             frames: int | None = None, dedup_seed: int | None = None) -> list[str]:
    """单段截取：双 seek（输入侧粗 + 输出侧精）+ 适配画布 + 静音 + libx264。

    `mirror` / `speed` 是可选的画面扰动（由调用方按 seed 决定，保持参数默认值即
    原行为，向后兼容）：

    - `mirror=True` 水平翻转（`hflip`）。用于增加画面差异；
      注意素材里有文字/包装时镜像会露出破绽，调用方应控制概率。
    - `speed>1` 用 `setpts=PTS/speed` 加速。**加速会缩短画面**，所以调用方必须
      把 `take` 按 `speed` 放大（取源素材更久）才能填满同样的时间槽——
      `_normalize_segments` 已按 `speed` 反算，见其实现。
    """
    off = max(0.0, float(offset))
    coarse = max(0.0, off - float(config.COARSE_BACKOFF))
    fine = off - coarse
    chain = fit_filter(canvas, fps)
    if dedup_seed is not None:
        from .dedup import montage_variation
        _params, variation = montage_variation(*(canvas or config.MONTAGE_CANVAS), dedup_seed)
        chain += "," + variation
    # 速度/镜像放在 scale 之前：镜像后缩放与先缩放后镜像等价，但先变速能少处理
    # 一些帧（setpts 改时间轴，越靠前生效越早丢帧）。
    pre = []
    if abs(speed - 1.0) > 1e-3:
        pre.append(f"setpts=PTS/{float(speed):.5f}")
    if mirror:
        pre.append("hflip")
    if pre:
        chain = ",".join(pre + [chain])
    if frames is not None:
        # 累计舍入的帧预算可避免多片拼接后产生每段一帧的时长漂移。
        chain += ",tpad=stop_mode=clone:stop_duration=1"
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{coarse:.3f}", "-i", str(src),   # 输入侧粗 seek（回退 COARSE_BACKOFF）
        "-ss", f"{fine:.3f}",                      # 输出侧精 seek 补回来
        "-t", f"{max(0.0, float(take)):.3f}",
        "-vf", chain,
        *([] if frames is None else ["-frames:v", str(frames)]),
        *_enc_args(out, fps, crf),
    ]


def _normalize_segments(items: list[dict], canvas, fps, crf: str,
                        seam_fade: float = 0.0) -> list[Path]:
    """阶段1：每段按 offset/take 双 seek 精确截取，并统一成一致的流参数。

    统一后的段才能喂给 concat demuxer——这正是本模块不用 inpoint/outpoint 的原因。
    任一分段失败立即停止，并把实际编码错误交给 concat_clips 汇总。
    """
    segdir = _seg_dir("seg")
    try:
        shutil.rmtree(segdir, ignore_errors=True)
        segdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建分段目录 {segdir}: {e}") from e

    segs: list[Path] = []
    planned_seconds = 0.0
    assigned_frames = 0
    frame_rate = float(fps or config.MONTAGE_FPS)
    for i, d in enumerate(items):
        it = d.get("item")
        if it is None or d.get("take", 0.0) <= 0:
            continue
        seg = segdir / f"{i:04d}.mp4"
        speed = float(d.get("speed") or 1.0)
        # 关键：`take` 是**成片里该段占多长**；加速后源素材要取更久，
        # 否则 setpts 压缩后段长会不足，成片总时长就会短于目标。
        src_take = float(d["take"]) * (speed if speed > 1.0 else 1.0)
        planned_seconds += float(d["take"])
        cumulative_frames = round(planned_seconds * frame_rate)
        frames = max(1, cumulative_frames - assigned_frames)
        assigned_frames = cumulative_frames
        timeout = min(ENC_TIMEOUT_MAX,
                      max(ENC_TIMEOUT_MIN, int(src_take * ENC_TIMEOUT_PER_SEC)))
        rc, _o, err = run_cmd(
            _seg_cmd(it.path, d["offset"], src_take, seg, canvas, fps, crf,
                     mirror=bool(d.get("mirror")), speed=speed, frames=frames,
                     **({"dedup_seed": d["dedup_seed"]} if "dedup_seed" in d else {})),
            timeout=timeout)
        if rc != 0 or not seg.is_file() or seg.stat().st_size < 1024:
            msg = err.strip()[:4000] or "输出缺失或文件过小"
            raise RuntimeError(
                f"分段 {i + 1}/{len(items)} 截取失败: {it.path}; "
                f"offset={d['offset']:.3f}s take={src_take:.3f}s "
                f"speed={speed:.3f} frames={frames} canvas={canvas} "
                f"fps={frame_rate:g} rc={rc}\n{msg}")

        # 贴纸叠加：段级叠加（拼接后再加就分不清哪段配哪张了）。
        # items 里带 "sticker" 才叠；明确选择后叠加失败不得静默发布无贴纸成片。
        stk = d.get("sticker")
        if stk:
            from .montage_sticker import apply_sticker_to_file  # noqa: PLC0415
            if apply_sticker_to_file(
                    seg, seg, canvas,
                    crf=crf, fps=fps,
                    scale_ratio=config.MONTAGE_STICKER_SCALE,
                    margin_ratio=config.MONTAGE_STICKER_MARGIN,
                    opacity=config.MONTAGE_STICKER_OPACITY,
                    seed=i * 7919 + 13, chance=1.0,
                    sticker=Path(stk)):
                d["sticker_applied"] = True
            else:
                raise RuntimeError(f"分段 {i + 1} 已指定贴纸但叠加失败: {stk}")
        # 段间亮度淡化在标准化和段级贴纸完成后执行，原片段不重叠也不裁短。
        if seam_fade > 0 and len(items) > 1:
            faded = None
            from .montage_transitions import (build_seam_fade_cmd,
                                               probe_segment_duration)
            try:
                real_duration = probe_segment_duration(seg)
                faded = seg.with_name(f"{seg.stem}_fade.mp4")
                cmd = build_seam_fade_cmd(
                    seg, faded, duration=real_duration, seconds=seam_fade,
                    first=(i == 0), last=(i == len(items) - 1),
                    fps=fps, crf=crf)
                if cmd:
                    frc, _fo, ferr = run_cmd(cmd, timeout=timeout)
                    if frc != 0 or not faded.is_file() or faded.stat().st_size < 1024:
                        raise RuntimeError(
                            ferr.strip().splitlines()[-1] if ferr.strip()
                            else "转场输出缺失")
                    faded_duration = probe_segment_duration(faded)
                    if abs(faded_duration - real_duration) > 0.15 + 1 / max(float(fps or config.MONTAGE_FPS), 1):
                        raise RuntimeError("转场前后片段时长不一致")
                    os.replace(faded, seg)
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"分段 {i + 1} 转场失败: {exc}") from exc
            finally:
                if faded is not None and faded.is_file():
                    faded.unlink()
        segs.append(seg)
    return segs


def build_concat_cmd(items: list[dict], out: Path, canvas=None, fps=None,
                     crf: str = "20", seam_fade: float = 0.0) -> tuple[list[str], Path]:
    """用 concat demuxer 拼接成单条静音视频（**两阶段**，原因见模块 docstring 实测）。

    本函数先在 `config.MONTAGE_TMP/seg_<pid>/` 下执行**阶段1**（每段双 seek 精确截取
    + 统一规格），再返回**阶段2** 的 concat 命令。调用方自己跑 argv。

    - 目标画布默认 `config.MONTAGE_CANVAS = (720, 1276)`（注意高度 1276 不是 1280，
      以步骤A 基准视频为准）。
    - 竖屏素材用 `scale` + `pad` 适配画布：**等比、不裁切内容**，实测补边仅 1~3px；
      **严禁直接拉伸变形**（会明显变形）。
    - **静音**：`-an`，且 concat 列表只含视频流（阶段1 产出的段本就无音轨）。
    - `libx264` + `-pix_fmt yuv420p` + `-movflags +faststart` + `-fps_mode cfr`。
    - **原子替换**：阶段2 产物先写 `<out>.tmp_<pid>.mp4`，由 `concat_clips` 在
      ffmpeg 成功后才 `os.replace` 到 `out`（本函数只负责阶段2 的命令，替换由
      `concat_clips` 执行；阶段1 的截取编码已经在本函数内跑完了）。
    - 返回 `(ffmpeg argv, 实际使用的 concat 列表路径)`；列表在 `config.MONTAGE_TMP`
      下、文件名带 pid，便于调用方打印与清理。

    注意：本函数**有副作用**（会真的跑阶段1 的截取编码）。只想看命令请自行拼装。
    """
    out = Path(out)
    canvas = canvas or config.MONTAGE_CANVAS
    fps = fps or config.MONTAGE_FPS

    segs = _normalize_segments(items, canvas, fps, str(crf), seam_fade=seam_fade)
    # 之前任意失败片段会被静默丢弃，仍把剩下的片段发布为“成功混剪”。
    # 当输入列表已经过过滤后，阶段1必须逐段完整产出；缺一段也拒绝发布。
    if len(segs) != len(items):
        log(f"[混剪B] 分段截取不完整：期望 {len(items)} 段，"
            f"实际 {len(segs)} 段；已取消拼接")
        return [], Path()

    lst = _write_concat_list(segs)
    tmp = out.with_name(f"{out.stem}.tmp_{os.getpid()}{out.suffix}")
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        *_enc_args(tmp, fps, crf),
    ]
    return cmd, lst


def concat_clips(items: list[dict], out: Path, canvas=None, fps=None,
                 crf: str = CRF_DEFAULT, seam_fade: float = 0.0) -> dict:
    """执行拼接并原子替换到 `out`，返回结果 dict（供命令层汇总/写清单）。

    流程：`build_concat_cmd` → ffmpeg → 校验产物 → `os.replace(tmp, out)` → 清理
    分段目录与 concat 列表。任何异常都收敛成 `ok=False`，不外抛。
    """
    out = Path(out)
    t0 = time.time()
    res: dict = {"ok": False, "out": str(out), "segments": 0, "duration": 0.0,
                 "seconds": 0.0, "error": ""}
    lst = Path()
    tmp = out.with_name(f"{out.stem}.tmp_{os.getpid()}{out.suffix}")
    try:
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
        if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            msg = err.strip().splitlines()[-1] if err.strip() else "无输出"
            res["error"] = f"ffmpeg 失败 rc={rc}: {msg}"
            log(f"  [拼接失败] {res['error']}")
            return res

        # 容器能写出文件不代表所有内容都拼进去了：曾实测 rc=0，
        # 但拼接结果比预期短一大截。按每段最多约一帧舍入误差
        # （另加 0.15s 封装余量）校验，不能改掉原有成片。
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

        os.replace(str(tmp), str(out))          # 验证后再原子替换
        res.update(ok=True, duration=dur, seconds=round(time.time() - t0, 2))
        log(f"  [已产出] {out.name}  {dur:.2f}s  用时 {time.time() - t0:.1f}s")
        return res
    except Exception as e:  # noqa: BLE001 单次任务失败不拖垮批处理
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
    """产物时长（秒）；探测失败返回 0.0。"""
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
