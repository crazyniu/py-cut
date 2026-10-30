"""Processing wizards using Web/.env settings and CLI subprocesses."""
from __future__ import annotations

import curses
import os
import sys
from pathlib import Path

from . import cli, config
from .core.scan import list_videos_recursive
from .folder_picker import choose_directory


_COLOR_DEFS: dict[str, tuple[str, int, int]] = {
    "accent":    ("amber",     214,            -1),
    "dim":       ("gray",      244,            -1),
    "muted":     ("dimgray",   238,            -1),
    "text":      ("white",     255,            -1),
    "good":      ("green",     114,            -1),
    "warn":      ("yellow",    221,            -1),
    "bad":       ("red",       203,            -1),
    "info":      ("cyan",      81,             -1),
    "keyword":   ("purple",    141,            -1),
    "value":     ("blue",      117,            -1),
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
    """Read COLORFGBG; keep the default palette when the background is unknown.

    Curses palette entries do not identify the actual terminal background."""
    value = os.environ.get("COLORFGBG", "").split(";")
    try:
        return bool(value) and int(value[-1]) in (7, 15)
    except ValueError:
        return False


def _attr(name: str) -> int:
    if not _HAS_COLORS:
        return curses.A_BOLD if name in ("accent", "good", "bad", "keyword") else 0
    return curses.color_pair(1 + list(_COLOR_DEFS).index(name))


FEATURES = [
    ("cover", "字幕擦除", "擦除画面中的字幕"),
    ("dedup", "去重",    "调整视频画面，降低相似度"),
    ("montage", "混剪",  "组合片段生成新视频（可批量裂变）"),
    ("fission", "抖音裂变", "逐个设置数量，复制视频并追加尾部随机字节"),
]

FEATURE_HINTS = {
    "cover": "读取用户切片 → 单层毛玻璃遮盖字幕",
    "dedup": "读取切片配置 → 画面处理及相似度自检",
    "montage": "读取目录配置 → 选择话术 → 数量 → 确认混剪",
    "fission": "读取原视频目录 → 逐个设置数量 → 确认裂变",
}


def _char_width(char: str) -> int:
    import unicodedata  # noqa: PLC0415
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" or ord(char) >= 0x1F300 else 1


def _disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def _clip(s: str, width: int, *, tail: bool = False) -> str:
    """Clip by terminal display columns without splitting wide characters."""
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
    header = _brand(stdscr)
    _safe_add(stdscr, header, 2, "终端窗口太小", w, "warn", curses.A_BOLD)
    _safe_add(stdscr, header + 2, 2, f"当前 {w}×{h}，至少需要 60×14", w, "text")
    _safe_add(stdscr, header + 4, 2, "请放大窗口 · Esc 返回 · Q 取消/退出", w, "dim")
    stdscr.refresh()
    return True


_LOGO = (
    "████   █   █   ████  █   █  █████",
    "█   █   █ █   █      █   █    █",
    "████     █    █      █   █    █",
    "█        █    █      █   █    █",
    "█        █     ████   ███     █",
)


def _brand(stdscr) -> int:
    """Logo, actionable configuration help and one shared content divider."""
    from . import web_config
    h, w = stdscr.getmaxyx()
    logo = _LOGO if h >= 20 and w >= 60 else ("▶ PYCUT",)
    url = web_config.start(open_browser=False)
    for y, line in enumerate(logo):
        _safe_add(stdscr, y, 2, line, w, "accent", curses.A_BOLD)
    settings_x = 2 + max(_disp_width(line) for line in logo) + 5
    explanation = "设置素材目录和模型，保存后回到终端"
    if len(logo) > 1 and settings_x + _disp_width(explanation) <= w - 2:
        _safe_add(stdscr, 0, settings_x, "素材目录与 LLM 配置", w, "text", curses.A_BOLD)
        _safe_add(stdscr, 1, settings_x, url, w, "value")
        _safe_add(stdscr, 2, settings_x, "复制地址到浏览器打开", w, "text")
        _safe_add(stdscr, 3, settings_x, explanation, w, "dim")
        divider = len(logo)
    else:
        _safe_add(stdscr, len(logo), 2, "素材/LLM设置 · 复制地址到浏览器打开", w, "text")
        _safe_add(stdscr, len(logo) + 1, 2, url, w, "value")
        divider = len(logo) + 2
    _safe_add(stdscr, divider, 2, "─" * max(0, w - 4), w, "muted")
    return divider + 1


def _frame(stdscr, title: str, subtitle: str = "") -> int:
    """Shared brand/title/context; return the first free content row."""
    _h, w = stdscr.getmaxyx()
    header = _brand(stdscr)
    _safe_add(stdscr, header, 2,
              _ellipsize(title.replace(" · ", "  /  ", 1), w - 4),
              w, "text", curses.A_BOLD)
    _safe_add(stdscr, header + 1, 2,
              _ellipsize(subtitle, w - 4, tail=True), w, "dim")
    return header + 2


def _footer(stdscr, full: str, compact: str) -> None:
    """Show all essential keys, without a bright reversed terminal-wide bar."""
    h, w = stdscr.getmaxyx()
    width = max(0, w - 4)
    full = full.strip()
    compact = compact.strip()
    hint = full if _disp_width(full) <= width else compact
    _safe_add(stdscr, h - 2, 2, _ellipsize(hint, width), w, "dim")


def _home_brand(stdscr) -> int:
    """The home screen uses the shared large wordmark and responsive height."""
    _h, w = stdscr.getmaxyx()
    return _brand(stdscr) + 1


def _prompt_int(stdscr, y, x, w, title, default, lo, hi) -> int | None:
    """Edit an integer; the first digit replaces the default and invalid values stay editable."""
    buf = str(default)
    untouched = True
    error = ""
    while True:
        h, screen_w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        header = _brand(stdscr)
        line_y = max(header, min(y, h - 4))
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
        header = _brand(stdscr)
        line_y = max(header, min(y, h - 4))
        w = screen_w
        try:
            stdscr.move(line_y, 0)
            stdscr.clrtoeol()
            stdscr.move(line_y + 1, 0)
            stdscr.clrtoeol()
        except curses.error:
            # The terminal can resize between measurement and drawing.
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


def _confirm(stdscr, lines: list[tuple[str, str]],
             toggles: list[tuple[str, str, bool]] | None = None,
             out_vals: list[bool] | None = None) -> bool:
    """Return whether Enter confirmed the task.

    lines contains (label, value) pairs. toggles contains (label, description, initial)
    triples; out_vals receives their current states, including edits before returning."""
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
        stdscr.erase()
        body = _frame(stdscr, "处理前确认")
        visible_rows = max(1, h - body - 8 - len(toggle_vals)) if toggles else max(1, h - body - 4)
        summary_offset = min(summary_offset, max(0, len(lines) - visible_rows))
        has_more = len(lines) > visible_rows
        subtitle = "检查输入与选项"
        if has_more:
            subtitle += (f" · 第 {summary_offset + 1}-"
                         f"{min(len(lines), summary_offset + visible_rows)}/{len(lines)} 行"
                         " · PgUp/PgDn 查看")
        else:
            subtitle += "，按 Enter 开始执行"
        _safe_add(stdscr, body - 2, 2, _ellipsize(subtitle, w - 4, tail=True), w, "dim")

        label_width = 16 if w >= 80 else 12
        value_x = 2 + label_width
        y = body
        for label, value in lines[summary_offset:summary_offset + visible_rows]:
            _safe_add(stdscr, y, 2,
                      _pad(_ellipsize(str(label), label_width - 1), label_width),
                      w, "dim")
            _safe_add(stdscr, y, value_x,
                      _ellipsize(str(value), w - value_x - 2, tail=True),
                      w, "value")
            y += 1

        if toggles:
            y += 1 if h >= 20 else 0
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


def _reference_label(source: Path) -> str:
    try:
        return str(source.resolve().relative_to(config.REFERENCE_DIR.resolve()))
    except ValueError:
        return source.name


def _multi_select(stdscr, title: str, files: list[Path],
                  initial: tuple[list[Path], dict[str, int]] | None = None
                  ) -> tuple[list[Path], dict[str, int]] | None:
    """Select reference videos while retaining selections and the common variant count."""
    if not files:
        return None
    previous_files, previous_counts = initial if initial is not None else ([], {})
    previous_paths = {str(p) for p in previous_files}
    selected = {i for i, f in enumerate(files) if str(f) in previous_paths}
    shared_count = next(iter(previous_counts.values()), 1)
    fission = {str(f): shared_count for f in files}
    sel = min(selected) if selected else 0
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        body = _frame(stdscr, title, f"{len(files)} 个话术 · 已选 {len(selected)} 个 · 下一步统一设置数量")

        max_rows = max(1, h - body - 3)
        top = 0
        if sel >= max_rows:
            top = sel - max_rows + 1
        for i in range(top, min(len(files), top + max_rows)):
            y = body + (i - top)
            f = files[i]
            active = (i == sel)
            checked = i in selected
            mark = "❯" if active else " "
            box = "[✓]" if checked else "[ ]"
            bcolor = "good" if checked else "muted"
            attr = curses.A_BOLD if active else curses.A_NORMAL
            name = _reference_label(f)
            name = _ellipsize(name, w - 11)
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, box, w, bcolor, attr)
            _safe_add(stdscr, y, 5 + 4, name, w,
                      "accent" if active else "text", attr)

        _footer(stdscr,
                "↑↓ 移动 · Space 多选 · A 全选 · ←/Esc 返回 · →/Enter 设置统一数量",
                "↑↓移动  Space多选  A全选  ←返回  →数量")
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
        elif k in (10, curses.KEY_ENTER, ord("\r"), curses.KEY_RIGHT, ord("l")):
            if not selected:
                continue
            chosen = [files[i] for i in sorted(selected)]
            return chosen, {str(f): fission[str(f)] for f in chosen}


