"""pycut 交互式终端界面（curses TUI）—— 引导式向导。

风格取向：Claude Code 观感——深色底、琥珀色强调、顶部 logo+版本号、底部状态栏、
❯ 选中标记、语法高亮。

交互模型（引导式向导，替代旧的「参数表单」）：
    主菜单列出 5 个功能，选中后进入**分步向导**，每步只问一个决定：

      切片        → 选源目录 → 报视频数 → 选「擦除字幕/去重」开关 → 确认执行
      字幕擦除    → 选目录 → 报视频数 → 确认执行（只有擦除，无去重）
      去重        → 选目录 → 报视频数 → 确认执行（只有去重）
      混剪        → 选片段目录 → 选画中画目录 → 选爆款视频目录 →
                    多选爆款视频 + 每个裂变数量 → 确认执行
      查素材      → 选起点目录 → 输入关键词 → 滚动查看匹配的视频路径

关键机制：
  - **目录选择器**：文件系统层级浏览器，从根目录出发逐级浏览，进入子目录 /
    返回上级 / 直接选当前目录，自动过滤系统级目录与隐藏目录，不要求手敲路径。
  - **递归扫描**：选定目录后用 `core/scan` 递归统计视频数并展示（最多下探 3 层）。
  - **多选**（混剪爆款视频）：空格勾选/取消，数字输入裂变数量。
  - **查素材**：按关键词递归搜索视频文件名，滚动查看结果路径。
  - 最终生成 argv 交给 `cli.main` 执行（与命令行完全同源，无漂移）。

健壮性：非交互环境（管道/无 tty）回退 CLI 帮助；任何异常回退到提示，绝不卡死。
"""
from __future__ import annotations

import curses
import curses.textpad  # noqa: F401  必须显式导入：不会随 import curses 自动加载
import os
import sys
from pathlib import Path

from . import __version__, cli
from .core.scan import (count_videos_recursive, list_dirs, list_videos_flat,
                        search_videos)

# ---------------------------------------------------------------------------
# 配色主题（Claude Code 风格：深底 + 琥珀强调）
# ---------------------------------------------------------------------------
_COLOR_DEFS: dict[str, tuple[str, int, int]] = {
    "accent":    ("amber",     214,            -1),  # 强调 / 选中 / 标题
    "dim":       ("gray",      244,            -1),  # 弱化 / 说明
    "muted":     ("dimgray",   238,            -1),  # 更弱的占位 / 边框
    "text":      ("white",     255,            -1),  # 正文
    "good":      ("green",     114,            -1),  # 成功 / 勾选
    "warn":      ("yellow",    221,            -1),  # 警告
    "bad":       ("red",       203,            -1),  # 错误 / 危险
    "info":      ("cyan",      81,             -1),  # 信息
    "keyword":   ("purple",    141,            -1),  # 命令名 / 关键字
    "value":     ("blue",      117,            -1),  # 参数值
}

_FALLBACK_8 = {
    "accent": curses.COLOR_YELLOW, "dim": curses.COLOR_WHITE,
    "muted": curses.COLOR_WHITE,   "text": curses.COLOR_WHITE,
    "good": curses.COLOR_GREEN,    "warn": curses.COLOR_YELLOW,
    "bad": curses.COLOR_RED,       "info": curses.COLOR_CYAN,
    "keyword": curses.COLOR_MAGENTA, "value": curses.COLOR_BLUE,
}

_LIGHT_BG = False
_HAS_COLORS = False

# A light terminal needs dark, higher-contrast foregrounds rather than the
# bright-on-dark 256-colour palette. The background remains the user's default.
_LIGHT_256 = {
    "accent": 130, "dim": 238, "muted": 242, "text": 232,
    "good": 22, "warn": 130, "bad": 124, "info": 25,
    "keyword": 90, "value": 24,
}
_LIGHT_8 = {
    "accent": curses.COLOR_MAGENTA, "dim": curses.COLOR_BLACK,
    "muted": curses.COLOR_BLACK, "text": curses.COLOR_BLACK,
    "good": curses.COLOR_GREEN, "warn": curses.COLOR_MAGENTA,
    "bad": curses.COLOR_RED, "info": curses.COLOR_BLUE,
    "keyword": curses.COLOR_MAGENTA, "value": curses.COLOR_BLUE,
}


