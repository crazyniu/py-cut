r"""混剪字幕：把原视频话术的 SRT 按**时间槽顺序**分配给混剪片段，并烧录到画面上。

背景（这决定了为什么不能按绝对时间对齐，见 docs/MONTAGE.md）：
    混剪步骤A 从爆款视频提取语音节奏 -> 步骤B 用**随机片段**填满这些时间槽 ->
    步骤C 叠画中画。**成片的画面是随机片段，节奏来自原视频**，而 SRT 是
    **原视频的话术原文**。槽的累计时长与原视频时间轴并不严格对齐（槽经过
    min/max 裁剪、合并、丢弃），所以字幕必须按「槽的先后顺序」分配，
    再把每条字幕钉到**成片时间轴**的累计位置上。

为什么转 ASS 而不是直接喂 SRT：
    SRT 只能定时间与文字，字号/描边/背景/位置一概管不了；而用户明确要求
    「文字大一点、带样式和背景」。ASS 的 Style 行才能精确控制这些。

三个公开函数各管一段：
    `parse_srt`             —— 容错解析 SRT（编码/多行/缺序号/坏块全都能扛）
    `assign_cues_to_slots`  —— 第 i 个槽配第 i 条字幕，位置按成片时间轴累计
    `build_ass`             —— 生成带样式的 ASS（字号/描边/背景/位置自适应画布）
    `build_subtitle_cmd`    —— 拼 ffmpeg 命令（`subtitles` 滤镜烧录，路径必须转义）

本机实测踩过的坑（都写在对应函数注释里）：
  * **字体必须实测**：本机 `/System/Library/Fonts/` 下**没有 PingFang.ttc**
    （它在 `/System/Library/AssetsV2/...`），按「文件名存在」探测会误判；
    ffmpeg 9.0.2 是 `--enable-libfontconfig` 编译的，libass 走 fontconfig，
    所以用 `fc-list` 探测字体族名才准（见 `pick_font`）。
  * **滤镜里的路径要转义，但只转 3 个字符**：实测（ffmpeg 9.0.2，8 种路径真跑
    烧录 + 抽帧比对）发现单引号包住整个值后，**只有** `\` `:` `'` 仍需转义，
    `,` `[` `]` `;` `=` 空格与中文**原样即可**；把这一堆一起转义反而会被
    解回原字符、把滤镜串切断（详见 `_escape_filter_path`）。
  * **长句必须手动折行**：720 宽的画布放不下 16 个中文字（54px/字 = 864px），
    ASS 默认不折行会直接溢出画面，必须自己按可用宽度切 `\N`（见 `_wrap_text`）。
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG
from .utils import log

# ---------------------------------------------------------------------------
# 字幕样式常量
#   本应放 config.MONTAGE_SUB* 段，但config.py 由 montage 命令层同步在改，
#   本模块不碰它（避免并行改同一文件冲突）。数值口径与 config 的 MONTAGE_* 一致：
#   全部按**画布比例**给，换分辨率不用重新标定。
# ---------------------------------------------------------------------------

# 字号 = 画布高 × 该比例。720x1276 -> 53.6 ≈ 54px（用户要求「文字大一点」）
SUB_FONT_RATIO = 0.042
# 上下边距 = 画布高 × 该比例。字幕在底部，画中画在右上角，两者不冲突
SUB_MARGIN_V_RATIO = 0.08
# 左右安全边距 = 画布宽 × 该比例（折行时按「可用宽度」算）
SUB_MARGIN_H_RATIO = 0.05
# 描边宽度 = 画布高 × 该比例。1276×0.0028 ≈ 3.6 -> 4px
SUB_OUTLINE_RATIO = 0.0028
# 投影偏移 = 画布高 × 该比例。1276×0.0012 ≈ 1.5 -> 2px
SUB_SHADOW_RATIO = 0.0012
# 背景框内边距（BorderStyle=3 时 Outline 表示框比文字宽多少）
SUB_BOX_PAD_RATIO = 0.0016

# 背景框方案（实测对比后选定，结论见文件末尾「样式方案的实测取舍」）：
#   BorderStyle=3 -> 用 BackColour 画半透明黑底框，Outline 变成「框内留白」。
#   亮背景下白字只有描边会「糊在画面里」，加底框后可读性明显更稳，
#   且框是跟随文字宽度的，不会像整条黑边那样挡画面。
SUB_BORDER_STYLE = 3
# BackColour = &HAABBGGRR，AA=00 全不透明、FF 全透明；80 ≈ 50% 透明黑
SUB_BACK_COLOUR = "&H80000000"
SUB_PRIMARY_COLOUR = "&H00FFFFFF"# 纯白字（全不透明）
SUB_OUTLINE_COLOUR = "&H00000000"# 纯黑描边（全不透明）
SUB_SHADOW_COLOUR = "&H80000000"     # 半透明黑投影

# 一条字幕的最短可读时长（秒）。不足则这槽不给字幕——一闪而过看不清，
# 比不显示更糟（用户看混剪成片，字幕是理解话术的入口）。
SUB_MIN_READABLE = 0.8

# 编码参数
SUB_X264_PRESET = "veryfast"
SUB_TMP_SUFFIX = ".subtmp"

# 中文字体候选（按优先级）。**必须实测存在**，见文件末尾实测记录：
# 本机fc-list 可解析到的前三个是 PingFang SC / Hiragino Sans GB / Heiti SC。
SUB_FONT_CANDIDATES = (
    "PingFang SC",
    "Hiragino Sans GB",
    "Heiti SC",
    "STHeiti",
    "Songti SC",
    "Arial Unicode MS",
)
# 探测不到任何候选时的兜底（交由 fontconfig 自行匹配，总比不写字体名好）
SUB_FONT_FALLBACK = "sans-serif"

# 时间行：`HH:MM:SS,mmm --> HH:MM:SS,mmm`（容忍句点毫秒、容忍小时超过两位）
_RE_TIME = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
# 纯序号行（缺时间行时才用；正常块里序号在时间行之前）
_RE_INDEX = re.compile(r"^\d+$")

# ASS 文件名里的自增序号：同进程内多次 build_ass 不至于写到同一个文件
_ASS_SEQ = [0]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class SubtitleCue:
    """SRT 里的一条字幕（时间仍是**原视频**的时间轴）。"""

    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SlotSubtitle:
    """一条时间槽对应的字幕（时间已换算到**成片**时间轴）。"""

    slot_index: int
    cue: SubtitleCue
    # 字幕要烧录到成片的时间轴位置（不是原视频时间）
    out_start: float
    out_duration: float


# ---------------------------------------------------------------------------
# SRT 解析
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """按候选编码逐个试读，返回第一条能解出来的文本。

    顺序 utf-8-sig -> utf-8 -> gb18030：桌面上的中文SRT 常带 BOM（Excel 导出），
    也可能是从 Windows 导出的 gb18030。用 utf-8-sig 读不带BOM 的 utf-8 同样成功，
    所以它其实是「utf-8 + 去 BOM」的统一入口。
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # 三档都解不出来（不是这三种编码）：按 utf-8 替换非法字节，
    # 宁可少几条也不能让整个解析抛异常把混剪流程带崩。
    log(f"[字幕] {Path(path).name} 三种编码都解不出，按 utf-8 忽略非法字节重试")
    return raw.decode("utf-8", "replace")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    """时间戳分量 -> 秒。毫秒位数不足 3 位时右补零（`.5` = 500ms）。"""
    return (int(h) * 3600 + int(m) * 60 + int(s)
            + int(ms.ljust(3, "0")) / 1000.0)