def _set_fission_counts(stdscr, chosen: list[Path], counts: dict[str, int]) -> bool:
    """Apply one count to every selected reference; cancellation preserves saved counts."""
    if not chosen:
        return False
    _, w = stdscr.getmaxyx()
    stdscr.erase()
    body = _frame(stdscr, "混剪 · 2/3 设置裂变数量",
                  f"已选 {len(chosen)} 个话术 · 每个话术生成相同数量（1～99 条）")
    _safe_add(stdscr, body, 2, "统一应用到所有选中的话术视频", w, "accent", curses.A_BOLD)
    _safe_add(stdscr, body + 1, 2,
              f"例如：每个生成 5 条，共生成 {len(chosen) * 5} 支成片", w, "text")
    _footer(stdscr, "Enter 保存数量并继续 · Esc 返回视频选择", "Enter 继续 · Esc 返回")
    stdscr.refresh()
    number = _prompt_int(stdscr, body + 3, 2, w, "每个话术生成条数", counts[str(chosen[0])], 1, 99)
    if number is None:
        return False
    counts.update((str(source), number) for source in chosen)
    return True


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
        menu_y = _home_brand(stdscr)
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
                f"↑↓ 移动 · →/Enter 进入 · 1-{len(FEATURES)} 快捷键 · W 设置地址 · Q 退出",
                f"↑↓移动  →进入  W地址  Q退出")
        stdscr.refresh()

        k = stdscr.getch()
        if k in (ord("q"), ord("Q")):
            return ("quit", None)
        if k in (ord("w"), ord("W")):
            from . import web_config
            _flash(stdscr, "手动在浏览器访问：" + web_config.start(), "text")
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
    """Reload settings and return CLI argument lists for the selected wizard.

    None cancels; the legacy __BACK__ sentinel returns to the home menu."""
    from . import settings, web_config
    config.refresh()
    keys = (("PYCUT_SOURCE_DIR",) if key == "fission" else
            ("PYCUT_CLIP_DIR",) if key in ("cover", "dedup") else
            ("PYCUT_REFERENCE_DIR", "PYCUT_CLIP_DIR", "PYCUT_OUTPUT_DIR", "PYCUT_INTRO_DIR"))
    errors = settings.problems(settings.effective(), keys=keys)
    if errors:
        url = web_config.start()
        _flash(stdscr, "请先在网页配置：" + url + " · " + "；".join(errors), "bad")
        return None
    if key == "cover" and not config.key_ready():
        _flash(stdscr, "字幕定位需要配置视觉模型和密钥，请在顶部设置地址填写 LLM 网关", "bad")
        return None
    wizard = {"cover": _wizard_cover, "dedup": _wizard_dedup,
              "montage": _wizard_montage, "fission": _wizard_fission}.get(key)
    if wizard:
        try:
            return wizard(stdscr, w)
        except (ValueError, OSError) as exc:
            _flash(stdscr, f"无法读取素材目录：{exc}", "bad")
    return None


