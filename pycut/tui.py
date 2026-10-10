"""pycut 交互式终端界面（curses TUI）—— 引导式向导。

风格取向：Claude Code 观感——深色底、琥珀色强调、顶部 logo+版本号、底部状态栏、
❯ 选中标记、语法高亮。

交互模型（引导式向导，替代旧的「参数表单」）：
    主菜单列出 5 个功能，选中后进入**分步向导**，每步只问一个决定：

      切片        → 选源目录 → 报视频数 → 选「擦除字幕/去重」开关 → 确认执行
      字幕擦除    → 选目录 → 报视频数 → 确认执行（只有擦除，无去重）
      去重        → 选目录 → 报视频数 → 确认执行（只有去重）
      混剪        → 选片段目录 → 选画中画目录 → 选爆款视频目录 →
                    多选爆款视频 + 每个裂变数量 → 选「去重/字幕」开关 → 确认执行

关键机制：
  - **目录选择器**：TUI 列表展示候选目录（含 config 里的常见目录 + 任意子目录），
    支持进入子目录 / 返回上级 / 直接选当前目录，不要求手敲路径。
  - **递归扫描**：选定目录后用 `core/scan` 递归统计视频数并展示。
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
from .core.scan import count_videos_recursive, list_videos_flat

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


def _init_colors() -> None:
    global _LIGHT_BG
    if not curses.has_colors():
        return
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    _LIGHT_BG = _detect_light_bg()
    rich = curses.COLORS >= 256
    for name, (cname, fg, _bg) in _COLOR_DEFS.items():
        idx = 1 + list(_COLOR_DEFS).index(name)
        use_fg = fg if rich else _FALLBACK_8[name]
        if _LIGHT_BG and name in ("text", "keyword", "value", "info", "good"):
            use_fg = 0 if name != "keyword" else 5
        try:
            curses.init_pair(idx, use_fg, -1)
        except curses.error:
            pass


def _detect_light_bg() -> bool:
    try:
        if curses.COLORS >= 256 and curses.can_change_color():
            r, g, b = curses.color_content(curses.COLOR_BLACK)
            return (r + g + b) / 3 > 40000
    except curses.error:
        pass
    return False


def _attr(name: str) -> int:
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


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------
def _disp_width(s: str) -> int:
    import unicodedata  # noqa: PLC0415
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(1, width - _disp_width(s))


def _safe_add(stdscr, y, x, s, maxw, color="text", attrs=0):
    if y < 0 or maxw <= 1 or x >= maxw - 1:
        return
    s = s[: maxw - x - 1] if x + _disp_width(s) > maxw - 1 else s
    if not s:
        return
    try:
        stdscr.addstr(y, x, s, _attr(color) | attrs)
    except curses.error:
        pass


# ---------------------------------------------------------------------------
# 输入框（数字 / 简单文本，带默认值）
# ---------------------------------------------------------------------------
def _prompt_int(stdscr, y, x, w, title, default, lo, hi) -> int | None:
    """输入一个整数，返回 None 表示取消。"""
    buf = str(default)
    while True:
        stdscr.move(y, 0)
        stdscr.clrtoeol()
        _safe_add(stdscr, y, x, f"{title} [{lo}~{hi}]：", w, "text")
        body = buf
        _safe_add(stdscr, y, x + _disp_width(f"{title} [{lo}~{hi}]：") + 1, body, "value",
                  curses.A_REVERSE)
        _safe_add(stdscr, y + 1, x, "Enter 确认 · Esc 取消", w, "dim")
        stdscr.refresh()
        k = stdscr.getch()
        if k in (10, curses.KEY_ENTER, ord("\r")):
            try:
                v = int(buf)
            except ValueError:
                continue
            return max(lo, min(hi, v))
        if k in (27,):
            return None
        if k in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif ord("0") <= k <= ord("9"):
            buf += chr(k)


# ---------------------------------------------------------------------------
# 目录选择器（文件系统层级浏览器）
# ---------------------------------------------------------------------------
def _pick_dir(stdscr, title: str, start: Path | None = None) -> str | None:
    """目录选择器：从根目录出发逐级浏览文件系统，选定任意目录。

    交互：
      ↑/↓ 或 j/k   在「子目录列表」中移动
      Enter        进入选中的子目录；在「..」或「选择当前目录」上则选定
      ←/Backspace  返回上一级目录
      Esc          取消（返回 None）
      ~            直接跳到用户主目录；/ 跳到根目录

    列表第一项固定为「📁 选择当前目录」（直接选定当前所在目录），
    第二项固定为「📁 ..」（返回上一级），之后才是当前目录的子目录。
    从根目录 / 开始，可一路浏览到任意位置，不再依赖预置候选。
    """
    # 起点：默认根目录；若有 start 则从那里开始
    cur = Path(start).expanduser() if start else Path("/")

    def _entries() -> list[tuple[str, Path]]:
        """返回当前目录下的可选项：(显示名, 目标路径)。"""
        out: list[tuple[str, Path]] = []
        out.append(("📁  选择当前目录", cur))
        if cur != cur.parent:
            out.append(("📁  ..  返回上一级", cur.parent))
        try:
            names = sorted(
                (e.name for e in os.scandir(str(cur)) if e.is_dir()),
                key=lambda n: n.lower(),
            )
        except OSError:
            names = []
        for n in names:
            out.append((f"📂  {n}", cur / n))
        return out

    entries = _entries()
    sel = 0
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        # 标题 + 当前路径（面包屑）
        _safe_add(stdscr, 2, 2, title, w, "keyword", curses.A_BOLD)
        _safe_add(stdscr, 3, 2, str(cur), w, "info")
        _safe_add(stdscr, 4, 2, "─" * (w - 4), w, "muted")

        if sel >= len(entries):
            sel = max(0, len(entries) - 1)
        start_y = 5
        max_rows = h - 9
        top = 0
        if sel >= max_rows:
            top = sel - max_rows + 1
        for i in range(top, min(len(entries), top + max_rows)):
            label, _path = entries[i]
            y = start_y + (i - top)
            active = (i == sel)
            mark = "❯" if active else " "
            # 过长目录名：截断，保留末尾
            maxlen = w - 8
            if _disp_width(label) > maxlen:
                label = "…" + label[-(maxlen - 1):]
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "text"
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, label, w, fg, attr)

        _safe_add(stdscr, h - 2, 2,
                  " ↑/↓ 移动 · Enter 进入/选定 · ← 返回上级 · ~ 主目录 · / 根目录 · Esc 取消 ",
                  w, "dim", curses.A_REVERSE)
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
        elif k in (curses.KEY_LEFT, 8, 127, curses.KEY_BACKSPACE):
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
             toggles: list[tuple[str, str, bool]] | None = None) -> bool:
    """展示一段汇报 + 若干开关，返回是否确认执行。

    lines: [(label, value, color?)] 展示项。
    toggles: [(label, 说明, 初始bool)] 可切换开关（空格切换）。
    """
    toggle_vals = [t[2] for t in toggles] if toggles else []
    sel = 0  # 0..len(toggles)-1 是开关，最后一项是「开始执行」
    n_items = (len(toggles) if toggles else 0) + 1

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _safe_add(stdscr, 2, 2, "处理前确认", w, "keyword", curses.A_BOLD)
        _safe_add(stdscr, 3, 2, "─" * (w - 4), w, "muted")
        y = 4
        for label, value in lines:
            _safe_add(stdscr, y, 2, _pad(label, 16), w, "dim")
            _safe_add(stdscr, y, 2 + 16, value, w, "value")
            y += 1
        if toggles:
            y += 1
            _safe_add(stdscr, y, 2, "功能开关（空格切换）：", w, "dim")
            y += 1
            for i, (label, desc, _v) in enumerate(toggles):
                active = (sel == i)
                mark = "❯" if active else " "
                state = "● 开" if toggle_vals[i] else "○ 关"
                sc = "good" if toggle_vals[i] else "muted"
                attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
                _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
                _safe_add(stdscr, y, 5, _pad(label, 14), w, "text", attr)
                _safe_add(stdscr, y, 5 + 14, state, w, sc, attr)
                _safe_add(stdscr, y, 5 + 14 + _disp_width(state) + 1, desc, w, "dim", attr)
                y += 1
            y += 1
        # 开始执行
        run_idx = len(toggles) if toggles else 0
        active = (sel == run_idx)
        attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
        _safe_add(stdscr, y, 3, "❯" if active else " ", w, "good" if active else "muted", attr)
        _safe_add(stdscr, y, 5, "▶▶ 确认并开始处理", w, "good", attr)
        _safe_add(stdscr, h - 2, 2, " ↑/↓ 移动 · 空格 切换 · Enter 确认 · Esc 返回 ", w,
                  "dim", curses.A_REVERSE)
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return False
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % n_items
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % n_items
        elif k == ord(" "):
            if toggles and sel < len(toggles):
                toggle_vals[sel] = not toggle_vals[sel]
        elif k in (10, curses.KEY_ENTER, ord("\r")):
            if sel == run_idx:
                return True
            # 在开关上按 Enter 也切换
            if toggles and sel < len(toggles):
                toggle_vals[sel] = not toggle_vals[sel]


# ---------------------------------------------------------------------------
# 多选列表（混剪爆款视频）
# ---------------------------------------------------------------------------
def _multi_select(stdscr, title: str, files: list[Path]) -> tuple[list[Path], dict[str, int]] | None:
    """多选视频列表 + 每个的裂变数量。返回 (选中列表, {路径: 裂变数})；Esc 返回 None。

    空格勾选/取消；选中项上按数字键 0-9 设置裂变数量（默认 1）；A 全选/取消全选。
    """
    selected: set[int] = set()
    fission: dict[str, int] = {str(f): 1 for f in files}
    sel = 0

    def refresh_view():
        pass

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _safe_add(stdscr, 2, 2, title, w, "keyword", curses.A_BOLD)
        _safe_add(stdscr, 3, 2, f"共 {len(files)} 个视频，已选 {len(selected)} 个", w, "dim")
        _safe_add(stdscr, 4, 2, "─" * (w - 4), w, "muted")

        max_rows = h - 8
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
            maxlen = w - 24
            if _disp_width(name) > maxlen:
                name = name[:maxlen - 1] + "…"
            _safe_add(stdscr, y, 3, mark, w, "accent" if active else "muted", attr)
            _safe_add(stdscr, y, 5, box, w, bcolor, attr)
            _safe_add(stdscr, y, 5 + 4, name, w, "text", attr)
            n = fission.get(str(f), 1)
            _safe_add(stdscr, y, w - 12, f"裂变 x{n}", w, "value", attr)

        _safe_add(stdscr, h - 2, 2,
                  " ↑/↓ 移动 · 空格 勾选 · 数字 设裂变数 · A 全选/取消 · Enter 确认 · Esc 返回 ",
                  w, "dim", curses.A_REVERSE)
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
        elif ord("0") <= k <= ord("9"):
            n = k - ord("0")
            fission[str(files[sel])] = n if n >= 1 else 1
        elif k in (10, curses.KEY_ENTER, ord("\r")):
            if not selected:
                continue
            chosen = [files[i] for i in sorted(selected)]
            return chosen, {str(f): fission[str(f)] for f in chosen}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _loop(stdscr) -> tuple[str, list[list[str]]] | None:
    curses.curs_set(0)
    stdscr.keypad(True)

    def draw_header(y: int, w: int) -> None:
        # 顶部栏：logo + 版本号
        _safe_add(stdscr, y, 0, " " * w, w, "text", curses.A_REVERSE)
        _safe_add(stdscr, y, 1, "  pycut ", w, "accent", curses.A_REVERSE | curses.A_BOLD)
        _safe_add(stdscr, y, 11, "短视频智能处理工具箱", w, "dim", curses.A_REVERSE)
        ver = f"v{__version__}"
        _safe_add(stdscr, y, w - len(ver) - 2, ver, w, "muted", curses.A_REVERSE)

    def draw_footer(y: int, hints: str, w: int) -> None:
        _safe_add(stdscr, y, 0, " " * w, w, "text", curses.A_REVERSE)
        _safe_add(stdscr, y, 1, hints, w, "dim", curses.A_REVERSE)

    feat_sel = getattr(_loop, "_feat_sel", 0)  # 选中项只初始化一次，循环内保持
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        draw_header(0, w)

        # ---- 主菜单 ----
        _safe_add(stdscr, 2, 2, "选择功能", w, "keyword", curses.A_BOLD)
        _safe_add(stdscr, 3, 2, "─" * (w - 4), w, "muted")
        sel = feat_sel
        for i, (key, name, desc) in enumerate(FEATURES):
            y = 4 + i
            active = (i == sel)
            mark = "❯" if active else " "
            attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_NORMAL
            fg = "accent" if active else "keyword"
            _safe_add(stdscr, y, 3, mark, w, fg if active else "muted", attr)
            _safe_add(stdscr, y, 5, _pad(name, 10), w, fg, attr)
            _safe_add(stdscr, y, 16, desc, w, "dim", attr)
        draw_footer(h - 1, " ↑/↓ 移动 · Enter 进入 · Q 退出 ", w)
        stdscr.refresh()
        k = stdscr.getch()
        if k in (27, ord("q"), ord("Q")):
            return ("quit", None)
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(FEATURES)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(FEATURES)
        elif k in (10, curses.KEY_ENTER, ord("\r"), ord(" ")):
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
    return None


# ---------------------------------------------------------------------------
# 各功能向导
# ---------------------------------------------------------------------------
def _wizard_slice(stdscr, w: int) -> list[list[str]] | None:
    d = _pick_dir(stdscr, "切片 · 选择源视频目录", None)
    if d is None:
        return None
    n = count_videos_recursive(d)
    if n == 0:
        _flash(stdscr, f"该目录下没有视频：{d}", "bad")
        return None
    toggles = [
        ("擦除字幕", "切片时顺带擦除画面字幕", True),
        ("去重", "切片后过平台查重", False),
    ]
    ok = _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")], toggles)
    if not ok:
        return None
    sub_on = toggles[0][2]
    # 切片里「擦除字幕」默认开启（--no-sub 才是关闭）；去重由 slice 流水线内置，
    # 无需单独开关（切片去重能力走 dedup 命令，这里只控制字幕擦除）
    argv = ["slice", "--src-dir", d]
    if not sub_on:
        argv.append("--no-sub")
    return [argv]


def _wizard_cover(stdscr, w: int) -> list[list[str]] | None:
    d = _pick_dir(stdscr, "字幕擦除 · 选择视频目录", None)
    if d is None:
        return None
    n = count_videos_recursive(d)
    if n == 0:
        _flash(stdscr, f"该目录下没有视频：{d}", "bad")
        return None
    ok = _confirm(stdscr, [("源目录", d), ("视频数量", f"{n} 个")])
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
    chosen, fission = _multi_select(stdscr, "混剪 · 多选爆款视频并设裂变数量", files)
    if chosen is None or not chosen:
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


def _flash(stdscr, msg: str, color: str = "bad") -> None:
    h, w = stdscr.getmaxyx()
    _safe_add(stdscr, h - 3, 2, " " * (w - 4), w, color, curses.A_REVERSE)
    _safe_add(stdscr, h - 3, 2, " " + msg, w, color, curses.A_REVERSE)
    _safe_add(stdscr, h - 2, 2, "按任意键继续", w, "dim", curses.A_REVERSE)
    stdscr.refresh()
    stdscr.getch()


def run() -> int:
    """入口：非交互回退 CLI 帮助；否则启动 curses TUI。"""
    if not sys.stdin.isatty():
        cli.build_parser().print_help()
        return 0
    try:
        stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        try:
            curses.start_color()
            _init_colors()
            result = _loop(stdscr)
        finally:
            curses.echo()
            curses.nocbreak()
            curses.endwin()
    except Exception as e:  # noqa: BLE001
        try:
            curses.endwin()
        except Exception:  # noqa: BLE001
            pass
        print(f"[TUI 启动失败] {e}\n回退：python3 pycut.py <command> --help")
        return 0

    if result and result[0] == "run":
        _, argv_list = result
        # 多支成片（混剪多选爆款视频）= 多个 argv，逐条执行
        final_code = 0
        for argv in argv_list:
            code = cli.main(argv)
            if code:
                final_code = code
        return final_code
    return 0


# _loop 的选中记忆（静态属性）
_loop._feat_sel = 0