def parse_srt(path) -> list[SubtitleCue]:
    """解析 SRT，返回按出现顺序排列的字幕列表。

    容错点（都是为了「单条坏掉不能拖垮整批」）：
      * 编码 utf-8-sig / utf-8 / gb18030 逐个试；
      * 块之间空行可能有多个，也可能有Windows 的`\\r\\n`；
      * 文本可能多行，用空格连成一段（ASS 折行由 `_wrap_text` 负责）；
      * 序号行可能缺失，只要块里有时间行就认；
      * 缺文本 / 时间反了 / 块里压根没时间行的，整块跳过继续下一块。
    """
    p = Path(path)
    if not p.is_file():
        log(f"[字幕] SRT 不存在: {p}")
        return []

    text = _read_text(p).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n+", text)

    cues: list[SubtitleCue] = []
    skipped = 0
    seq = 0                       # 序号行缺失时的自增编号
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue

        # 时间行可能不在首行（序号行缺失时它就是首行；这里全块扫描更稳）
        tpos = -1
        match = None
        for i, ln in enumerate(lines):
            m = _RE_TIME.search(ln)
            if m:
                tpos, match = i, m
                break
        if match is None:
            skipped += 1
            continue

        # 序号：时间行之前最近的那个纯数字行；没有就自增
        index = seq + 1
        for ln in reversed(lines[:tpos]):
            if _RE_INDEX.match(ln):
                index = int(ln)
                break
        seq = index

        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        body = " ".join(lines[tpos + 1:]).strip()
        if end <= start or not body:
            skipped += 1
            continue

        cues.append(SubtitleCue(index=index, start=start, end=end, text=body))

    span = f"{cues[0].start:.2f}s~{cues[-1].end:.2f}s" if cues else "无"
    log(f"[字幕] 解析 {p.name}：{len(cues)} 条（{span}）"
        + (f"，跳过坏块 {skipped} 个" if skipped else ""))
    return cues