def _library_summary(paths: list[Path]) -> str:
    return "；".join(str(p) for p in paths) or "未配置"


def _wizard_cover(stdscr, w: int) -> list[list[str]] | None:
    from . import settings
    from .core.output_paths import without_outputs
    paths = config.CLIP_DIRS
    files = [p for p in list_videos_recursive(paths, max_depth=None, raise_on_error=True)
             if p.suffix.lower() == ".mp4"]
    files = without_outputs(files, paths, "字幕查重")
    if not files:
        _flash(stdscr, "混剪切片目录及其子目录没有 MP4：" + _library_summary(paths), "bad")
        return None
    if _confirm(stdscr, [("用户切片目录", _library_summary(paths)),
                        ("字幕处理", "单层毛玻璃遮盖字幕"), ("MP4 数量", str(len(files))),
                        ("输出", "各输入目录同级的字幕查重目录")]):
        return [["cover", "--src-dir", settings.encoded_directories(paths)]]
    return None


def _wizard_dedup(stdscr, w: int) -> list[list[str]] | None:
    from . import settings
    from .core.output_paths import without_outputs
    paths = config.CLIP_DIRS
    files = [p for p in list_videos_recursive(paths, max_depth=None, raise_on_error=True)
             if p.suffix.lower() == ".mp4"]
    files = without_outputs(files, paths, "去重")
    if not files:
        _flash(stdscr, "混剪切片目录及其子目录没有 MP4：" + _library_summary(paths), "bad")
        return None
    if _confirm(stdscr, [("用户切片目录", _library_summary(paths)), ("MP4 数量", str(len(files))),
                        ("输出", "各输入目录同级的去重目录")]):
        return [["dedup", "--src-dir", settings.encoded_directories(paths)]]
    return None


