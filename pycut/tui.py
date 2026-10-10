"""pycut 交互式终端界面（curses TUI）—— 引导式向导。

风格取向：Claude Code 观感——深色底、琥珀色强调、顶部 logo+版本号、底部状态栏、
❯ 选中标记、语法高亮。

交互模型（引导式向导，替代旧的「参数表单」）：
    主菜单列出 4 个视频处理功能，选中后进入**分步向导**，每步只问一个决定：

      切片        → 选源目录 → 报视频数 → 选「擦除字幕/去重」开关 → 确认执行
      字幕擦除    → 选目录 → 报视频数 → 确认执行（只有擦除，无去重）
      去重        → 选目录 → 报视频数 → 确认执行（只有去重）
      混剪        → 选片段目录 → 选画中画目录 → 选爆款视频目录 →
                    多选爆款视频 + 每个裂变数量 → 确认执行

关键机制：
  - **目录选择器**：文件系统层级浏览器，从根目录出发逐级浏览，进入子目录 /
    返回上级 / 直接选当前目录，自动过滤系统级目录与隐藏目录，不要求手敲路径。
  - **递归扫描**：选定目录后用 `core/scan` 递归统计视频数并展示（最多下探 3 层）。
  - **多选**（混剪爆款视频）：空格勾选/取消，数字输入裂变数量。
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
from .core.scan import count_videos_recursive, list_dirs, list_videos_flat

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
]

FEATURE_HINTS = {
    "slice": "场景分段 → 人脸分流 → 可选字幕擦除 / 去重",
    "cover": "选择视频目录 → 自动检测并擦除字幕",
    "dedup": "选择素材目录 → 运行画面处理及相似度自检",
    "montage": "选素材库 → 选画中画 → 选择多支基准视频",
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
    _safe_add(stdscr, 5, 2, "请放大窗口 · Esc 返回 · Q 退出", w, "dim")
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


def _home_brand(stdscr) -> None:
    """A visible CLI-style identity lockup on spacious terminals."""
    _h, w = stdscr.getmaxyx()
    _safe_add(stdscr, 5, 3, "╭─────╮", w, "muted")
    _safe_add(stdscr, 6, 3, "│  ▶  │", w, "accent", curses.A_BOLD)
    _safe_add(stdscr, 7, 3, "╰─────╯", w, "muted")
    _safe_add(stdscr, 5, 14, "P Y C U T", w, "accent", curses.A_BOLD)
    _safe_add(stdscr, 6, 14, f"v{__version__}  ·  Terminal video workstation",
              w, "text")
    _safe_add(stdscr, 7, 14, "选择工作流程  /  Choose a workflow", w, "dim")
    _safe_add(stdscr, 9, 2, "─" * max(0, w - 4), w, "muted")


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
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        line_y = min(y, h - 4)
        w = screen_w
        try:
            stdscr.move(line_y, 0)
            stdscr.clrtoeol()
        except curses.error:
            # Terminal may resize between measuring its height and moving the cursor.
            continue
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
    """Browse folders like a regular file picker.

    Enter opens a folder (or confirms the last "use this folder" row);
    Backspace/Left goes to its parent, Esc goes back to the wizard.
    Space confirms the currently displayed directory. The default is home,
    with ~ and / available for home/root navigation.
    """
    home = Path.home()
    cur = Path(start).expanduser() if start is not None else home
    if not cur.is_dir():
        cur = home if home.is_dir() else Path("/")

    def _entries() -> list[tuple[str, str, Path]]:
        items: list[tuple[str, str, Path]] = []
        if cur != cur.parent:
            items.append(("parent", "../  上一级", cur.parent))
        items.extend(("directory", f"{p.name}/", p) for p in list_dirs(cur))
        items.append(("select", "✓  使用当前文件夹", cur))
        return items

    entries = _entries()
    # Initial opening favours a child folder. On returning from a confirmation
    # page, focus the already chosen directory instead of entering it again.
    sel = (len(entries) - 1 if start is not None else
           next((i for i, item in enumerate(entries)
                 if item[0] == "directory"), len(entries) - 1))

    def _change_dir(target: Path, *, focus_child: Path | None = None) -> None:
        nonlocal cur, entries, sel
        if not target.is_dir():
            return
        cur = target
        entries = _entries()
        # After entering a directory, Enter a second time selects it.
        # After moving up, highlight the child we just left.
        sel = next((i for i, (kind, _label, path) in enumerate(entries)
                    if kind == "directory" and path == focus_child),
                   len(entries) - 1)

    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        _frame(stdscr, title, str(cur))

        sel = min(sel, len(entries) - 1)
        max_rows = max(1, h - 9)
        top = min(max(0, sel - max_rows + 1), max(0, len(entries) - max_rows))
        for i in range(top, min(len(entries), top + max_rows)):
            kind, label, _target = entries[i]
            y = 5 + i - top
            active = i == sel
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            icon = "✓" if kind == "select" else "↖" if kind == "parent" else "▸"
            color = "good" if kind == "select" else "accent" if active else "text"
            _safe_add(stdscr, y, 3, "❯" if active else " ", w,
                      "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, icon, w, color, attr)
            _safe_add(stdscr, y, 8, _ellipsize(label, w - 12, tail=False), w,
                      color, attr)

        _safe_add(stdscr, h - 3, 2,
                  f"子目录 {sum(kind == 'directory' for kind, _, _ in entries)} 个 · {sel + 1}/{len(entries)}",
                  w, "dim")
        _footer(stdscr,
                " ↑↓ 选择 · Enter 打开/确认 · Space 使用当前目录 · ←/Backspace 上级 · ~ 主目录 · / 根 · Esc 返回 ",
                "Esc返回 ←上级 Enter打开 Space选当前 ↑↓移动 ~主目录 /根")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = min(len(entries) - 1, sel + 1)
        elif k in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8, ord("h")):
            if cur != cur.parent:
                old = cur
                _change_dir(cur.parent, focus_child=old)
        elif k == ord("~"):
            _change_dir(home)
        elif k == ord("/"):
            _change_dir(Path("/"))
        elif k == ord(" "):
            return str(cur)
        elif k in (10, curses.KEY_ENTER, ord("\r"), curses.KEY_RIGHT, ord("l")):
            kind, _label, target = entries[sel]
            if kind == "select":
                if k not in (curses.KEY_RIGHT, ord("l")):
                    return str(cur)
            elif kind == "parent":
                old = cur
                _change_dir(target, focus_child=old)
            else:
                _change_dir(target)


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
def _multi_select(stdscr, title: str, files: list[Path],
                  initial: tuple[list[Path], dict[str, int]] | None = None
                  ) -> tuple[list[Path], dict[str, int]] | None:
    """多选视频列表 + 每个的裂变数量。返回 (选中列表, {路径: 裂变数})；Esc 返回 None。

    空格勾选/取消；选中项上按数字键 0-9 设置裂变数量（默认 1）；A 全选/取消全选。
    """
    if not files:
        return None
    previous_files, previous_counts = initial if initial is not None else ([], {})
    previous_paths = {str(p) for p in previous_files}
    selected = {i for i, f in enumerate(files) if str(f) in previous_paths}
    fission = {str(f): previous_counts.get(str(f), 1) for f in files}
    sel = min(selected) if selected else 0
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
        if k in (27, ord("q"), ord("Q")):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = min(len(files) - 1, sel + 1)
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
    """Main menu: Back stays here; only Q explicitly exits the application."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    feat_sel = max(0, min(getattr(_loop, "_feat_sel", 0), len(FEATURES) - 1))
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            # Back on the final (home) page must not close the process.
            if stdscr.getch() in (ord("q"), ord("Q")):
                return ("quit", None)
            continue
        stdscr.erase()
        _frame(stdscr, "选择功能", "本地视频工作台  /  选择一个处理流程")
        spacious = h >= 20
        if spacious:
            _home_brand(stdscr)
        menu_y = 11 if spacious else 5

        for i, (key, name, desc) in enumerate(FEATURES):
            y = menu_y + i
            active = i == feat_sel
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "keyword"
            _safe_add(stdscr, y, 3, "❯" if active else " ", w,
                      "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, f"{i + 1:02d}  ", w, "dim", attr)
            _safe_add(stdscr, y, 9, _pad(name, 10), w, fg, attr)
            _safe_add(stdscr, y, 20, desc, w, "dim", attr)

        key, name, _desc = FEATURES[feat_sel]
        if spacious:
            _safe_add(stdscr, 16, 3, f"当前选择  /  {name}", w,
                      "accent", curses.A_BOLD)
            if h >= 22:
                _safe_add(stdscr, 17, 3, FEATURE_HINTS[key], w, "text")
        elif h >= 17:
            _safe_add(stdscr, 11, 2, "─" * max(0, w - 4), w, "muted")
            _safe_add(stdscr, 12, 3, f"当前选择  /  {name}", w,
                      "accent", curses.A_BOLD)
            _safe_add(stdscr, 13, 3, FEATURE_HINTS[key], w, "text")
        _footer(stdscr,
                f" ↑↓ 移动 · 1-{len(FEATURES)} 快速进入 · Enter 选择 · Esc 返回主菜单 · Q 退出 ",
                f"Esc返回 Q退出 ↑↓移动 1-{len(FEATURES)}快捷 Enter进入")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (ord("q"), ord("Q")):
            return ("quit", None)
        if k in (curses.KEY_UP, ord("k")):
            feat_sel = max(0, feat_sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            feat_sel = min(len(FEATURES) - 1, feat_sel + 1)
        elif k in (10, curses.KEY_ENTER, ord("\r"), ord(" ")) or (
            ord("1") <= k < ord("1") + len(FEATURES)
        ):
            if ord("1") <= k < ord("1") + len(FEATURES):
                feat_sel = k - ord("1")
            _loop._feat_sel = feat_sel
            key = FEATURES[feat_sel][0]
            argv_list = _run_feature_wizard(stdscr, key, w)
            if argv_list == "__BACK__":
                continue
            if argv_list:
                return ("run", argv_list)
        # Esc/Left/Backspace are intentionally no-ops at the root menu.
        # They never call curses.endwin() or terminate the application.
        _loop._feat_sel = feat_sel



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
    return None


# ---------------------------------------------------------------------------
# 各功能向导
# ---------------------------------------------------------------------------
def _wizard_slice(stdscr, w: int) -> list[list[str]] | None:
    """Choose a folder, review existing options, and go back one step on Esc."""
    from .core.scan import list_videos_recursive  # noqa: PLC0415

    d: str | None = None
    settings = [True, False]
    while True:
        selected = _pick_dir(stdscr, "切片 · 1/2 选择源视频目录",
                             start=Path(d) if d else None)
        if selected is None:
            return None
        d = selected
        # Keep the TUI preview in sync with the CLI's explicit depth limit.
        n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
        if n == 0:
            _flash(stdscr, f"目录 3 层内没有可处理的 MP4：{d}", "bad")
            continue

        toggles = [
            ("擦除字幕", "切片时顺带擦除画面字幕", settings[0]),
            ("去重", "切片后对产出切片过平台查重", settings[1]),
        ]
        values: list[bool] = []
        ok = _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")],
                      toggles, values)
        if len(values) == len(settings):
            settings = values
        if not ok:
            # The next iteration revisits the selected directory, not main.
            continue

        argv = ["slice", "--src-dir", d, "--max-depth", "3"]
        if not settings[0]:
            argv.append("--no-sub")
        argv_list = [argv]
        if settings[1]:
            argv_list.append(["dedup", "--all"])
        return argv_list


def _wizard_cover(stdscr, w: int) -> list[list[str]] | None:
    from .core.scan import list_videos_recursive  # noqa: PLC0415

    d: str | None = None
    while True:
        selected = _pick_dir(stdscr, "字幕擦除 · 1/2 选择视频目录",
                             start=Path(d) if d else None)
        if selected is None:
            return None
        d = selected
        n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
        if n == 0:
            _flash(stdscr, f"目录 3 层内没有可处理的 MP4：{d}", "bad")
            continue
        if _confirm(stdscr, [("源目录", d), ("MP4 数量", f"{n} 个")]):
            return [["cover", "--src-dir", d]]
        # Esc in the review opens the directory picker again.


def _wizard_dedup(stdscr, w: int) -> list[list[str]] | None:
    d: str | None = None
    while True:
        selected = _pick_dir(stdscr, "去重 · 1/2 选择视频目录",
                             start=Path(d) if d else None)
        if selected is None:
            return None
        d = selected
        n = count_videos_recursive(d)
        if n == 0:
            _flash(stdscr, f"该目录下没有视频：{d}", "bad")
            continue
        if _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")]):
            return [["dedup", "--src-dir", d]]


def _wizard_montage(stdscr, w: int) -> list[list[str]] | None:
    """Five existing steps with reversible navigation and preserved selection.

    0: clip directory  1: PiP directory  2: source directory
    3: source selection / fission  4: confirmation
    """
    titles = (
        "混剪 · 1/5 混剪片段目录（递归）",
        "混剪 · 2/5 画中画切片目录（递归）",
        "混剪 · 3/5 爆款视频目录（非递归）",
    )
    directories: list[str | None] = [None, None, None]
    clip_count = 0
    pip_count = 0
    selection: tuple[list[Path], dict[str, int]] | None = None
    step = 0

    while True:
        if step < 3:
            previous = directories[step]
            if previous is None and step > 0:
                previous = directories[step - 1]
            selected = _pick_dir(stdscr, titles[step],
                                 start=Path(previous) if previous else None)
            if selected is None:
                if step == 0:
                    return None
                step -= 1
                continue

            if step == 0:
                clip_count = count_videos_recursive(selected)
                if clip_count == 0:
                    _flash(stdscr, f"片段库内没有视频：{selected}", "bad")
                    continue
            elif step == 1:
                pip_count = count_videos_recursive(selected)
                # A blank PiP directory is supported by the original pipeline.
            else:
                files = list_videos_flat(selected)
                if not files:
                    _flash(stdscr, f"该目录下没有视频：{selected}", "bad")
                    continue
            directories[step] = selected
            step += 1
            continue

        if step == 3:
            assert directories[2] is not None
            files = list_videos_flat(directories[2])
            if not files:
                _flash(stdscr, f"该目录下没有视频：{directories[2]}", "bad")
                step = 2
                continue
            result = _multi_select(stdscr, "混剪 · 4/5 选择视频及裂变数量",
                                   files, initial=selection)
            if result is None:
                step = 2
                continue
            selection = result
            step = 4
            continue

        assert selection is not None
        assert all(d is not None for d in directories)
        clip, pip, src_dir = directories
        chosen, fission = selection
        total_out = sum(fission[str(f)] for f in chosen)
        out_dir = str(Path.home() / "Desktop/out")
        lines = [
            ("片段库", f"{clip}（{clip_count} 个）"),
            ("画中画", f"{pip}（{pip_count} 个）"),
            ("爆款视频", f"{len(chosen)} 支 × 裂变 = {total_out} 支成片"),
            ("输出目录", out_dir),
        ]
        for i, src in enumerate(chosen, 1):
            lines.append((f"来源 {i}", f"{src.name} × {fission[str(src)]}"))
        if not _confirm(stdscr, lines):
            step = 3
            continue

        # Preserve the existing argv/CLI contract exactly.
        argv_list: list[list[str]] = []
        for src in chosen:
            argv_list.append([
                "montage", "--source", str(src), "--clip-dir", clip,
                "--dedup-dir", pip, "--out-dir", out_dir,
                "--fission", str(fission[str(src)]),
            ])
        return argv_list



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