def _init_colors() -> None:
    global _LIGHT_BG, _HAS_COLORS
    _HAS_COLORS = curses.has_colors()
    _LIGHT_BG = _detect_light_bg()
    if not _HAS_COLORS:
        return
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    rich = getattr(curses, "COLORS", 0) >= 256
    for idx, name in enumerate(_COLOR_DEFS, start=1):
        fg = _COLOR_DEFS[name][1] if rich else _FALLBACK_8[name]
        if _LIGHT_BG:
            fg = _LIGHT_256[name] if rich else _LIGHT_8[name]
        try:
            curses.init_pair(idx, fg, -1)
        except curses.error:
            # Some terminals do not support the default background sentinel.
            try:
                curses.init_pair(idx, fg, curses.COLOR_WHITE if _LIGHT_BG
                                 else curses.COLOR_BLACK)
            except curses.error:
                pass


def _detect_light_bg() -> bool:
    """通过终端的 COLORFGBG 约定识别浅色背景；未知时使用默认配色。

    curses.color_content 返回 0..1000 的颜色分量，不能靠读取黑色索引
    判断终端背景，原有 >40000 的判定永远不会成立。
    """
    value = os.environ.get("COLORFGBG", "").split(";")
    try:
        return bool(value) and int(value[-1]) in (7, 15)
    except ValueError:
        return False


def _attr(name: str) -> int:
    if not _HAS_COLORS:
        return curses.A_BOLD if name in ("accent", "good", "bad", "keyword") else 0
    return curses.color_pair(1 + list(_COLOR_DEFS).index(name))


# ---------------------------------------------------------------------------
# 功能菜单文案
# ---------------------------------------------------------------------------
FEATURES = [
    ("slice", "切片",    "把长视频切成多段，可选擦除字幕、去重"),
    ("cover", "字幕擦除", "擦除画面中的字幕"),
    ("dedup", "去重",    "去除重复、相似的视频"),
    ("montage", "混剪",  "组合片段生成新视频（可批量裂变）"),
    ("search", "查素材",  "按关键词搜索视频素材"),
]

FEATURE_HINTS = {
    "slice": "场景分段 → 人脸分流 → 可选字幕擦除 / 去重",
    "cover": "选择视频目录 → 自动检测并擦除字幕",
    "dedup": "选择素材目录 → 运行画面处理及相似度自检",
    "montage": "选素材库 → 选画中画 → 选择多支基准视频",
    "search": "中文 / 英文关键词搜索 → macOS Finder 定位",
}


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------
def _char_width(char: str) -> int:
    import unicodedata  # noqa: PLC0415
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" or ord(char) >= 0x1F300 else 1


def _disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def _clip(s: str, width: int, *, tail: bool = False) -> str:
    """按终端显示列而不是 Python 字符数裁剪，保留宽字符完整性。"""
    if width <= 0:
        return ""
    chars = reversed(s) if tail else iter(s)
    out: list[str] = []
    used = 0
    for char in chars:
        col = _char_width(char)
        if used + col > width:
            break
        out.append(char)
        used += col
    return "".join(reversed(out) if tail else out)


