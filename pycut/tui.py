"""四个视频处理向导：Web/.env 配置目录，终端预览素材、确认任务、显示日志。"""
from __future__ import annotations

import curses
import curses.textpad  # noqa: F401  必须显式导入：不会随 import curses 自动加载
import os
import sys
import time
from pathlib import Path

from . import __version__, cli, config
from .core.scan import count_videos_recursive, list_videos_flat
from .folder_picker import choose_directory

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


def _configure_esc_delay() -> None:
    """Avoid ncurses' usual ~1s wait before a standalone Esc is delivered.

    Keep enough time to decode arrow-key escape sequences on macOS terminals.
    Honour users' explicit ESCDELAY environment setting.
    """
    if "ESCDELAY" in os.environ:
        return
    setter = getattr(curses, "set_escdelay", None)
    if callable(setter):
        try:
            setter(75)
        except (curses.error, ValueError):
            pass


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
    ("dedup", "去重",    "调整视频画面，降低相似度"),
    ("montage", "混剪",  "组合片段生成新视频（可批量裂变）"),
]

FEATURE_HINTS = {
    "slice": "场景分段 → 人脸分流 → 可选字幕擦除 / 去重",
    "cover": "读取切片配置 → 全宽矩形马赛克",
    "dedup": "读取切片配置 → 画面处理及相似度自检",
    "montage": "读取目录配置 → 选择爆款视频 → 自动加前贴",
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
    _safe_add(stdscr, 5, 2, "请放大窗口 · Esc 返回 · Q 取消/退出", w, "dim")
    stdscr.refresh()
    return True


def _frame(stdscr, title: str, subtitle: str = "") -> None:
    """Consistent, non-inverted CLI header, title, context and divider."""
    _h, w = stdscr.getmaxyx()
    _safe_add(stdscr, 0, 2, "◈  pycut", w, "accent", curses.A_BOLD)
    version = f"v{__version__}"
    _safe_add(stdscr, 0, max(18, w - _disp_width(version) - 3),
              version, w, "dim")
    _safe_add(stdscr, 1, 2, "─" * max(0, w - 4), w, "muted")
    _safe_add(stdscr, 2, 2,
              _ellipsize(title.replace(" · ", "  /  ", 1), w - 4),
              w, "text", curses.A_BOLD)
    _safe_add(stdscr, 3, 2,
              _ellipsize(subtitle, w - 4, tail=True), w, "dim")
    _safe_add(stdscr, 4, 2, "─" * max(0, w - 4), w, "muted")


def _footer(stdscr, full: str, compact: str) -> None:
    """Show all essential keys, without a bright reversed terminal-wide bar."""
    h, w = stdscr.getmaxyx()
    width = max(0, w - 4)
    full = full.strip()
    compact = compact.strip()
    hint = full if _disp_width(full) <= width else compact
    _safe_add(stdscr, h - 2, 2, _ellipsize(hint, width), w, "dim")


def _home_brand(stdscr) -> None:
    """A single prominent wordmark; instructions belong in the footer."""
    h, w = stdscr.getmaxyx()
    if h >= 20:
        logo = (
            "████   █   █   ████  █   █  █████",
            "█   █   █ █   █      █   █    █",
            "████     █    █      █   █    █",
            "█        █    █      █   █    █",
            "█        █     ████   ███     █",
        )
        for y, line in enumerate(logo, 1):
            _safe_add(stdscr, y, 3, line, w, "accent", curses.A_BOLD)
        divider_y = 7
    else:
        _safe_add(stdscr, 1, 3, "PYCUT", w, "accent", curses.A_BOLD)
        divider_y = 3
    version = f"v{__version__}"
    _safe_add(stdscr, 1, w - _disp_width(version) - 3, version, w, "dim")
    _safe_add(stdscr, divider_y, 2, "─" * max(0, w - 4), w, "muted")


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
        elif k in (27, ord("q"), ord("Q")):
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


def _prompt_text(stdscr, y, x, w, title, default="", *, validate=None) -> str | None:
    """Unicode-capable input; an optional validator keeps bad paths editable."""
    buf = default
    error = ""
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
            stdscr.move(line_y + 1, 0)
            stdscr.clrtoeol()
        except curses.error:
            # 窗口也可能恰好在绘制时发生尺寸变化。
            continue
        prompt = f"{title}："
        if x + _disp_width(prompt) + 4 >= w:
            prompt = "输入："
        _safe_add(stdscr, line_y, x, prompt, w, "text")
        bx = x + _disp_width(prompt) + 1
        _safe_add(stdscr, line_y, bx, _clip(buf, max(0, w - bx - 2), tail=True),
                  w, "value", curses.A_REVERSE)
        hint = (error or ("Enter 确认 · Esc 取消 · Ctrl+U 清空 · Backspace 删除"
                          if supports_unicode else "仅 ASCII 输入 · Enter 确认 · Esc 取消"))
        _safe_add(stdscr, line_y + 1, x, hint, w, "bad" if error else "dim")
        stdscr.refresh()
        try:
            k = get_key()
        except curses.error:
            continue
        if k in ("\n", "\r", 10, 13, curses.KEY_ENTER):
            error = validate(buf) if validate is not None else ""
            if error:
                continue
            return buf
        if k in ("\x1b", 27):
            return None
        if k in ("\x15", 21):  # Standard terminal Ctrl+U: clear the current path.
            buf = ""
            error = ""
        elif k in ("\b", "\x7f", curses.KEY_BACKSPACE, 127, 8):
            buf = _erase_grapheme(buf)
            error = ""
        elif isinstance(k, str) and k.isprintable():
            buf += k
            error = ""
        elif isinstance(k, int) and 32 <= k <= 126:
            buf += chr(k)
            error = ""



def _pick_dir(stdscr, title: str, start: Path | None = None) -> str | None:
    """Pick a directory inside Textual's cross-platform terminal UI.

    The open-source textual-fspicker owns directory navigation and disk lists.
    The existing curses wizard only supplies an initial path and validates the
    returned directory; cancel returns to the preceding wizard step.
    """
    home = Path.home()
    initial = Path(start).expanduser() if start is not None else home
    try:
        if not initial.is_dir():
            initial = home
    except OSError:
        initial = home

    stdscr.erase()
    _frame(stdscr, title, "在终端目录选择器中选择文件夹 · Esc 取消返回")
    stdscr.refresh()

    try:
        selected = choose_directory(title, initial)
    except (OSError, RuntimeError, ImportError) as exc:
        _flash(stdscr, f"终端文件夹选择器不可用：{exc}", "bad")
        return None
    finally:
        # Textual used the same terminal while curses was suspended. Redraw
        # the existing wizard after it restores curses' saved terminal mode.
        stdscr.erase()
        stdscr.refresh()

    if not selected:
        return None
    folder = Path(selected).expanduser()
    try:
        valid = folder.is_absolute() and folder.is_dir()
    except OSError:
        valid = False
    if not valid:
        _flash(stdscr, "目录不存在、已卸载或无权访问，请重新选择", "bad")
        return None
    return str(folder)


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
                attr = curses.A_BOLD if active else curses.A_NORMAL
                _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
                _safe_add(stdscr, y, 5, _pad(label, 12), w, "text", attr)
                _safe_add(stdscr, y, 17, state, w, sc, attr)
                _safe_add(stdscr, y, 17 + _disp_width(state) + 1,
                          _ellipsize(desc, w - 26), w, "dim", attr)
                y += 1
            y += 1

        active = sel == len(toggle_vals)
        attr = curses.A_BOLD if active else curses.A_NORMAL
        _safe_add(stdscr, y, 3, "❯" if active else " ",
                  w, "good" if active else "muted", attr)
        _safe_add(stdscr, y, 5, "▶▶ 确认并开始处理", w, "good", attr)
        _footer(stdscr,
                "↑↓ 选项 · ←/Esc 返回 · → 到确认 · Space 切换 · Enter 确认 · PgUp/PgDn 摘要",
                "↑↓选项  ←返回  →确认  Space切换  Enter执行")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, curses.KEY_LEFT, 127, 8, ord("q"), ord("Q")):
            return False
        if k in (curses.KEY_RIGHT,):
            # Right chooses the confirmation control; only Enter starts jobs.
            sel = len(toggle_vals)
        elif k in (curses.KEY_PPAGE, 339):
            summary_offset = max(0, summary_offset - visible_rows)
        elif k in (curses.KEY_NPAGE, 338):
            summary_offset = min(max(0, len(lines) - visible_rows),
                                 summary_offset + visible_rows)
        elif k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = min(n_items - 1, sel + 1)
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
            attr = curses.A_BOLD if active else curses.A_NORMAL
            name = f.name
            name = _ellipsize(name, w - 24)
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, box, w, bcolor, attr)
            _safe_add(stdscr, y, 5 + 4, name, w,
                      "accent" if active else "text", attr)
            n = fission.get(str(f), 1)
            _safe_add(stdscr, y, w - 12, f"裂变 x{n}", w, "value", attr)

        _footer(stdscr,
                "↑↓ 移动 · Space 多选 · ←/Esc 返回 · →/Enter 下一步 · E 裂变数量 · A 全选",
                "↑↓移动  Space多选  ←返回  →下一步  E数量")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8,
                 ord("q"), ord("Q")):
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
        elif k in (10, curses.KEY_ENTER, ord("\r"), curses.KEY_RIGHT, ord("l")):
            if not selected:
                continue
            chosen = [files[i] for i in sorted(selected)]
            return chosen, {str(f): fission[str(f)] for f in chosen}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _loop(stdscr) -> tuple[str, list[list[str]]] | None:
    """Up/down moves, Right/Enter opens, Back never exits the home screen."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    feat_sel = max(0, min(getattr(_loop, "_feat_sel", 0), len(FEATURES) - 1))
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (ord("q"), ord("Q")):
                return ("quit", None)
            continue

        stdscr.erase()
        _home_brand(stdscr)
        spacious = h >= 20
        menu_y = 9 if spacious else 5
        row_step = 2 if h >= 29 else 1

        for i, (key, name, desc) in enumerate(FEATURES):
            y = menu_y + i * row_step
            active = i == feat_sel
            attrs = curses.A_BOLD if active else curses.A_NORMAL
            _safe_add(stdscr, y, 3, "▌" if active else " ", w,
                      "accent" if active else "muted")
            _safe_add(stdscr, y, 5, f"{i + 1:02d}", w, "dim")
            _safe_add(stdscr, y, 9, _pad(name, 10), w,
                      "accent" if active else "text", attrs)
            _safe_add(stdscr, y, 20, _ellipsize(desc, w - 23), w,
                      "text" if active else "dim")

        key, name, _desc = FEATURES[feat_sel]
        summary_y = h - 4
        if summary_y > menu_y + (len(FEATURES) - 1) * row_step:
            _safe_add(stdscr, summary_y, 3,
                      f"当前：{name}  ·  {FEATURE_HINTS[key]}", w, "dim")
        _footer(stdscr,
                f"↑↓ 移动 · →/Enter 进入 · 1-{len(FEATURES)} 快捷键 · W 配置网页 · Q 退出",
                f"↑↓移动  →进入  W配置  Q退出")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (ord("q"), ord("Q")):
            return ("quit", None)
        if k in (ord("w"), ord("W")):
            from . import web_config
            _flash(stdscr, "配置网页：" + web_config.start(), "text")
            continue
        if k in (curses.KEY_UP, ord("k")):
            feat_sel = max(0, feat_sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            feat_sel = min(len(FEATURES) - 1, feat_sel + 1)
        elif k in (10, curses.KEY_ENTER, ord("\r"),
                   curses.KEY_RIGHT, ord("l")) or (
            ord("1") <= k < ord("1") + len(FEATURES)
        ):
            if ord("1") <= k < ord("1") + len(FEATURES):
                feat_sel = k - ord("1")
            _loop._feat_sel = feat_sel
            argv_list = _run_feature_wizard(stdscr, FEATURES[feat_sel][0], w)
            if argv_list == "__BACK__":
                continue
            if argv_list:
                return ("run", argv_list)
        # Esc/left/backspace and space do not exit or launch from the home menu.
        _loop._feat_sel = feat_sel



def _run_feature_wizard(stdscr, key: str, w: int) -> list[list[str]] | str | None:
    """执行某个功能的引导式向导，返回 argv 列表（多支成片 = 多个 argv）。

    返回特殊值 "__BACK__" 表示回到主菜单；None 表示取消。
    """
    from . import settings, web_config
    config.refresh()
    errors = settings.problems(settings.effective())
    if errors:
        url = web_config.start()
        _flash(stdscr, "请先在网页配置：" + url + " · " + "；".join(errors), "bad")
        return None
    if key == "slice":
        return _wizard_slice(stdscr, w)
    if key == "cover" and not config.key_ready():
        _flash(stdscr, "字幕定位需要配置 LLM 主网关密钥，按 W 打开配置网页", "bad")
        return None
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
    from .core.scan import list_videos_recursive
    d = str(config.SRC_DIR)
    n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
    if not n:
        _flash(stdscr, f"源目录 3 层内没有 MP4：{d}", "bad")
        return None
    values = []
    if not _confirm(stdscr, [("源目录", d), ("切片输出", str(config.CLIP_DIR)),
                            ("视频数量", f"{n} 个")],
                    [("擦除字幕", "全宽矩形马赛克", True),
                     ("去重", "仅处理本次生成的切片", False)], values):
        return None
    if values[0] and not config.key_ready():
        _flash(stdscr, "擦除字幕需要配置 LLM 主网关密钥，按 W 打开配置网页", "bad")
        return None
    output = config.CLIP_DIR / str(time.time_ns())
    argv = ["slice", "--src-dir", d, "--max-depth", "3", "--out-dir", str(output)]
    if not values[0]:
        argv.append("--no-sub")
    jobs = [argv]
    if values[1]:
        jobs.append(["dedup", "--src-dir", str(output), "--force", "--source-stems",
                     *[p.stem for p in list_videos_recursive(d) if p.suffix.lower() == ".mp4"]])
    return jobs


def _wizard_cover(stdscr, w: int) -> list[list[str]] | None:
    from .core.scan import list_videos_recursive
    d = str(config.CLIP_DIR)
    n = sum(p.suffix.lower() == ".mp4" for p in list_videos_recursive(d))
    if not n:
        _flash(stdscr, f"切片目录 3 层内没有 MP4：{d}", "bad")
        return None
    if _confirm(stdscr, [("切片目录", d), ("字幕处理", "按字幕高度覆盖全宽矩形马赛克"),
                        ("MP4 数量", f"{n} 个")]):
        return [["cover", "--src-dir", d]]
    return None


def _wizard_dedup(stdscr, w: int) -> list[list[str]] | None:
    d = str(config.CLIP_DIR)
    n = count_videos_recursive(d)
    if not n:
        _flash(stdscr, f"切片目录没有视频：{d}", "bad")
        return None
    if _confirm(stdscr, [("切片目录", d), ("视频数量", str(n)),
                        ("输出", str(config.DEDUP_OUT))]):
        return [["dedup", "--src-dir", d]]
    return None


def _count_sticker_images(folder: Path) -> int:
    """Count available stickers for the existing montage overlay."""
    if not folder.is_dir():
        return 0
    try:
        return sum(p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                   for p in folder.rglob("*"))
    except OSError:
        return 0


def _wizard_montage(stdscr, w: int) -> list[list[str]] | None:
    """Use configured libraries, then select references and confirm."""
    from .core.montage_clips import _collect_files, VIDEO_EXTS
    directories = [str(config.REFERENCE_DIR), str(config.MONTAGE_CLIP_DIR),
                   str(config.MONTAGE_DEDUP_DIR)]
    # Match the actual montage collector: recursive, same supported formats.
    counts = [len(_collect_files(Path(d))) for d in directories[1:]]
    missing = [f"{label}：{path}" for label, path, count in
               zip(("背景切片", "画中画切片"), directories[1:], counts) if not count]
    if missing:
        _flash(stdscr, "未找到混剪候选\n" + "\n".join(missing)
               + "\n递归支持 " + "/".join(ext[1:].upper() for ext in VIDEO_EXTS)
               + "，请检查 Web 中该目录及其子目录", "bad")
        return None
    clip_count, pip_count = counts
    selection = None
    step = 3
    while True:
        if step == 3:
            assert directories[0] is not None
            files = list_videos_flat(directories[0])
            if not files:
                _flash(stdscr, f"该目录下没有视频：{directories[0]}", "bad")
                return None
            result = _multi_select(stdscr, "混剪 · 1/2 选择爆款视频及裂变数量",
                                   files, initial=selection)
            if result is None:
                return None
            selection = result
            step = 4
            continue

        assert selection is not None
        assert all(d is not None for d in directories)
        src_dir, clip, pip = directories
        chosen, fission = selection
        total_out = sum(fission[str(f)] for f in chosen)
        out_dir = str(config.MONTAGE_OUT)
        lines = [
            ("片段库", f"{clip}（{clip_count} 个）"),
            ("画中画", f"{pip}（{pip_count} 个）"),
            ("规格", "720×1280 · 9:16 · 固定右上角画中画"),
            ("切换效果", "段间转场 · 背景与画中画标准档扰动"),
            ("贴纸", "本地优先，未配置则在线下载"),
            ("前贴", f"{config.INTRO_DIR} · 自动去重、贴纸，保留字幕"),
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
            argv = [
                "montage", "--source", str(src), "--clip-dir", clip,
                "--dedup-dir", pip, "--out-dir", out_dir,
                "--intro-dir", str(config.INTRO_DIR),
            ]
            argv.extend(["--fission", str(fission[str(src)])])
            argv_list.append(argv)
        return argv_list



def _flash(stdscr, msg: str, color: str = "bad") -> None:
    h, w = stdscr.getmaxyx()
    lines = []
    for paragraph in msg.splitlines():
        while _disp_width(paragraph) > max(2, w - 6):
            part = _clip(paragraph, max(2, w - 6))
            lines.append(part)
            paragraph = paragraph[len(part):]
        lines.append(paragraph)
    lines = lines[:max(1, h - 4)]
    for row, line in enumerate(lines, h - 2 - len(lines)):
        _safe_add(stdscr, row, 2, " " * (w - 4), w, color, curses.A_REVERSE)
        _safe_add(stdscr, row, 2, " " + line, w, color, curses.A_REVERSE)
    _footer(stdscr, "按任意键继续", "按任意键继续")
    stdscr.refresh()
    stdscr.getch()


def run() -> int:
    """终端交互入口：每次处理后留在主菜单，错误码不再被误报成功。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        cli.build_parser().print_help()
        return 0

    def _app(stdscr) -> int:
        _configure_esc_delay()
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

    from . import web_config
    try:
        url = web_config.start()
        print(f"[配置页面] {url}")
        return curses.wrapper(_app)
    except Exception as e:  # noqa: BLE001
        # curses.wrapper 负责在任何异常下恢复终端，错误必须有非零退出码。
        print(f"[TUI 运行失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        web_config.stop()


# _loop 的选中记忆（静态属性）
_loop._feat_sel = 0