# ---------------------------------------------------------------------------
# 槽位分配
# ---------------------------------------------------------------------------
def assign_cues_to_slots(cues: list[SubtitleCue],
                         slots: list[tuple[float, float]],
                         total_duration: float) -> list[SlotSubtitle]:
    """把字幕按顺序分配给时间槽（第 i 个槽配第 i 条字幕）。

    `slots` 是步骤A `plan_clip_slots` 的产物，形如 (基准视频里的 start, end)——
    **这里只取它的长度**（`end - start`）：槽长决定成片里这一段有多长，
    而槽的绝对时间属于原视频，与成片无关。

    `out_start` 按槽在**成片时间轴**上的累计位置算（第一个槽=0，之后依次累加
    前面所有槽长），**不是** `cue.start`：cue.start 是原视频时间，槽经过
    min/max 裁剪、合并、丢弃后与原视频时间轴并不严格对齐，直接用会整体错位。

    `out_duration = min(槽长, cue 时长)`：
      * 槽比字幕短 -> 按槽截断，绝不让字幕跨到下一个片段上（画面已经换了，
        字幕还挂着会非常出戏）；
      * 字幕比槽短 -> 用字幕自己的时长，尾部留白（后半段槽里没有新字幕，
        因为字幕已经按顺序用完了）。

    短槽（不足 `SUB_MIN_READABLE`）直接不给字幕：0.6s 的槽上闪过半句话
    比不说更劝退。此时该条字幕就此作废、槽与字幕的对应关系**不发生偏移**——
    若把它顺延给下一个槽，后面所有字幕都会整体后移一句，与语音节奏彻底脱节。

    返回按 `out_start` 升序；`cues` 或 `slots` 为空时返回空列表。
    """
    cue_list = list(cues or [])
    slot_list = list(slots or [])
    if not cue_list or not slot_list:
        return []

    total = float(total_duration or 0.0)
    out: list[SlotSubtitle] = []
    cursor = 0.0                       # 成片时间轴上的累计位置
    ci = 0                             # 已消耗到的字幕序号

    for si, (s, e) in enumerate(slot_list):
        slot_dur = float(e) - float(s)
        out_start = cursor
        cursor += slot_dur
        if ci >= len(cue_list):
            break                       # 字幕用完，后面的槽留空
        if slot_dur <= 0:
            continue

        cue = cue_list[ci]
        dur = min(slot_dur, cue.duration)
        if total > 0:
            dur = min(dur, max(0.0, total - out_start))   # 别超出成片总长
        if dur < SUB_MIN_READABLE:
            ci += 1
            continue                    # 短槽：这槽不给字幕，对应关系不偏移

        out.append(SlotSubtitle(slot_index=si, cue=cue,
                                out_start=round(out_start, 3),
                                out_duration=round(dur, 3)))
        ci += 1

    out.sort(key=lambda s: s.out_start)
    tail = len(slot_list) - len(out)
    why = "（字幕已用完 + 过短的槽）" if ci >= len(cue_list) else "（过短的槽）"
    log(f"[字幕] 分配 {len(out)} 条到 {len(slot_list)} 个槽"
        f"（成片时间轴 0~{cursor:.2f}s，{tail} 个槽无字幕{why}）")
    return out