def _ellipsize(s: str, width: int, *, tail: bool = False) -> str:
    """Fit labels and paths by terminal columns, keeping the ellipsis visible."""
    if width <= 0:
        return ""
    if _disp_width(s) <= width:
        return s
    if width == 1:
        return "…"
    body = _clip(s, width - 1, tail=tail)
    return ("…" + body) if tail else (body + "…")


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _safe_add(stdscr, y, x, s, maxw, color="text", attrs=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w - 1:
        return
    s = _clip(str(s), min(maxw, w) - x - 1)
    if not s:
        return
    try:
        stdscr.addstr(y, x, s, _attr(color) | attrs)
    except curses.error:
        pass


def _small_screen(stdscr) -> bool:
    h, w = stdscr.getmaxyx()
    if w >= 60 and h >= 14:
        return False
    stdscr.erase()
    _safe_add(stdscr, 1, 2, "终端窗口太小", w, "warn", curses.A_BOLD)
    _safe_add(stdscr, 3, 2, f"当前 {w}×{h}，至少需要 60×14", w, "text")
    _safe_add(stdscr, 5, 2, "请放大窗口 · Esc/Q 返回", w, "dim")
    stdscr.refresh()
    return True


def _frame(stdscr, title: str, subtitle: str = "") -> None:
    """Every view shares the same identity bar, breadcrumb and context rows."""
    _h, w = stdscr.getmaxyx()
    _safe_add(stdscr, 0, 0, " " * w, w, "text", curses.A_REVERSE)
    _safe_add(stdscr, 0, 2, "◈  PYCUT", w, "accent", curses.A_REVERSE | curses.A_BOLD)
    version = f"v{__version__}"
    _safe_add(stdscr, 0, max(20, w - _disp_width(version) - 3), version,
              w, "dim", curses.A_REVERSE)
    breadcrumb = title.replace(" · ", "  /  ", 1)
    _safe_add(stdscr, 2, 2, _ellipsize(breadcrumb, w - 4), w,
              "keyword", curses.A_BOLD)
    _safe_add(stdscr, 3, 2, _ellipsize(subtitle, w - 4, tail=True), w, "info")
    _safe_add(stdscr, 4, 2, "─" * max(0, w - 4), w, "muted")


def _footer(stdscr, full: str, compact: str) -> None:
    """A stable shortcut row; compact mode never hides Esc/return controls."""
    h, w = stdscr.getmaxyx()
    _safe_add(stdscr, h - 2, 0, " " * w, w, "dim", curses.A_REVERSE)
    _safe_add(stdscr, h - 2, 2,
              _ellipsize(compact if w < 80 else full, w - 4),
              w, "dim", curses.A_REVERSE)


# ---------------------------------------------------------------------------
# 输入框（数字 / 简单文本，带默认值）
# ---------------------------------------------------------------------------
def _prompt_int(stdscr, y, x, w, title, default, lo, hi) -> int | None:
    """可编辑整数输入，首个数字覆盖默认值；越界需改正而非悄悄钳制。"""
    buf = str(default)
    untouched = True
    error = ""
    while True:
        h, screen_w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() == 27:
                return None
            continue
        line_y = min(y, h - 4)
        w = screen_w
        stdscr.move(line_y, 0)
        stdscr.clrtoeol()
        prompt = f"{title} [{lo}~{hi}]："
        _safe_add(stdscr, line_y, x, prompt, w, "text")
        bx = x + _disp_width(prompt) + 1
        _safe_add(stdscr, line_y, bx, _clip(buf, max(0, w - bx - 2), tail=True), w,
                  "value", curses.A_REVERSE)
        _safe_add(stdscr, line_y + 1, x, error or "Enter 确认 · Esc 取消 · Backspace 删除", w,
                  "bad" if error else "dim")
        stdscr.refresh()
        k = stdscr.getch()
        if k in (10, curses.KEY_ENTER, ord("\r")):
            if buf.isdigit() and lo <= int(buf) <= hi:
                return int(buf)
            error = f"请输入 {lo}～{hi} 之间的整数"
        elif k == 27:
            return None
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
            untouched = False
            error = ""
        elif ord("0") <= k <= ord("9"):
            buf = ("" if untouched else buf) + chr(k)
            untouched = False
            error = ""


def _erase_grapheme(s: str) -> str:
    """Backspace removes a user-perceived character, including combining marks."""
    if not s:
        return s
    i = len(s) - 1
    while i > 0 and (_char_width(s[i]) == 0 or 0x1F3FB <= ord(s[i]) <= 0x1F3FF):
        i -= 1
    while i >= 2 and ord(s[i - 1]) == 0x200D:
        i -= 2
        while i > 0 and _char_width(s[i]) == 0:
            i -= 1
    return s[:i]


def _prompt_text(stdscr, y, x, w, title, default="") -> str | None:
    """通过 get_wch 接受中文/日文输入法及 Unicode 粘贴；Esc 取消。"""
    buf = default
    supports_unicode = callable(getattr(stdscr, "get_wch", None))
    get_key = stdscr.get_wch if supports_unicode else stdscr.getch
    while True:
        h, screen_w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            try:
                k = get_key()
            except curses.error:
                continue
            if k in ("\x1b", 27, "q", "Q", ord("q"), ord("Q")):
                return None
            continue
        line_y = min(y, h - 4)
        w = screen_w
        try:
            stdscr.move(line_y, 0)
            stdscr.clrtoeol()
        except curses.error:
            # 窗口也可能恰好在绘制时发生尺寸变化。
            continue
        prompt = f"{title}："
        if x + _disp_width(prompt) + 4 >= w:
            prompt = "关键词："
        _safe_add(stdscr, line_y, x, prompt, w, "text")
        bx = x + _disp_width(prompt) + 1
        _safe_add(stdscr, line_y, bx, _clip(buf, max(0, w - bx - 2), tail=True),
                  w, "value", curses.A_REVERSE)
        hint = ("Enter 搜索 · Esc 取消 · Backspace 删除" if supports_unicode else
                "仅 ASCII 输入 · Enter 搜索 · Esc 取消")
        _safe_add(stdscr, line_y + 1, x, hint, w, "dim")
        stdscr.refresh()
        try:
            k = get_key()
        except curses.error:
            continue
        if k in ("\n", "\r", 10, 13, curses.KEY_ENTER):
            return buf
        if k in ("\x1b", 27):
            return None
        if k in ("\b", "\x7f", curses.KEY_BACKSPACE, 127, 8):
            buf = _erase_grapheme(buf)
        elif isinstance(k, str) and k.isprintable():
            buf += k
        elif isinstance(k, int) and 32 <= k <= 126:
            buf += chr(k)


# ---------------------------------------------------------------------------
# 目录选择器（文件系统层级浏览器）
# ---------------------------------------------------------------------------
def _pick_dir(stdscr, title: str, start: Path | None = None) -> str | None:
    """目录选择器：从根目录出发逐级浏览文件系统，选定任意目录。

    交互：
      ↑/↓ 或 j/k   在「子目录列表」中移动
      Enter        进入选中的子目录；在「..」或「选择当前目录」上则选定
      ←/h/Backspace 返回上一级目录
      Esc          取消（返回 None）
      ~            直接跳到用户主目录；/ 跳到根目录

    列表第一项固定为「📁 选择当前目录」（直接选定当前所在目录），
    第二项固定为「📁 ..」（返回上一级），之后才是当前目录的子目录。
    系统级目录与隐藏目录自动过滤，不再显示。从根目录 / 开始，
    可一路浏览到任意位置，不再依赖预置候选。
    """
    # 起点：默认根目录；若有 start 则从那里开始
    cur = Path(start).expanduser() if start else Path("/")
    if not cur.is_dir():
        cur = Path("/")

    def _entries() -> list[tuple[str, Path]]:
        """返回当前目录下的可选项：(显示名, 目标路径)。"""
        out: list[tuple[str, Path]] = []
        out.append(("📁  选择当前目录", cur))
        if cur != cur.parent:
            out.append(("📁  ..  返回上一级", cur.parent))
        # 只列可见子目录（自动过滤系统级目录 + 隐藏目录）
        for p in list_dirs(cur):
            out.append((f"📂  {p.name}", p))
        return out

    entries = _entries()
    # 从根目录浏览时默认落在第一个子目录，避免误按 Enter 直接选择 /。
    sel = 2 if len(entries) > 2 else 0
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        _frame(stdscr, title, str(cur))

        if sel >= len(entries):
            sel = max(0, len(entries) - 1)
        start_y = 5
        max_rows = max(1, h - 9)
        top = 0
        if sel >= max_rows:
            top = sel - max_rows + 1
        for i in range(top, min(len(entries), top + max_rows)):
            label, _path = entries[i]
            y = start_y + (i - top)
            active = (i == sel)
            mark = "❯" if active else " "
            # 过长目录名：截断，保留末尾
            label = _ellipsize(label, w - 8, tail=True)
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "text"
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, label, w, fg, attr)

        _footer(stdscr,
                " ↑/↓ 移动 · Enter 进入/选定 · ←/h 返回上级 · ~ 主目录 · / 根目录 · Esc 取消 ",
                "↑↓移动  Enter进入  ←返回  ~主目录  /根  Esc取消")
        stdscr.refresh()
        k = stdscr.getch()

        if k in (27,):  # Esc
            return None
        if k in (ord("q"), ord("Q")):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % max(1, len(entries))
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % max(1, len(entries))
        elif k in (curses.KEY_LEFT, 8, 127, curses.KEY_BACKSPACE, ord("h")):
            if cur != cur.parent:
                cur = cur.parent
                entries = _entries()
                sel = 0
        elif k in (ord("~"),):
            cur = Path.home()
            entries = _entries()
            sel = 0
        elif k in (ord("/"),):
            cur = Path("/")
            entries = _entries()
            sel = 0
        elif k in (10, curses.KEY_ENTER, ord("\r"), ord(" ")):
            if not entries:
                continue
            label, target = entries[sel]
            if label.startswith("📁  选择当前目录"):
                return str(cur)
            if label.startswith("📁  .."):
                cur = target
                entries = _entries()
                sel = 0
                continue
            # 进入子目录
            cur = target
            entries = _entries()
            sel = 0