def _wizard_montage(stdscr, w: int) -> list[list[str]] | None:
    """Choose script-folder videos, then quantities and optional PIP."""
    from . import settings
    from .core.montage_clips import _collect_files, VIDEO_EXTS
    from .core.output_paths import montage_output, montage_batch
    clips = config.CLIP_DIRS
    pips = config.PIP_DIRS
    intros = config.INTRO_DIRS
    output = montage_output(config.REFERENCE_DIR)
    clip_files = {p.resolve() for root in clips for p in _collect_files(root)}
    if not clip_files:
        _flash(stdscr, "未找到混剪切片：" + _library_summary(clips)
               + "\n递归支持 " + "/".join(ext[1:].upper() for ext in VIDEO_EXTS), "bad")
        return None
    files = [p for p in list_videos_recursive(config.REFERENCE_DIR, max_depth=None, raise_on_error=True)
             if p.parent.resolve() != config.REFERENCE_DIR.resolve()]
    if not files:
        _flash(stdscr, "话术目录的子文件夹中没有视频：" + str(config.REFERENCE_DIR)
               + "\n例如：a/1.mp4 和 a/1.srt", "bad")
        return None
    values = [bool(pips)]
    selection = None
    step = 1
    while True:
        if step == 1:
            result = _multi_select(stdscr, "混剪 · 1/3 选择话术视频", files, initial=selection)
            if result is None:
                return None
            selection = result
            step = 2
        chosen, fission = selection
        if step == 2:
            if not _set_fission_counts(stdscr, chosen, fission):
                step = 1
                continue
            step = 3
        lines = [("混剪 · 3/3", f"{len(chosen)} 个话术，共 {sum(fission[str(f)] for f in chosen)} 支成片"),
                 ("混剪切片", f"{len(clips)} 个目录 · {len(clip_files)} 个视频"),
                 ("画中画目录", _library_summary(pips)),
                 ("前贴目录", _library_summary(intros) if intros else "未配置，跳过"),
                 ("规格", "720×1280 · 9:16"),
                 ("原声", "正文使用话术原声，前贴保留自身原声"),
                 ("输出", str(output) + "（本次新建批次目录，视频集中存放）")]
        lines += [(f"话术 {i}", f"{_reference_label(src)} × {fission[str(src)]}")
                  for i, src in enumerate(chosen, 1)]
        if not _confirm(stdscr, lines, [("叠加画中画", "关闭则不读取画中画素材", values[0])], values):
            step = 2
            continue
        if values[0]:
            errors = settings.problems(settings.effective(), keys=("PYCUT_PIP_DIR",))
            if errors or not pips or not any(_collect_files(p) for p in pips):
                _flash(stdscr, "画中画素材不可用，请添加目录或取消勾选：" + "；".join(errors), "bad")
                return None
        batch_dir = montage_batch(output)
        argv_list = []
        for src in chosen:
            argv = ["montage", "--source", str(src),
                    "--clip-dir", settings.encoded_directories(clips),
                    "--out-dir", str(output),
                    "--batch-dir", str(batch_dir),
                    "--intro-dir", settings.encoded_directories(intros),
                    "--fission", str(fission[str(src)])]
            if values[0]:
                argv.extend(["--dedup-dir", settings.encoded_directories(pips)])
            else:
                argv.append("--no-pip")
            argv_list.append(argv)
        return argv_list