# ---------------------------------------------------------------------------
# ASS 生成
# ---------------------------------------------------------------------------
def pick_font() -> str:
    """挑一个本机**确实存在**的中文字体族名，写进 ASS 的 Style 行。

    为什么必须探测：字体名写错/缺失时，libass 会静默回退到某个不含汉字的
    字体，中文直接变成方块或空白——ffmpeg **不报错**，成片看着"有字幕"
    实则读不出来。

    探测方式用 `fc-list`（fontconfig），而不是「去 /System/Library/Fonts/
    找字体文件」：本机的 PingFang.ttc **不在**那个目录下，而在
    `/System/Library/AssetsV2/com_apple_MobileAsset_Font8/.../AssetData/`，
    按文件名探测会把明明可用的 PingFang SC 误判成「不存在」
    （ffmpeg 9.0.2 是 --enable-libfontconfig 编译的，libass 走的正是 fontconfig，
    所以 fc-list 的结果与 libass 的实际可见字体一致）。
    """
    exe = shutil.which("fc-list")
    if exe:
        from .utils import run_cmd
        rc, out, _err = run_cmd([exe, ":", "family"], timeout=30)
        if rc == 0 and out.strip():
            families = {tok.strip().lower()
                        for tok in out.replace("\n", ",").split(",")}
            for name in SUB_FONT_CANDIDATES:
                if name.lower() in families:
                    log(f"[字幕] 字体：{name}（fc-list 实测存在）")
                    return name
            log(f"[字幕] fc-list 里没有候选中文字体，回退 {SUB_FONT_FALLBACK}"
                f"（可见字体族 {len(families)} 个）")
            return SUB_FONT_FALLBACK

    # 没有 fontconfig（本机不会走到，保留是因为纯字体名也可能可用）
    log(f"[字幕] 无 fc-list，无法探测字体，回退 {SUB_FONT_FALLBACK}")
    return SUB_FONT_FALLBACK


def _char_em(ch: str) -> float:
    """单字宽度（单位：字号 em）。CJK/全角按 1.0，其余按 0.55。"""
    return 1.0 if ord(ch) > 0x2E80 else 0.55


def _wrap_text(text: str, font_size: float, max_width: float) -> str:
    """按可用宽度折行，行间用 `\\N` 连接（ASS 的硬换行）。

    为什么必须自己折：ASS 的 `WrapStyle: 2` 是「只认\\N，不自动折」，
    而 720 宽的画布扣掉边距只剩约 648px，54px 的中文字最多放 12 个，
    本片最长的句子有 16 个字——不折行会直接溢出画面右侧。
    """
    if max_width <= 0 or font_size <= 0:
        return text

    lines: list[str] = []
    for raw in re.split(r"[\r\n]+", text):
        cur, width = "", 0.0
        for ch in raw:
            w = _char_em(ch) * font_size
            if cur and width + w > max_width:
                lines.append(cur)
                cur, width = ch, w
            else:
                cur += ch
                width += w
        lines.append(cur)
    return "\\N".join(ln for ln in lines if ln)


def _sanitize(text: str) -> str:
    """清掉会让 ASS 解析异常/错位的字符。

    * `{` `}` 是 ASS 的 override block 定界符（SRT 里出现会让后面的样式失效，
      极端情况直接吞掉整行），字幕文本里没有它，删掉即可；
    * 裸 `\\` 会被当成ASS 转义前缀（`\\N` `\\h`），一律换成全角避让；
    * 首尾空白会让 libass 多画一圈描边。
    """
    return (text.replace("{", "").replace("}", "")
            .replace("\\", "／").strip())