# ---------------------------------------------------------------------------
# 确认页（汇报数量 + 开关 + 执行确认）
# ---------------------------------------------------------------------------
def _confirm(stdscr, lines: list[tuple[str, str]],
             toggles: list[tuple[str, str, bool]] | None = None,
             out_vals: list[bool] | None = None) -> bool:
    """展示一段汇报 + 若干开关，返回是否确认执行。

    lines: [(label, value, color?)] 展示项。
    toggles: [(label, 说明, 初始bool)] 可切换开关（空格切换）。
    out_vals: 传入一个可变 list，确认后回写每个开关的最终值（默认 [初始值]*N，
      用户切换后与之同步）；不传则结果丢弃。
    """
    toggle_vals = [t[2] for t in toggles] if toggles else []
    if out_vals is not None:
        out_vals[:] = toggle_vals
    sel = 0
    n_items = len(toggle_vals) + 1  # last focusable item starts the job
    summary_offset = 0

    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return False
            continue

        # Never hide the action button or the toggles beneath the footer.
        visible_rows = max(1, h - 13 - len(toggle_vals)) if toggles else max(1, h - 9)
        summary_offset = min(summary_offset, max(0, len(lines) - visible_rows))
        has_more = len(lines) > visible_rows
        subtitle = "检查输入与选项"
        if has_more:
            subtitle += (f" · 第 {summary_offset + 1}-"
                         f"{min(len(lines), summary_offset + visible_rows)}/{len(lines)} 行"
                         " · PgUp/PgDn 查看")
        else:
            subtitle += "，按 Enter 开始执行"
        stdscr.erase()
        _frame(stdscr, "处理前确认", subtitle)

        label_width = 16 if w >= 80 else 12
        value_x = 2 + label_width
        y = 5
        for label, value in lines[summary_offset:summary_offset + visible_rows]:
            _safe_add(stdscr, y, 2,
                      _pad(_ellipsize(str(label), label_width - 1), label_width),
                      w, "dim")
            _safe_add(stdscr, y, value_x,
                      _ellipsize(str(value), w - value_x - 2, tail=True),
                      w, "value")
            y += 1

        if toggles:
            y += 1
            _safe_add(stdscr, y, 2, "功能开关（空格切换）", w, "dim")
            y += 1
            for i, (label, desc, _v) in enumerate(toggles):
                active = (sel == i)
                mark = "❯" if active else " "
                state = "● 开" if toggle_vals[i] else "○ 关"
                sc = "good" if toggle_vals[i] else "muted"
                attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
                _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
                _safe_add(stdscr, y, 5, _pad(label, 12), w, "text", attr)
                _safe_add(stdscr, y, 17, state, w, sc, attr)
                _safe_add(stdscr, y, 17 + _disp_width(state) + 1,
                          _ellipsize(desc, w - 26), w, "dim", attr)
                y += 1
            y += 1

        active = sel == len(toggle_vals)
        attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
        _safe_add(stdscr, y, 3, "❯" if active else " ",
                  w, "good" if active else "muted", attr)
        _safe_add(stdscr, y, 5, "▶▶ 确认并开始处理", w, "good", attr)
        _footer(stdscr,
                " ↑/↓ 移动 · 空格 切换 · PgUp/PgDn 查看摘要 · Enter 确认 · Esc 返回 ",
                "↑↓选项  空格切换  PgDn摘要  Enter确认  Esc返回")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return False
        if k in (curses.KEY_PPAGE, 339):
            summary_offset = max(0, summary_offset - visible_rows)
        elif k in (curses.KEY_NPAGE, 338):
            summary_offset = min(max(0, len(lines) - visible_rows),
                                 summary_offset + visible_rows)
        elif k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % n_items
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % n_items
        elif k == ord(" ") and sel < len(toggle_vals):
            toggle_vals[sel] = not toggle_vals[sel]
            if out_vals is not None:
                out_vals[:] = toggle_vals
        elif k in (10, curses.KEY_ENTER, ord("\r")):
            if sel == len(toggle_vals):
                if out_vals is not None:
                    out_vals[:] = toggle_vals
                return True
            # Enter on a switch toggles it; only the action starts the job.
            toggle_vals[sel] = not toggle_vals[sel]
            if out_vals is not None:
                out_vals[:] = toggle_vals


