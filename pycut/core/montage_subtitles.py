"""Parse reference SRT and build styled ASS with original speech timestamps.

Montage retains reference audio and body duration; random background shots must
not shift or discard subtitle cues. Use fontconfig family discovery for CJK fonts,
manual ASS line wrapping, and FFmpeg-specific path escaping. Style sizes scale
with the canvas. SlotSubtitle inputs remain supported for existing callers."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .ffmpeg import FFMPEG
from .utils import log


# ASS style dimensions scale with the canvas; keep calibrated values unchanged.
SUB_FONT_RATIO = 0.042

SUB_MARGIN_V_RATIO = 0.08

SUB_MARGIN_H_RATIO = 0.05

SUB_OUTLINE_RATIO = 0.0028

SUB_SHADOW_RATIO = 0.0012
# With BorderStyle 3, Outline supplies text-box padding.
SUB_BOX_PAD_RATIO = 0.0016

# A translucent box follows text width and keeps white glyphs readable on varied footage.
SUB_BORDER_STYLE = 3
# ASS colours use AABBGGRR; alpha 00 is opaque and FF transparent.
SUB_BACK_COLOUR = "&H80000000"
SUB_PRIMARY_COLOUR = "&H00FFFFFF"
SUB_OUTLINE_COLOUR = "&H00000000"


SUB_X264_PRESET = "veryfast"
SUB_TMP_SUFFIX = ".subtmp"

# Probe font family availability through fontconfig in priority order.
SUB_FONT_CANDIDATES = (
    "Noto Sans CJK SC",
    "PingFang SC",
    "Hiragino Sans GB",
    "Heiti SC",
    "STHeiti",
    "Songti SC",
    "Arial Unicode MS",
)
# Let fontconfig resolve the generic fallback when no candidate is available.
SUB_FONT_FALLBACK = "sans-serif"

# Accept comma/period fractions and hours longer than two digits.
_RE_TIME = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)

_RE_INDEX = re.compile(r"^\d+$")

# Process-local counter prevents ASS filename collisions across repeated calls.
_ASS_SEQ = [0]


@dataclass
class SubtitleCue:
    """SRT text and timing on the original reference timeline."""

    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SlotSubtitle:
    """Subtitle timing already mapped onto an output timeline."""

    slot_index: int
    cue: SubtitleCue

    out_start: float
    out_duration: float


def _read_text(path: Path) -> str:
    """Try UTF-8 BOM/plain UTF-8/GB18030; replace invalid UTF-8 bytes only as a last resort."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # If all supported decoders fail, replace invalid bytes instead of aborting the batch.
    log(f"[字幕] {Path(path).name} 三种编码都解不出，按 utf-8 忽略非法字节重试")
    return raw.decode("utf-8", "replace")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    """Convert timestamp fields to seconds; right-pad fractional milliseconds."""
    return (int(h) * 3600 + int(m) * 60 + int(s)
            + int(ms.ljust(3, "0")) / 1000.0)


def parse_srt(path) -> list[SubtitleCue]:
    """Return cues in file order, tolerating encoding, multiline text, and missing indices.

    Skip blocks with no valid timing/text or reversed intervals; one bad cue must not
    abort the batch. Join text lines here; ASS wrapping happens later."""
    p = Path(path)
    if not p.is_file():
        log(f"[字幕] SRT 不存在: {p}")
        return []

    text = _read_text(p).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n+", text)

    cues: list[SubtitleCue] = []
    skipped = 0
    seq = 0
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue

        # Find timing anywhere in the block because the sequence-number line is optional.
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


def pick_font() -> str:
    """Select an available CJK font family using fc-list, then use a generic fallback.

    libass can silently substitute fonts without CJK glyphs. Font file paths are not
    a reliable availability check, especially for on-demand macOS fonts."""
    if os.environ.get("PYCUT_FONT_DIR"):
        return "Noto Sans SC"
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

    # Without fontconfig, a family name may still resolve in the rendering backend.
    log(f"[字幕] 无 fc-list，无法探测字体，回退 {SUB_FONT_FALLBACK}")
    return SUB_FONT_FALLBACK


def _char_em(ch: str) -> float:
    """Estimate glyph width in em: CJK/full-width 1.0, other characters 0.55."""
    return 1.0 if ord(ch) > 0x2E80 else 0.55


def _wrap_text(text: str, font_size: float, max_width: float) -> str:
    """Wrap within available width using ASS hard line breaks.

    WrapStyle 2 disables automatic wrapping, so long CJK text must be split here."""
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
    """Remove ASS override braces, neutralize literal backslashes, and trim whitespace."""
    return (text.replace("{", "").replace("}", "")
            .replace("\\", "／").strip())