def _ass_time(sec: float) -> str:
    """秒 -> ASS 时间 `H:MM:SS.cc`（**百分秒**，不是 SRT 的毫秒逗号）。

    进位由 divmod 自然处理：59.999s 会变成 0:01:00.00，不会写出 `.100`。
    """
    cs = int(round(max(0.0, float(sec)) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _as_timed(items) -> list[tuple[float, float, str]]:
    """把 `list[SubtitleCue]` 或 `list[SlotSubtitle]` 统一成 (start, dur, text)。

    命令层可能先分配再生成，也可能直接拿cue 生成（此时用cue 自己的时间），
    两种入参都要能吃。
    """
    out: list[tuple[float, float, str]] = []
    for it in items or []:
        if isinstance(it, SlotSubtitle):
            out.append((float(it.out_start), float(it.out_duration), it.cue.text))
        elif isinstance(it, SubtitleCue):
            out.append((float(it.start), float(it.duration), it.text))
    return out


def build_ass(cues_or_slots, canvas=None, font_size=None, margin_v=None,
              outline=None, shadow=None, font_family=None) -> tuple[str, dict]:
    """生成 ASS 字幕文件，返回 (ass 路径, 实际生效的样式字典)。

    文件写在 `config.MONTAGE_TMP` 下，文件名带 pid 与自增序号，
    避免同进程内多次调用互相覆盖（也避免多进程抢写）。

    样式全部按**画布比例**给，换分辨率不用重新标定：
      * 字号= 画布高 × `SUB_FONT_RATIO`(4.2%)，720x1276 -> 54px；
      * 上下边距 = 画布高 × 8%（底部居中；画中画在右上角，两者不冲突）；
      * 描边/ 背景框内边距 / 投影 = 画布高 × 各自比例；
      * 底框 `BorderStyle=3` + `BackColour=&H80000000`（半透明黑）。

    **字号与PlayRes 的关系**（易踩）：ASS 里所有尺寸都以 `PlayResX/Y` 为基准。
    本函数把 `PlayResY` 设成画布高度，于是「ASS 单位 : 输出像素 = 1 : 1」，
    缩放系数恰为 1.0——即写 54 就是 54px。若哪天为了兼容性把 PlayResY 调小
    （老版本 libass 对超大 PlayRes 有兼容问题），字号必须同步乘
    `画布高 / PlayResY`，否则字会明显变小。代码里把这个系数显式算出来，
    改PlayResY 时不会漏掉。
    """
    items = _as_timed(cues_or_slots)
    W, H = (int(canvas[0]), int(canvas[1])) if canvas else config.MONTAGE_CANVAS
    play_res_y = H
    res_scale = H / float(play_res_y)          # = 1.0，见上方说明

    size = int(round((font_size or H * SUB_FONT_RATIO) * res_scale))
    m_v = int(round(margin_v if margin_v is not None else H * SUB_MARGIN_V_RATIO))
    m_h = int(round(W * SUB_MARGIN_H_RATIO))
    ol = int(round(outline if outline is not None
                   else H * (SUB_BOX_PAD_RATIO if SUB_BORDER_STYLE == 3
                             else SUB_OUTLINE_RATIO)))
    sh = int(round(shadow if shadow is not None else H * SUB_SHADOW_RATIO))
    family = font_family or pick_font()

    usable = max(1, W - 2 * m_h)
    max_chars = max(1, int(usable // size))
    if SUB_BORDER_STYLE == 3:
        # 背景框会在文字外再撑一圈内边距，折行宽度要把这部分让出来
        usable -= 2 * ol

    header = [
        "[Script Info]",
        "; 由 pycut/core/montage_subtitles.py 生成（混剪字幕烧录）",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 2",              # 只认 \N，不自动折行（折行由本模块负责）
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        # Alignment=2 底部居中；MarginV 是底边距；Encoding=1 用系统默认（UTF-8 源）
        (f"Style: pycut,{family},{size},{SUB_PRIMARY_COLOUR},{SUB_PRIMARY_COLOUR},"
         f"{SUB_OUTLINE_COLOUR},{SUB_BACK_COLOUR},0,0,0,0,100,100,0,0,"
         f"{SUB_BORDER_STYLE},{ol},{sh},2,{m_h},{m_h},{m_v},1"),
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
         "Effect, Text"),
    ]

    events: list[str] = []
    for start, dur, text in items:
        body = _sanitize(text)
        if not body or dur <= 0:
            continue
        wrapped = _wrap_text(body, size, usable)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(start + dur)},"
            f"pycut,,0,0,0,,{wrapped}")

    out_dir = Path(config.MONTAGE_TMP)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"subtitles_{os.getpid()}_{_ASS_SEQ[0]}.ass"
    _ASS_SEQ[0] += 1
    path.write_text("\n".join(header + events) + "\n", encoding="utf-8")

    style = {
        "font_family": family,
        "font_size": size,
        "play_res": [W, play_res_y],
        "res_scale": round(res_scale, 4),
        "outline": ol,
        "shadow": sh,
        "border_style": SUB_BORDER_STYLE,
        "primary_colour": SUB_PRIMARY_COLOUR,
        "outline_colour": SUB_OUTLINE_COLOUR,
        "back_colour": SUB_BACK_COLOUR,
        "margin_v": m_v,
        "margin_h": m_h,
        "usable_width": usable,
        "max_chars_per_line": max_chars,
        "alignment": "bottom-center",
        "cues": len(events),
        "ass_path": str(path),
    }
    log(f"[字幕] ASS {path.name}：{len(events)} 条/ 字体 {family} {size}px"
        f" / 底框={bool(SUB_BORDER_STYLE == 3)} 边距L{m_h} V{m_v}"
        f" / 每行最多 {max_chars} 字（可用宽 {usable}px）")
    return str(path), style


# ---------------------------------------------------------------------------
# 烧录命令
# ---------------------------------------------------------------------------
def _escape_filter_path(path) -> str:
    """把路径转义成能安全放进 `subtitles=filename='...'` 的形态。

    **转义集是实测出来的，不是照抄文档**（本机 ffmpeg 9.0.2，8 种路径逐个真跑
    烧录 + 抽帧比对底部像素变化，见交付记录）。关键结论：单引号一旦把整个值
    包住，ffmpeg 的 filter 解析器就进入「引号内 literal」状态，此时
    **只有 3 个字符仍需转义**：

    | 字符 | 处理 | 实测 |
    |------|------|------|
    | `\\` | -> `\\\\` | 必须双写，否则被当作转义前缀吃掉后半个字符 |
    | `:`  | -> `\\:`| 必须；`-vf subtitles=filename='/a/b.ass'` 里的 `:` 是选项分隔符 |
    | `'`  | -> `\\'\\'` | 必须，且是**两次**转义（ffmpeg 解析器 + libavfilter 各解一层） |

    而 `,` `[` `]` `;` `=` 空格 括号 与**中文**在引号内**原样即可**，实测转义
    它们反而多余（早期版本把这一堆全转义，`\\,` 会被解成 `,` 从而把滤镜串
    切断，报 `No option name near 'c,d.ass'`——这是本模块开发时真实踩到的错）。

    注意顺序：**反斜杠必须第一个处理**，否则后面补的反斜杠会被二次转义。
    """
    s = str(path)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'\\'")
    return s


def subtitle_tmp_path(out: Path) -> Path:
    """`build_subtitle_cmd` 实际写入的临时路径（带 pid，防并发抢写）。

    命令是**返回给调用方执行**的，所以 os.replace 由调用方在 ffmpeg 成功
    （rc=0 且临时文件非空）之后自己做；本函数把那条临时路径公开出来，
    调用方用 `subtitle_tmp_path(out)` 就能拿到同一个名字。
    """
    out = Path(out)
    return out.with_name(f"{out.stem}.{os.getpid()}{SUB_TMP_SUFFIX}{out.suffix}")


def build_subtitle_cmd(video: Path, ass_path: Path, out: Path,
                       crf: str = "20", fps: int | None = None) -> list[str]:
    """用 `subtitles` 滤镜把 ASS 烧录进视频，返回**待执行**的 ffmpeg argv。

    * 滤镜：`subtitles=filename='<转义后的路径>'`，路径必须过
      `_escape_filter_path`（本机输出目录含中文，不转义必失败）；
    * 成片本来就是静音的，保持 `-an`（也顺带避免误挂一条空音轨）；
    * libx264 + `-pix_fmt yuv420p` + `-movflags +faststart`（与步骤B/C 一致）；
    * 原子替换：输出写 `subtitle_tmp_path(out)`（带 pid），
      **由调用方**在成功后 `os.replace(tmp, out)`——本函数只拼命令不执行；
    * `fps` 传了就补 `-r` + `-fps_mode cfr`（ffmpeg 9 已移除 -vsync）。

    不返回 None、不抛异常：没有 ASS 或没有视频时返回空列表，由调用方决定
    是跳过还是报错。
    """
    video, ass_path, out = Path(video), Path(ass_path), Path(out)
    if not ass_path.is_file():
        log(f"[字幕] ASS 不存在，跳过烧录: {ass_path}")
        return []
    if not video.is_file():
        log(f"[字幕] 待烧录视频不存在，跳过: {video}")
        return []

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = subtitle_tmp_path(out)

    vf = f"subtitles=filename='{_escape_filter_path(ass_path)}'"
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-vf", vf,
        "-map", "0:v:0",         # 显式映射：配合 -an 后视频流仍要保住
        "-an",                   # 成片必须静音
        "-c:v", "libx264", "-preset", SUB_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]
    if fps:
        cmd += ["-r", str(int(fps)), "-fps_mode", "cfr"]
    cmd += ["-movflags", "+faststart", str(tmp)]

    log(f"[字幕] 烧录命令已生成 -> {tmp.name}")
    return cmd