# ---------------------------------------------------------------------------
# 多选列表（混剪爆款视频）
# ---------------------------------------------------------------------------
def _multi_select(stdscr, title: str, files: list[Path]) -> tuple[list[Path], dict[str, int]] | None:
    """多选视频列表 + 每个的裂变数量。返回 (选中列表, {路径: 裂变数})；Esc 返回 None。

    空格勾选/取消；选中项上按数字键 0-9 设置裂变数量（默认 1）；A 全选/取消全选。
    """
    if not files:
        return None
    selected: set[int] = set()
    fission: dict[str, int] = {str(f): 1 for f in files}
    sel = 0
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        total = sum(fission[str(files[i])] for i in selected)
        _frame(stdscr, title, f"{len(files)} 个视频 · 已选 {len(selected)} 个 · 预计输出 {total} 支")

        max_rows = max(1, h - 8)
        top = 0
        if sel >= max_rows:
            top = sel - max_rows + 1
        for i in range(top, min(len(files), top + max_rows)):
            y = 5 + (i - top)
            f = files[i]
            active = (i == sel)
            checked = i in selected
            mark = "❯" if active else " "
            box = "[✓]" if checked else "[ ]"
            bcolor = "good" if checked else "muted"
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            name = f.name
            name = _ellipsize(name, w - 24)
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, box, w, bcolor, attr)
            _safe_add(stdscr, y, 5 + 4, name, w, "text", attr)
            n = fission.get(str(f), 1)
            _safe_add(stdscr, y, w - 12, f"裂变 x{n}", w, "value", attr)

        _footer(stdscr,
                " ↑/↓ 移动 · 空格 勾选 · E 设裂变数(1~99) · A 全选 · Enter 确认 · Esc 返回 ",
                "↑↓移动  空格勾选  E数量  A全选  Enter确认  Esc返回")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27,):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % max(1, len(files))
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % max(1, len(files))
        elif k == ord(" "):
            if sel in selected:
                selected.discard(sel)
            else:
                selected.add(sel)
        elif k in (ord("a"), ord("A")):
            if len(selected) == len(files):
                selected.clear()
            else:
                selected = set(range(len(files)))
        elif k in (ord("e"), ord("E")):
            p = str(files[sel])
            n = _prompt_int(stdscr, h - 4, 2, w, "设置裂变数量",
                            fission[p], 1, 99)
            if n is not None:
                fission[p] = n
        elif ord("1") <= k <= ord("9"):
            fission[str(files[sel])] = k - ord("0")
        elif k in (10, curses.KEY_ENTER, ord("\r")):
            if not selected:
                continue
            chosen = [files[i] for i in sorted(selected)]
            return chosen, {str(f): fission[str(f)] for f in chosen}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _loop(stdscr) -> tuple[str, list[list[str]]] | None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # 少数终端不支持隐藏光标，仍允许使用 TUI。
    stdscr.keypad(True)

    feat_sel = getattr(_loop, "_feat_sel", 0)  # 选中项只初始化一次，循环内保持
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return ("quit", None)
            continue
        stdscr.erase()
        _frame(stdscr, "选择功能", "本地视频工作台  /  ↑↓ 导航，Enter 进入")
        sel = feat_sel
        for i, (key, name, desc) in enumerate(FEATURES):
            y = 5 + i
            active = (i == sel)
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "keyword"
            _safe_add(stdscr, y, 3, "❯" if active else " ", w,
                      "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, f"{i + 1:02d}  ", w, "dim", attr)
            _safe_add(stdscr, y, 9, _pad(name, 10), w, fg, attr)
            _safe_add(stdscr, y, 20, desc, w, "dim", attr)
        if h >= 17:
            key, name, _desc = FEATURES[sel]
            _safe_add(stdscr, 11, 2, "─" * max(0, w - 4), w, "muted")
            _safe_add(stdscr, 12, 3, f"当前选择  /  {name}", w, "accent", curses.A_BOLD)
            _safe_add(stdscr, 13, 3, FEATURE_HINTS[key], w, "text")
            _safe_add(stdscr, 15, 3, "每个向导会先展示处理范围，确认后才执行。", w, "dim")
        _footer(stdscr, " ↑/↓ 移动 · 1-5 快速进入 · Enter 选择 · Q 退出 ",
                "↑↓移动  1-5快捷  Enter选择  Q退出")
        stdscr.refresh()
        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return ("quit", None)
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(FEATURES)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(FEATURES)
        elif k in (10, curses.KEY_ENTER, ord("\r"), ord(" ")) or (
            ord("1") <= k <= ord("5")
        ):
            if ord("1") <= k <= ord("5"):
                sel = k - ord("1")
            _loop._feat_sel = sel
            key = FEATURES[sel][0]
            argv_list = _run_feature_wizard(stdscr, key, w)
            if argv_list == "__BACK__":
                continue
            if argv_list:
                return ("run", argv_list)
        # 每轮结束把当前选中保存下来（下一轮重绘沿用）
        feat_sel = sel
    return ("quit", None)


def _run_feature_wizard(stdscr, key: str, w: int) -> list[list[str]] | str | None:
    """执行某个功能的引导式向导，返回 argv 列表（多支成片 = 多个 argv）。

    返回特殊值 "__BACK__" 表示回到主菜单；None 表示取消。
    """
    if key == "slice":
        return _wizard_slice(stdscr, w)
    if key == "cover":
        return _wizard_cover(stdscr, w)
    if key == "dedup":
        return _wizard_dedup(stdscr, w)
    if key == "montage":
        return _wizard_montage(stdscr, w)
    if key == "search":
        return _wizard_search(stdscr, w)
    return None


# ---------------------------------------------------------------------------
# 各功能向导
# ---------------------------------------------------------------------------
def _wizard_slice(stdscr, w: int) -> list[list[str]] | None:
    d = _pick_dir(stdscr, "切片 · 选择源视频目录", None)
    if d is None:
        return None
    # TUI 所报数量必须与 slice 实际处理集合一致：3 层、过滤系统目录、仅 MP4。
    from .core.scan import list_videos_recursive  # noqa: PLC0415
    n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
    if n == 0:
        _flash(stdscr, f"目录 3 层内没有可处理的 MP4：{d}", "bad")
        return None
    toggles = [
        ("擦除字幕", "切片时顺带擦除画面字幕", True),
        ("去重", "切片后对产出切片过平台查重", False),
    ]
    vals: list[bool] = []
    ok = _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")], toggles, vals)
    if not ok:
        return None
    sub_on = vals[0] if len(vals) > 0 else True
    dedup_on = vals[1] if len(vals) > 1 else False
    # 切片 argv：擦除字幕默认开启，关闭时加 --no-sub
    argv = ["slice", "--src-dir", d, "--max-depth", "3"]
    if not sub_on:
        argv.append("--no-sub")
    # 勾选「去重」时：切片完成后对桌面切片桶（有人脸/无人脸）自动去重
    argv_list = [argv]
    if dedup_on:
        argv_list.append(["dedup", "--all"])
    return argv_list


def _wizard_cover(stdscr, w: int) -> list[list[str]] | None:
    d = _pick_dir(stdscr, "字幕擦除 · 选择视频目录", None)
    if d is None:
        return None
    # 与 cover_cmd._pick_clips 一致：最多 3 层，只处理 MP4。
    from .core.scan import list_videos_recursive  # noqa: PLC0415
    n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
    if n == 0:
        _flash(stdscr, f"目录 3 层内没有可处理的 MP4：{d}", "bad")
        return None
    ok = _confirm(stdscr, [("源目录", d), ("MP4 数量", f"{n} 个")])
    if not ok:
        return None
    return [["cover", "--src-dir", d]]


def _wizard_dedup(stdscr, w: int) -> list[list[str]] | None:
    d = _pick_dir(stdscr, "去重 · 选择视频目录", None)
    if d is None:
        return None
    n = count_videos_recursive(d)
    if n == 0:
        _flash(stdscr, f"该目录下没有视频：{d}", "bad")
        return None
    ok = _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")])
    if not ok:
        return None
    return [["dedup", "--src-dir", d]]


def _wizard_montage(stdscr, w: int) -> list[list[str]] | None:
    # 1) 片段库目录（递归）
    clip = _pick_dir(stdscr, "混剪 · 选择「混剪片段」目录（递归）", None)
    if clip is None:
        return None
    nc = count_videos_recursive(clip)
    if nc == 0:
        _flash(stdscr, f"片段库内没有视频：{clip}", "bad")
        return None
    # 2) 画中画目录（递归）
    pip = _pick_dir(stdscr, "混剪 · 选择「画中画切片」目录（递归）", None)
    if pip is None:
        return None
    npip = count_videos_recursive(pip)
    # 3) 爆款视频目录（非递归，列出视频多选）
    src_dir = _pick_dir(stdscr, "混剪 · 选择「爆款视频」目录（非递归）", None)
    if src_dir is None:
        return None
    files = list_videos_flat(src_dir)
    if not files:
        _flash(stdscr, f"该目录下没有视频：{src_dir}", "bad")
        return None
    selection = _multi_select(stdscr, "混剪 · 多选爆款视频并设裂变数量", files)
    if selection is None:
        return None
    chosen, fission = selection
    if not chosen:
        return None

    # 汇总确认
    total_out = sum(fission[str(f)] for f in chosen)
    out_dir = str(Path.home() / "Desktop/out")
    lines = [
        ("片段库", f"{clip}（{nc} 个）"),
        ("画中画", f"{pip}（{npip} 个）"),
        ("爆款视频", f"{len(chosen)} 支 × 裂变 = {total_out} 支成片"),
        ("输出目录", out_dir),
    ]
    # Every selected source can be checked on the review page (PgDn to scroll).
    for i, src in enumerate(chosen, 1):
        lines.append((f"来源 {i}", f"{src.name} × {fission[str(src)]}"))
    ok = _confirm(stdscr, lines)
    if not ok:
        return None

    # 多选爆款视频：逐支生成一条 argv（每支带自己的 --fission），run() 循环执行。
    argv_list: list[list[str]] = []
    for src in chosen:
        n = fission[str(src)]
        argv_list.append([
            "montage", "--source", str(src), "--clip-dir", clip,
            "--dedup-dir", pip, "--out-dir", out_dir, "--fission", str(n),
        ])
    return argv_list


def _wizard_search(stdscr, w: int) -> None:
    """查询视频素材：选起点目录 → 输入关键词 → 滚动查看匹配的视频路径。

    这是查看型功能，不生成 argv、不执行处理，看完按 Esc/Q 回到主菜单。
    """
    start = _pick_dir(stdscr, "查素材 · 选择搜索起点目录", None)
    if start is None:
        return None
    kw = _prompt_text(stdscr, 4, 2, w, "输入视频关键词（文件名）")
    if kw is None or not kw.strip():
        _flash(stdscr, "关键词为空，已取消", "dim")
        return None
    files = search_videos(start, kw.strip())
    if not files:
        _flash(stdscr, f"未找到匹配「{kw.strip()}」的视频", "warn")
        return None

    # 结果列表（滚动查看）
    sel = 0
    n = len(files)
    while True:
        h, w2 = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        _frame(stdscr, f"查素材 · 「{kw.strip()}」共 {n} 个", f"搜索起点: {start}")
        # Reserve h-3 for the full path and h-2 for the shortcut bar.
        max_rows = max(1, h - 9)
        top = 0
        if sel >= max_rows:
            top = sel - max_rows + 1
        for i in range(top, min(n, top + max_rows)):
            y = 5 + (i - top)
            p = files[i]
            active = (i == sel)
            mark = "❯" if active else " "
            # 显示：文件名（加粗）+ 相对目录（弱化）
            disp = p.name
            disp = _ellipsize(disp, w2 - 8)
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "text"
            _safe_add(stdscr, y, 3, mark, w2, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, disp, w2, fg, attr)
            # 在文件右侧显示父目录（弱化，方便定位）
            _safe_add(stdscr, y, 5 + _disp_width(disp) + 1, str(p.parent), w2,
                      "dim", attr)
        _safe_add(stdscr, h - 3, 2,
                  _clip(str(files[sel]), max(0, w2 - 6), tail=True), w2, "info")
        _footer(stdscr, " ↑/↓ 滚动 · Enter/O Finder 中定位 · Esc/Q 返回主菜单 ",
                "↑↓浏览  Enter/O定位  Esc/Q返回")
        stdscr.refresh()
        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % max(1, n)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % max(1, n)
        elif k in (10, curses.KEY_ENTER, ord("\r"), ord("o"), ord("O")):
            if sys.platform != "darwin":
                _flash(stdscr, "Finder 定位仅支持 macOS", "warn")
                continue
            import subprocess  # noqa: PLC0415
            try:
                subprocess.Popen(["open", "-R", str(files[sel])],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as exc:
                _flash(stdscr, f"无法打开 Finder：{exc}", "bad")


def _flash(stdscr, msg: str, color: str = "bad") -> None:
    h, w = stdscr.getmaxyx()
    _safe_add(stdscr, h - 3, 2, " " * (w - 4), w, color, curses.A_REVERSE)
    _safe_add(stdscr, h - 3, 2, " " + msg, w, color, curses.A_REVERSE)
    _footer(stdscr, "按任意键继续", "按任意键继续")
    stdscr.refresh()
    stdscr.getch()


def run() -> int:
    """终端交互入口：每次处理后留在主菜单，错误码不再被误报成功。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        cli.build_parser().print_help()
        return 0

    def _app(stdscr) -> int:
        _init_colors()
        final_code = 0
        while True:
            result = _loop(stdscr)
            if not result or result[0] != "run":
                return final_code
            from . import tui_runner  # noqa: PLC0415
            code, should_quit = tui_runner.run_jobs(stdscr, result[1])
            if code:
                final_code = code
            if should_quit:
                return final_code

    try:
        return curses.wrapper(_app)
    except Exception as e:  # noqa: BLE001
        # curses.wrapper 负责在任何异常下恢复终端，错误必须有非零退出码。
        print(f"[TUI 运行失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 2


# _loop 的选中记忆（静态属性）
_loop._feat_sel = 0