def _ass_time(sec: float) -> str:
    """Format seconds as ASS H:MM:SS.cc with centisecond rollover."""
    cs = int(round(max(0.0, float(sec)) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _as_timed(items) -> list[tuple[float, float, str]]:
    """Normalize SubtitleCue or SlotSubtitle inputs to (start, duration, text)."""
    out: list[tuple[float, float, str]] = []
    for it in items or []:
        if isinstance(it, SlotSubtitle):
            out.append((float(it.out_start), float(it.out_duration), it.cue.text))
        elif isinstance(it, SubtitleCue):
            out.append((float(it.start), float(it.duration), it.text))
    return out


def build_ass(cues_or_slots, canvas=None, font_size=None, margin_v=None,
              outline=None, shadow=None, font_family=None,
              border_style: int | None = None,
              ass_dir: Path | None = None) -> tuple[str, dict]:
    """Return (temporary ASS path, effective style dict).

    Scale fonts, margins, outline, shadow, and box padding by canvas dimensions.
    PlayRes matches the canvas so ASS units map 1:1 to output pixels. Process/counter
    filenames isolate repeated calls. Optional border_style and ass_dir support style comparisons."""
    style_mode = SUB_BORDER_STYLE if border_style is None else border_style
    if style_mode not in (1, 3):
        raise ValueError("BorderStyle 仅支持 1=描边或 3=半透明背景框")
    items = _as_timed(cues_or_slots)
    W, H = (int(canvas[0]), int(canvas[1])) if canvas else config.MONTAGE_CANVAS
    play_res_y = H
    res_scale = H / float(play_res_y)

    size = int(round((font_size or H * SUB_FONT_RATIO) * res_scale))
    m_v = int(round(margin_v if margin_v is not None else H * SUB_MARGIN_V_RATIO))
    m_h = int(round(W * SUB_MARGIN_H_RATIO))
    ol = int(round(outline if outline is not None
                   else H * (SUB_BOX_PAD_RATIO if style_mode == 3
                             else SUB_OUTLINE_RATIO)))
    sh = int(round(shadow if shadow is not None else H * SUB_SHADOW_RATIO))
    family = font_family or pick_font()

    usable = max(1, W - 2 * m_h)
    max_chars = max(1, int(usable // size))
    if style_mode == 3:
        # Subtract box padding before wrapping to keep the full subtitle inside safe margins.
        usable -= 2 * ol

    header = [
        "[Script Info]",
        "; 由 pycut/core/montage_subtitles.py 生成（混剪字幕烧录）",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 2",              # ASS hard line breaks only; wrapping is computed here.
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        # Alignment 2 is bottom-centred; MarginV measures from the bottom edge.
        (f"Style: pycut,{family},{size},{SUB_PRIMARY_COLOUR},{SUB_PRIMARY_COLOUR},"
         f"{SUB_OUTLINE_COLOUR},{SUB_BACK_COLOUR},0,0,0,0,100,100,0,0,"
         f"{style_mode},{ol},{sh},2,{m_h},{m_h},{m_v},1"),
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

    out_dir = Path(ass_dir) if ass_dir is not None else Path(config.MONTAGE_TMP)
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
        "border_style": style_mode,
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
        f" / 底框={bool(style_mode == 3)} 边距L{m_h} V{m_v}"
        f" / 每行最多 {max_chars} 字（可用宽 {usable}px）")
    return str(path), style


def _escape_filter_path(path) -> str:
    """Escape a path for a single-quoted FFmpeg subtitles filename value.

    Handle backslashes first, then colons and quotes for the two parser layers.
    Leave commas, brackets, semicolons, equals signs, spaces, and Unicode literal
    inside the quoted value; extra escaping can split the graph."""
    s = str(path)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'\\'")
    return s


def subtitle_tmp_path(out: Path) -> Path:
    """Return the same temporary destination used by build_subtitle_cmd.

    The caller executes the command, validates output, and performs os.replace."""
    out = Path(out)
    return out.with_name(f"{out.stem}.{os.getpid()}{SUB_TMP_SUFFIX}{out.suffix}")


def subtitle_filter(ass_path) -> str:
    """Build the subtitle filter with an optional configured font directory."""
    result = f"subtitles=filename='{_escape_filter_path(ass_path)}'"
    fonts = os.environ.get("PYCUT_FONT_DIR")
    if fonts:
        result += f":fontsdir='{_escape_filter_path(fonts)}'"
    return result


def build_subtitle_cmd(video: Path, ass_path: Path, out: Path,
                       crf: str = "20", fps: int | None = None) -> list[str]:
    """Return argv to burn ASS into a silent temporary video; do not execute it.

    Escape the filter path, map video explicitly, and use libx264/yuv420p/faststart.
    The caller validates and publishes; missing ASS/video returns an empty argv list."""
    video, ass_path, out = Path(video), Path(ass_path), Path(out)
    if not ass_path.is_file():
        log(f"[字幕] ASS 不存在，跳过烧录: {ass_path}")
        return []
    if not video.is_file():
        log(f"[字幕] 待烧录视频不存在，跳过: {video}")
        return []

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = subtitle_tmp_path(out)

    vf = subtitle_filter(ass_path)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-vf", vf,
        "-map", "0:v:0",         # Map video explicitly even though the intermediate stays silent.
        "-an",
        "-c:v", "libx264", "-preset", SUB_X264_PRESET,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]
    if fps:
        cmd += ["-r", str(int(fps)), "-fps_mode", "cfr"]
    cmd += ["-movflags", "+faststart", str(tmp)]

    log(f"[字幕] 烧录命令已生成 -> {tmp.name}")
    return cmd