def _wizard_fission(stdscr, w: int) -> list[list[str]] | None:
    """List all source videos, require individual counts, and retain edits on Back."""
    from .core.output_paths import sibling, without_outputs
    root = config.SRC_DIR.expanduser().resolve()
    output = sibling(root, "抖音裂变")
    files = list_videos_recursive(root, max_depth=None, raise_on_error=True)
    files = without_outputs(files, root, "抖音裂变")
    if not files:
        _flash(stdscr, "原视频目录及其子目录没有可处理的视频：" + str(root), "bad")
        return None
    counts = {}
    selected = 0
    while True:
        h, w = stdscr.getmaxyx()
        if _small_screen(stdscr):
            if stdscr.getch() in (27, ord("q"), ord("Q")):
                return None
            continue
        stdscr.erase()
        body = _frame(stdscr, "抖音裂变 · 1/2 设置每个视频的数量",
                      f"共 {len(files)} 个原视频 · 已设置 {len(counts)} 个 · 数量 0～99，0 表示跳过")
        rows = max(1, h - body - 3)
        top = max(0, selected - rows + 1)
        for i in range(top, min(len(files) + 1, top + rows)):
            if i == len(files):
                text = "下一步：确认生成"
            else:
                label = str(files[i].relative_to(root))
                number = str(counts[files[i]]) if files[i] in counts else "未设置"
                text = _ellipsize(label, w - 22) + f"  × {number}"
            _safe_add(stdscr, body + i - top, 2, ("❯ " if i == selected else "  ") + text,
                      w, "accent" if i == selected else "text")
        _footer(stdscr, "↑↓ 移动 · →/Enter 设置数量或进入确认 · Esc 返回",
                "↑↓移动 · Enter 设置/下一步 · Esc 返回")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (27, curses.KEY_LEFT, ord("q"), ord("Q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(files), selected + 1)
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT, ord("l")):
            if selected < len(files):
                source = files[selected]
                number = _prompt_int(stdscr, h - 4, 2, w, "生成数量（0 跳过）",
                                     counts.get(source, 1), 0, 99)
                if number is not None:
                    counts[source] = number
                    selected += 1
                continue
            if len(counts) != len(files):
                _flash(stdscr, "请为每个原视频设置数量；不需要生成的视频填 0", "warn")
                continue
            chosen = [source for source in files if counts[source]]
            if not chosen:
                _flash(stdscr, "请至少为一个视频设置大于 0 的生成数量", "warn")
                continue
            lines = [("抖音裂变 · 2/2", f"{len(chosen)} 个原视频，共生成 {sum(counts.values())} 个副本"),
                     ("处理方式", "仅在副本尾部追加 50～500KB 随机字节"),
                     ("原视频目录", str(root)), ("输出目录", str(output))]
            lines += [(str(source.relative_to(root)), f"{counts[source]} 个副本") for source in chosen]
            if _confirm(stdscr, lines):
                return [["fission", "--file", str(source), "--copies", str(counts[source]),
                         "--out-dir", str(output / source.relative_to(root).parent)]
                        for source in chosen]


def _flash(stdscr, msg: str, color: str = "bad") -> None:
    h, w = stdscr.getmaxyx()
    header = _brand(stdscr)
    lines = []
    for paragraph in msg.splitlines():
        while _disp_width(paragraph) > max(2, w - 6):
            part = _clip(paragraph, max(2, w - 6))
            lines.append(part)
            paragraph = paragraph[len(part):]
        lines.append(paragraph)
    lines = lines[:max(1, h - header - 2)]
    for row, line in enumerate(lines, h - 2 - len(lines)):
        _safe_add(stdscr, row, 2, " " * (w - 4), w, color, curses.A_REVERSE)
        _safe_add(stdscr, row, 2, " " + line, w, color, curses.A_REVERSE)
    _footer(stdscr, "按任意键继续", "按任意键继续")
    stdscr.refresh()
    stdscr.getch()


def run() -> int:
    """Run interactive tasks, return to the menu, and retain nonzero task exit codes."""
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
        # curses.wrapper restores terminal state; report application failures with a nonzero exit code.
        print(f"[TUI 运行失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        web_config.stop()


_loop._feat_sel = 0
