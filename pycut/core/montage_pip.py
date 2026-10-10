"""混剪步骤C：把「去重输出」的切片以**画中画**形式叠在步骤B 结果的右上角。

输入是步骤B 的成片（已静音、已统一到基准分辨率），输出是最终混剪成片：
右上角在**指定的时间区间内**才出现画中画，其余时刻画面干净，时长与基准一致。

三个公开函数各管一段：
    `scan_dedup_pool`     —— 递归扫描去重素材池，按最短时长过滤
    `plan_pip_timeline`   —— 在成片时间轴上规划互不重叠的画中画区间
    `build_pip_cmd`       —— 拼并执行 ffmpeg 命令（多条`-i` + 一条 filter_complex）

关键实现要点（都是踩过才知道的）：
  * **画中画必须用 `-ss/-t` 输入侧截取**：每个画中画是独立输入，各自 seek 互不干扰；
    输出侧 seek 会先解全部帧，47 条素材下慢到不可用。
  * **`setpts=PTS-STARTPTS+起点`不可省**：overlay 是拿主流时间戳去队列里配对的
    次流帧，不做时间平移的话，等enable 打开时次流已经播完了（黑屏/错位）。
  * **`enable='between(t,起,止)'` 精确到区间**：这是「同一时刻只有一个画中画」的
    实现方式——不靠多条命令拼，靠同一条filter_complex 里串联多个 overlay。
  * **不得拉伸变形**：画中画用 `scale=宽:-2`，`-2` = 按源宽高比自动取偶数高。
  * **分辨率不得改变**：base 已是基准尺寸，只在尺寸不符时才插 `scale`，
    否则就是 AGENTS.md 里记的「二次重采样，白丢清晰度」。
  * 坐标 `W-w-右边距:上边距` —— overlay 的 `W/H` 是**主输入**（即成片）的宽高，
    `w/h` 是画中画自己的宽高，所以右上角必须写成 `W-w-mx`，不能写死数字。
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG, FFPROBE, probe_video
from .utils import log, run_cmd

# ---------------------------------------------------------------------------
# 时间轴参数（config 里没给画中画的出现时长/间隔，值在此处定）
#   slot = 一次画中画持续多久；gap = 两次之间的空白（避免连续闪烁）
#   两者按 slot + gap 的节奏铺满成片，素材不够就少放几段（见 plan_pip_timeline）
# ---------------------------------------------------------------------------
PIP_SLOT = 2.5             # 单个画中画的出现时长（秒）
PIP_GAP = 0.6# 两次画中画之间的间隔（秒）
PIP_GAP_JITTER = 0.2       # 间隔抖动 ±，避免机械的等距感
PIP_LEAD_IN = 0.5          # 第一个画中画之前的留白
PIP_MIN_SHOT = 0.8         # 短于此就不放（闪一下看不清，也白占一次解码）

# 编码参数
PIP_X264_PRESET = "veryfast"
PIP_CRF_DEFAULT = "20"
ENC_TIMEOUT_PER_SEC = 20
ENC_TIMEOUT_MIN = 240
ENC_TIMEOUT_MAX = 3600

# 临时文件后缀（带 pid，避免同批并发时抢写同一文件）
TMP_SUFFIX = ".piptmp"


@dataclass
class PipItem:
    """一条可用的画中画素材。"""
    path: Path
    duration: float


# ---------------------------------------------------------------------------
# 素材池扫描
# ---------------------------------------------------------------------------
def _iter_mp4(root: Path):
    """递归产出 mp4 路径。

    用 `os.scandir` 而不是 `find -iname`——后者在 macOS 上会静默丢文件
    （AGENTS.md 已记录）。目录不可读时跳过，不抛异常。
    """
    stack = [Path(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(str(cur)) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif (entry.is_file(follow_symlinks=False)
                              and entry.name.lower().endswith(".mp4")):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _probe_duration(path: Path) -> float:
    """取**视频流**时长（秒），读不到返回 0。

    口径与 `core/dedup.py:_video_duration` 一致：只读 `stream.duration`，
    不用 `format=duration`（那是容器里最长一条流的长度）。
    mp4 实测一定有值；实测 matroska/flv 的 `stream.duration` 是 `N/A`，
    故加一档 format 兜底。
    """
    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        timeout=30)
    if rc == 0:
        try:
            v = float(out.strip().splitlines()[0])
            if v > 0:
                return v
        except (IndexError, TypeError, ValueError):
            pass

    rc, out, _err = run_cmd(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        timeout=30)
    if rc == 0:
        try:
            v = float(out.strip().splitlines()[0])
            if v > 0:
                return v
        except (IndexError, TypeError, ValueError):
            pass
    return 0.0


def scan_dedup_pool(root=None, min_dur=None) -> list[PipItem]:
    """递归扫描去重素材池，返回可用切片。

    root   —— 默认 `config.MONTAGE_DEDUP_DIR`
    min_dur —— 默认 `config.MONTAGE_CLIP_MIN_DUR`；短于此的素材不用（会闪）

    探测失败 / 读不出时长的直接跳过，不抛异常。按路径排序保证可复现。
    """
    base = Path(root) if root else Path(config.MONTAGE_DEDUP_DIR)
    lo = float(min_dur) if min_dur else float(config.MONTAGE_CLIP_MIN_DUR)

    items: list[PipItem] = []
    skipped = 0
    for path in sorted(_iter_mp4(base), key=lambda p: str(p)):
        dur = _probe_duration(path)
        if dur < lo:
            skipped += 1
            continue
        items.append(PipItem(path=path, duration=dur))

    log(f"  [画中画素材] {base}  可用 {len(items)} 条 / 跳过(短于 {lo:g}s) {skipped} 条")
    return items


# ---------------------------------------------------------------------------
# 时间轴规划
# ---------------------------------------------------------------------------
def plan_pip_timeline(pool: list[PipItem], total_duration: float,
                      slot=None, seed=None) -> list[dict]:
    """规划画中画在成片时间轴上的出现区间。

    返回每项形如
        {"item": PipItem, "start": float, "duration": float,
         "offset": float, "reached_end": bool}
    `offset` 是从该切片第几秒开始取。

    规则：
      * 同一时刻只有一个画中画（区间之间恒有 `gap` 空隙，绝不重叠）；
      * 同一素材不重复使用；
      * 每段的出现时长 = `slot`（默认 `PIP_SLOT`），素材本身不够长则按实际截短；
      * `offset + duration` 保证不超过素材实际时长（否则输入侧 `-t` 会伸到素材
        末尾之外，overlay 在区间尾部拿到空帧 -> 末帧黑屏）；
      * 素材不够铺满 `total_duration` 时**如实返回较少的项**，并在最后一项标
        `reached_end=True`（不编造、不重复用素材去凑）；时间轴正常排满不算；
      * `random.Random(seed)` 保证同 seed 结果可复现。
    """
    items = [p for p in (pool or []) if p is not None]
    total = float(total_duration)
    if not items or total <= 0:
        return []

    slot_len = float(slot) if slot else PIP_SLOT
    rng = random.Random(seed)

    order = list(items)
    rng.shuffle(order)          # 顺序随机 = 素材选取随机（seed 可复现）

    used: set[int] = set()
    timeline: list[dict] = []
    t = min(PIP_LEAD_IN, total)
    reached_end = False

    while True:
        # 找一条还没用、且够长的素材
        picked = None
        for idx, it in enumerate(order):
            if idx in used or it.duration < PIP_MIN_SHOT:
                continue
            dur = min(slot_len, it.duration)
            if dur < PIP_MIN_SHOT:
                continue
            picked = (idx, it, dur)
            break

        rest = total - t
        if picked is None:
            # 素材用完了 —— 这才是「素材不够填满」，标reached_end
            reached_end = True
            break
        if rest < PIP_MIN_SHOT:
            # 成片剩下的时间放不下一个画中画：正常收尾，不算素材不够
            break

        idx, it, dur = picked
        dur = min(dur, rest)
        # 截取起点：在素材可用范围内随机取，避开首尾（首尾常是转场/黑场）
        offset = rng.uniform(0.0, it.duration - dur) if it.duration > dur else 0.0

        used.add(idx)
        # **先截短再 round**：round 会把2.0687 进位成 2.069，
        # 于是 offset+duration 反超素材实际时长（实测039.mp4 2.0687s 被规划成
        # 2.069s），输入侧 -t 就会伸到素材末尾之外，overlay 在区间尾部拿到空帧
        # -> 末帧黑屏。所以 round 之后再夹一次，保证不越界。
        dur_r = round(dur, 3)
        off_r = round(offset, 3)
        dur_r = min(dur_r, it.duration - off_r)

        timeline.append({
            "item": it,
            "start": round(t, 3),
            "duration": dur_r,
            "offset": off_r,
            "reached_end": False,
        })

        gap = rng.uniform(PIP_GAP - PIP_GAP_JITTER, PIP_GAP + PIP_GAP_JITTER)
        t += dur + gap
        if t >= total - PIP_MIN_SHOT:
            # 时间轴排满了，正常结束（不是素材不够）
            break

    if timeline and reached_end:
        timeline[-1]["reached_end"] = True

    covered = sum(e["duration"] for e in timeline)
    log(f"  [画中画时间轴] {len(timeline)} 段/ 覆盖 {covered:.2f}s "
        f"/ 成片 {total:.2f}s"
        + ("（素材不足以铺满，已提前收尾）" if reached_end else ""))
    return timeline


# ---------------------------------------------------------------------------
# 滤镜链 / 命令行
# ---------------------------------------------------------------------------
def _even(v: float, lo: int = 2) -> int:
    """向下取偶数（h264 要求宽高为偶），并保证不小于 lo。"""
    n = int(v)
    n -= n % 2
    return max(lo, n)


def _ms(v: float) -> str:
    """秒 -> 毫秒字符串，**一律截断、绝不进位**。

    实测踩坑：`round(2.068733, 3)` = 2.069 > 素材真实时长 2.068733，
    写进 `-t` 就把截取区间伸到素材末尾之外，overlay 在区间尾部拿到空帧 ->
    末帧黑屏。所以凡是「取多少」的地方（`-ss` / `-t`）都用截断格式。
    """
    ms = int(v * 1000.0)
    if ms < 0:
        ms = 0
    return f"{ms // 1000}.{ms % 1000:03d}"


def _clamp_entries(timeline: list[dict]) -> list[dict]:
    """把时间轴条目整理成可执行的输入列表，并按素材实际剩余时长截短。

    截短是必须的：素材从 `offset` 起不足 `duration` 时，overlay 到了区间末尾
    还没有对应帧，画面上会出问题。这里以 probe 到的素材时长为准，
    再在命令层用 `-t` 二次保险。
    """
    out: list[dict] = []
    for entry in (timeline or []):
        item = entry.get("item")
        if item is None:
            continue
        offset = max(0.0, float(entry.get("offset") or 0.0))
        want = float(entry.get("duration") or 0.0)
        avail = float(item.duration) - offset
        dur = min(want, avail)
        if dur < PIP_MIN_SHOT:
            continue
        out.append({
            "path": Path(item.path),
            "start": max(0.0, float(entry.get("start") or 0.0)),
            "offset": offset,
            "duration": dur,
        })
    return out


def _tmp_path(out: Path) -> Path:
    """临时输出路径（带 pid，防同批并发抢写）。"""
    return out.with_name(f"{out.stem}.{os.getpid()}{TMP_SUFFIX}{out.suffix}")


def _encode_cmd(base: Path, tmp: Path, fcomplex: str,
                crf: str, fps: int) -> list[str]:
    return [
        FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(base),
        "-filter_complex", fcomplex,
        "-map", "[vout]",
        "-an",                              # 最终成片必须静音
        "-c:v", "libx264", "-preset", PIP_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps), "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(tmp),
    ]


def build_pip_cmd(base: Path, timeline: list[dict], out: Path,
                  canvas=None, scale=None, margin=None, fps=None,
                  crf: str = PIP_CRF_DEFAULT) -> list[str]:
    """把画中画叠到 base 上，输出最终成片，返回**实际执行的** ffmpeg argv。

    `canvas`   —— 目标画布，默认取 base 自身的宽高（必须与 base 一致）
    `scale`    —— 画中画宽度 = 画布宽 × 该比例（默认 config.MONTAGE_PIP_SCALE）
    `margin`   —— 距右/上边缘留白 = 画布宽/高 × 该比例（默认 config.MONTAGE_PIP_MARGIN）
    `fps`      —— 输出帧率（默认 config.MONTAGE_FPS）
    `crf`      —— libx264 质量

    时间轴为空时直接转存 base（`-c copy` 更快）；copy 失败则回退重编码，
    保证输出文件确实存在。全程写临时文件后 `os.replace` 原子替换。
    """
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
    margin_y = _even(H * margin_r, lo=0)

    entries = _clamp_entries(timeline)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(out)

    if not entries:
        log(f"  [画中画] 时间轴为空，直接转存 base: {base.name}")
        try:
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
                   "-i", str(base), "-map", "0:v:0", "-an",
                   "-c:v", "copy", "-movflags", "+faststart", str(tmp)]
            rc, _out, err = run_cmd(cmd, timeout=ENC_TIMEOUT_MAX)
            if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
                log(f"  [画中画] -c copy 转存失败（rc={rc}），回退重编码")
                # 不能让 copy 失败留下的半成品成为回退编码的输出。
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
                        f"{err.strip().splitlines()[-1] if err.strip() else '无输出'}")
            os.replace(str(tmp), str(out))
            return cmd
        finally:
            # 非空时间轴有清理逻辑，空时间轴的 copy/fallback 失败也必须一致。
            if tmp.is_file():
                tmp.unlink()

    # --- 输入：base 是第0 路，每个画中画各自 -ss/-t 输入侧精确截取 ---
    cmd: list[str] = [FFMPEG, "-y", "-loglevel", "error", "-nostdin",
                      "-i", str(base)]
    for e in entries:
        cmd += ["-ss", _ms(e["offset"]), "-t", _ms(e["duration"]),
                "-i", str(e["path"])]

    # --- base 链：只有尺寸与目标不一致时才scale（避免二次重采样）---
    head = "setsar=1"
    if (W, H) != (int(info.width), int(info.height)):
        head = f"scale={W}:{H}:flags=bicubic,setsar=1"

    parts = [f"[0:v]{head}[bg]"]
    cur = "bg"
    # --- 四角阴影：尺寸不变，只让边缘融进主画面 ---
    # 实测直接 overlay 是生硬矩形块。这里在 PiP 自身上加一圈渐变 alpha
    # （边缘与四角最透明、中心不透明）+ 一层向外扩散的黑色投影，
    # 让它看起来「浮」在主画面上而不是「贴」上去。
    # 羽化带取画中画短边的 22%（太窄看不出融合，太宽会吃掉画面内容）。
    fade = max(6, int(pip_w * (config.MONTAGE_PIP_FEATHER or 0.22)))
    sh = max(2, int(pip_w * (config.MONTAGE_PIP_SHADOW or 0.035)))   # 投影扩散
    # 边缘权重取两方向**平均**（不是连乘）：连乘会让「距边 100px、带宽 41px」
    # 处权重只剩 0.41，画中画主体会被压得半透明。平均后中心恒为 1。
    fw = (f"(min(1\\,min(X\\,W-1-X)/{fade})"
          f"+min(1\\,min(Y\\,H-1-Y)/{fade}))/2")
    for i, e in enumerate(entries):
        start = e["start"]
        # enable 的结束点同样截断：宁可区间略短，也不能让 overlay 在素材
        # 已播完的瞬间还开着（那正是黑屏出现的时刻）。
        end = start + min(e["duration"], e["duration"] - 0.001)
        # PiP 本体：四角羽化的 alpha（尺寸不变，只改边缘透明度）。
        # setpts=PTS-STARTPTS+起点**必须保留**：overlay 靠主流时间戳去次流队列
        # 配对，不平移时间轴的话，等 enable 打开时次流早已播完 -> 黑屏/错位。
        parts.append(
            f"[{i + 1}:v]fps={fps},scale={pip_w}:-2,setsar=1,format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='alpha(X,Y)*{fw}',"
            f"setpts=PTS-STARTPTS+{_ms(start)}/TB[pp{i}]"
        )
        # 投影层：同一张 PiP 的纯黑副本，向外扩散 sh 像素并整体压低 alpha
        parts.append(
            f"[pp{i}]split=2[pi{i}][ps{i}];"
            # 纯黑副本 + 扩散（boxblur 产生柔和外扩）
            f"[ps{i}]colorchannelmixer=rr=0:gg=0:bb=0,"
            f"boxblur={sh}:1,format=rgba,"
            f"geq=r='0':g='0':b='0':"
            f"a='{float(config.MONTAGE_PIP_SHADOW_ALPHA or 0.45):.2f}'[sh{i}]"
        )
        # 把「投影」和「本体」预先合成为一路（hstack 不合适，这里用
        # 两个 overlay 顺序作用于同一 enable 区间）：
        # 先叠纯黑扩散副本（阴影），再叠羽化后的本体。只做一次最终 overlay，
        # 避免两个 enable 区间重复计算带来的时序错位。
        parts.append(
            f"[{cur}][sh{i}]overlay=x=W-w-{margin_x}:y={margin_y}:"
            f"enable='between(t,{_ms(start)},{_ms(end)})'"
            f":eof_action=repeat:shortest=0[sv{i}]"
        )
        parts.append(
            f"[sv{i}][pi{i}]overlay=x=W-w-{margin_x}:y={margin_y}"
            f":enable='between(t,{_ms(start)},{_ms(end)})'"
            f":eof_action=repeat:shortest=0[v{i}]"
        )
        cur = f"v{i}"
    parts.append(f"[{cur}]null[vout]")
    fcomplex = ";".join(parts)

    log(f"  [画中画] {len(entries)} 段/ 画中画 {pip_w}x-2"
        f" / 右上角留白 x={margin_x} y={margin_y}")

    cmd += [
        "-filter_complex", fcomplex,
        "-map", "[vout]",
        "-an",                              # 最终成片必须静音
        "-c:v", "libx264", "-preset", PIP_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps), "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(tmp),
    ]

    timeout = min(ENC_TIMEOUT_MAX,
                  max(ENC_TIMEOUT_MIN, int(info.duration * ENC_TIMEOUT_PER_SEC)))
    try:
        rc, _out, err = run_cmd(cmd, timeout=timeout)
        if rc != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            raise RuntimeError(
                f"画中画叠加失败 rc={rc}: "
                f"{err.strip().splitlines()[-1] if err.strip() else '无输出'}")
        os.replace(str(tmp), str(out))
    finally:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass
    return cmd