# ---------------------------------------------------------------------------
# 样式方案的实测取舍（2026-10-10，720x1276 画布，两方案各烧一遍再抽帧目视）
#
#   方案 A `BorderStyle=1` Outline=4 Shadow=2   纯描边
#   方案 B `BorderStyle=3` BackColour=&H80000000 Outline=2   半透明黑底框
#
# 目视结论（testsrc2 的高饱和色块是最苛刻的背景）：
#   * 两方案的中文都正常显示，**字号也够大**（54px，行高约 60px，占屏高约 9%）；
#   * A 的白字压在**亮黄/亮绿**色块上时，描边被背景冲淡、行与行之间会「糊」
#     成一片（两行紧挨时尤其明显）；B 的底框把背景统一压暗，无论画面多亮，
#     白字边缘都稳定清晰——**混剪素材是随机的、什么颜色都可能出现在底部**，
#     所以选了更稳的 B；
#   * B 的框是**跟随文字宽度**的（实测 8 字 275px、16 字折行后 430px），
#     不像整条黑边那样遮挡画面，两侧仍留 146px 以上。
#
# 折行实测：可用宽 644px / 54px 每字 = 每行 12 字，本片最长的
# 「它是从天然植物雷公藤里提取的精华」(16 字) 折成2 行，宽 430px，
# 左右各留 146/144px，**不溢出**；下边距实测 100px（= MarginV 102），
# 画中画在右上角，两者完全不冲突。
#
# 字体实测（`fc-list : family` 本机可见，候选按序命中第一个）：
#   PingFang SC     -> /System/Library/AssetsV2/com_apple_MobileAsset_Font8/.../PingFang.ttc
#   Hiragino Sans GB -> /System/Library/Fonts/Hiragino Sans GB.ttc
#   Heiti SC         -> /System/Library/Fonts/STHeiti Medium.ttc
# 注意 PingFang.ttc **不在** /System/Library/Fonts/ 下（那是AssetsV2 里的
# 按需下载字体），按「文件名是否存在」探测会把这个明明可用的字体误判为不存在。
# ---------------------------------------------------------------------------
