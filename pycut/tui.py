"""TUI 启动器：终端交互式选择命令与编辑参数，生成 argv 后交给 cli.main 执行。

设计：
  - 命令与参数定义不重复：直接从 cli.build_parser() 的子解析器读取每个参数
    （option_strings / type / default / choices / help），CLI 与 TUI 共用同一份定义。
  - 用户在终端里用方向键选命令、编辑参数；回车运行，结束后回到 shell 看日志。
  - 任何异常或非交互环境（管道 / 无 tty）自动回退到 CLI 帮助，绝不卡死。
"""
from __future__ import annotations

import argparse
import curses
import curses.textpad  # noqa: F401  必须显式导入：curses.textpad 不会随 import curses 自动加载
import sys

from . import cli

# 命令菜单展示文案（与 commands 的 help 保持一致即可）
CMD_META = {
    "slice": "切片：场景分段 → 人脸分流 → 字幕擦除",
    "cover": "字幕擦除：对已有切片单独重跑去字幕",
}

# 「任务设置」页只放该命令真正需要用户决定的参数（按此顺序展示）；
# 其余全部归入「高级参数」页（按 A 切换），LLM / 并发 / 阈值等日常不用管。
BASIC_PARAMS = {
    "slice": ["--file", "--no-sub", "--dry-run", "--force"],
    "cover": ["--file", "--all", "--force", "--dry-run"],
}

# 参数在 TUI 里的中文显示名（argv 仍用原始 flag）
DISPLAY = {
    "--file": "指定视频（留空=全部）",
    "--all": "处理全部切片",
    "--no-sub": "切片时擦除字幕",
    "--dry-run": "试跑（不产出）",
    "--force": "重跑（忽略标记）",
    "--limit": "前 N 个视频",
    "--scene-th": "场景阈值",
    "--min-seg": "最短段落(秒)",
    "--workers-llm": "LLM 并发",
    "--workers-enc": "编码并发",
    "--samples": "每片抽帧数",
    "--workers": "并发片段数",
    "--no-llm": "启用 LLM",
    "--no-fallback": "故障自动切备用",
    "--gateway": "网关",
    "--llm-url": "网关地址(覆盖)",
    "--llm-key": "网关密钥(覆盖)",
    "--llm-model": "模型名(覆盖)",
}

# 反向开关：界面显示正向含义（勾选=开启），实际写入的是 --no-xxx
INVERT_FLAGS = {"--no-sub", "--no-llm", "--no-fallback"}


def _get_subparser(parser, cmd: str):
    """兼容不同 argparse 版本，取出某个子命令的 ArgumentParser。"""
    sp = getattr(parser, "_subparsers", None)
    candidates = []
    if sp is not None:
        candidates.extend(getattr(sp, "_group_actions", [sp]))
    for a in parser._actions:
        candidates.append(a)
    for a in candidates:
        choices = getattr(a, "choices", None)
        if choices and cmd in choices:
            return choices[cmd]
    return None


def _collect_params(cmd: str) -> list[dict]:
    """从子解析器提取参数表单（与 argparse 定义同源）。"""
    parser = cli.build_parser()
    sub = _get_subparser(parser, cmd)
    if sub is None:
        return []
    fields: list[dict] = []
    for a in sub._actions:
        if not a.option_strings:
            continue
        if a.dest in ("help", "command", "func"):
            continue
        long = next((o for o in a.option_strings if o.startswith("--")),
                   a.option_strings[0])
        invert = long in INVERT_FLAGS
        if isinstance(a, argparse._StoreTrueAction):
            kind, val = "flag", bool(a.default)
            if invert:                      # 界面显示正向含义：勾选=开启
                val = not val
        elif getattr(a, "nargs", None) in ("+", "*"):
            kind, val = "list", (a.default if a.default else "")
        elif a.choices:
            kind, val = "choice", a.default
        else:
            kind, val = "value", (a.default if a.default is not None else "")
        fields.append({
            "key": a.dest, "label": long, "kind": kind, "value": val,
            "choices": list(a.choices) if a.choices else None,
            "help": a.help or "", "default": a.default,
            "display": DISPLAY.get(long, long), "invert": invert,
        })
    # 任务设置页按 BASIC_PARAMS 的顺序展示，其余进高级页
    order = [p for p in BASIC_PARAMS.get(cmd, [])]
    rank = {p: i for i, p in enumerate(order)}
    fields.sort(key=lambda f: rank.get(f["label"], len(order)))
    return fields


def _build_argv(cmd: str, fields: list[dict]) -> list[str]:
    """把表单状态转成 argv（只带与默认不同的项，保持干净）。"""
    argv = [cmd]
    for f in fields:
        v = f["value"]
        if v is None:
            continue
        if f["kind"] == "flag":
            raw = (not v) if f.get("invert") else v   # 反向开关换算回原始 flag
            if raw != bool(f["default"]):
                argv.append(f["label"])
        elif f["kind"] == "list":
            s = str(v).strip()
            if s:
                argv += [f["label"], s]
        else:
            s = str(v).strip()
            if s == "":
                continue
            if f["default"] is not None and s == str(f["default"]):
                continue
            argv += [f["label"], s]
    return argv


def _edit_string(stdscr, label: str, initial: str) -> str | None:
    """在底部开一个空输入框（curses.textpad），回车返回新值；留空则保持原值。

    不预填原值：避免用户直接输入时把新值追加到旧值后面（0.3 + 0.35 -> 0.30.35）。
    当前值显示在提示行，用户直接输入新值即可。
    """
    h, w = stdscr.getmaxyx()
    if h < 4 or w < 12:
        return None
    _safe_add(stdscr, h - 1, 0,
              f" 编辑 {label}（当前 {initial}）：直接输入新值，回车确认；留空=不改 ", w)
    stdscr.refresh()

    win = curses.newwin(1, max(10, w - 4), h - 2, 2)
    win.erase()
    win.keypad(True)
    win.refresh()
    curses.curs_set(1)
    box = curses.textpad.Textbox(win)
    try:
        s = box.edit()
    except Exception:  # noqa: BLE001
        s = ""
    finally:
        curses.curs_set(0)
        try:            # 清掉输入框残留，避免回到参数表后底部仍显示旧输入
            win.erase()
            win.refresh()
        except curses.error:
            pass
    return s.strip() or None


def _safe_add(stdscr, y: int, x: int, s: str, maxw: int) -> None:
    """按可用宽度安全写入；终端太小时静默跳过，避免 curses.error 中断 TUI。"""
    if y < 0 or maxw <= 1:
        return
    s = s[: maxw - x - 1] if x + len(s) > maxw - 1 else s
    if not s:
        return
    try:
        stdscr.addstr(y, x, s)
    except curses.error:
        pass


def _finalize_argv(cmd: str, fields: list[dict]) -> list[str]:
    """生成将要执行的 argv：在 _build_argv 基础上补上命令的隐式默认。"""
    argv = _build_argv(cmd, fields)
    # cover 没给 --file 时，等同于「处理全部切片」，避免直接跑报参数缺失
    if cmd == "cover" and "--file" not in argv and "--all" not in argv:
        argv.append("--all")
    return argv


def _disp_width(s: str) -> int:
    """终端显示宽度：CJK / 全角字符占 2 列。"""
    import unicodedata  # noqa: PLC0415
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """按显示宽度右补空格，保证中英混排时数值列对齐。"""
    return s + " " * max(1, width - _disp_width(s))


def _val_text(f: dict) -> str:
    """参数当前值的展示文本。"""
    if f["kind"] == "flag":
        return "[x]" if f["value"] else "[ ]"
    s = str(f["value"])
    if f["kind"] in ("list", "value"):
        return s if s.strip() else "(空)"
    return s


def _loop(stdscr) -> tuple[str, list[str] | None]:
    curses.curs_set(0)
    stdscr.keypad(True)
    cmds = list(cli.COMMANDS.keys())

    phase = "menu"
    sel = 0
    cmd: str | None = None
    basic: list[dict] = []
    adv: list[dict] = []
    page = "basic"     # basic=任务设置页 / advanced=高级参数页
    fidx = 0

    def fields() -> list[dict]:
        return basic if page == "basic" else adv

    def n_items() -> int:
        return len(fields()) + (1 if page == "basic" else 0)

    def do_run() -> tuple[str, list[str]]:
        assert cmd is not None
        return ("run", _finalize_argv(cmd, basic + adv))

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if phase == "menu":
            _safe_add(stdscr, 0, 0, " pycut · 选择功能   (↑/↓ 移动, Enter 进入, Q 退出)", w)
            for i, c in enumerate(cmds):
                mark = "▶ " if i == sel else "  "
                _safe_add(stdscr, 2 + i, 2, f"{mark}{c:<8} {CMD_META.get(c, '')}", w)
            _safe_add(stdscr, h - 1, 0, " ↑/↓ 移动   Enter 进入   Q 退出 ", w)
        else:
            fs = fields()
            title = ("任务设置" if page == "basic" else "高级参数")
            _safe_add(stdscr, 0, 0, f" pycut · {cmd} · {title}", w)
            _safe_add(stdscr, 1, 2, "↑/↓ 移动 · Enter 编辑/切换 · A 高级参数 · R 运行 · Q 返回", w)
            for i, f in enumerate(fs):
                y = 3 + i
                if y >= h - 3:
                    break
                mark = "▶ " if i == fidx else "  "
                _safe_add(stdscr, y, 2, f"{mark}{_pad(f['display'], 20)} {_val_text(f)}", w)
            if page == "basic":     # 任务设置页末尾：开始执行
                y = 3 + len(fs)
                mark = "▶ " if fidx == len(fs) else "  "
                _safe_add(stdscr, y, 2, f"{mark}▶▶ 开始执行 {cmd}", w)
            # 底部：将执行的命令预览 + 当前项说明
            assert cmd is not None
            _safe_add(stdscr, h - 2, 0, " 将执行: " + " ".join(_finalize_argv(cmd, basic + adv)), w)
            if fidx < len(fs):
                tip = fs[fidx]["help"] or ""
            else:
                tip = "回车立即开始执行（也可按 R）"
            _safe_add(stdscr, h - 1, 0, " " + tip, w)

        stdscr.refresh()
        k = stdscr.getch()

        if phase == "menu":
            if k in (27, ord("q"), ord("Q")):
                return ("quit", None)
            if k in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(cmds)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(cmds)
            elif k in (10, curses.KEY_ENTER, ord(" "), ord("\r")):
                cmd = cmds[sel]
                allf = _collect_params(cmd)
                nb = len(BASIC_PARAMS.get(cmd, []))
                basic, adv = allf[:nb], allf[nb:]
                page, fidx = "basic", 0
                phase = "form"
            continue

        # ---- form ----
        assert cmd is not None
        fs = fields()
        if k in (27, ord("q"), ord("Q")):
            if page == "advanced":
                page, fidx = "basic", 0
            else:
                phase, page, fidx = "menu", "basic", 0
        elif k in (curses.KEY_UP, ord("k")):
            fidx = (fidx - 1) % n_items()
        elif k in (curses.KEY_DOWN, ord("j")):
            fidx = (fidx + 1) % n_items()
        elif k in (ord("a"), ord("A")):
            if adv:
                page = "advanced" if page == "basic" else "basic"
                fidx = 0
        elif k in (10, curses.KEY_ENTER, ord(" "), ord("\r")):
            if page == "basic" and fidx == len(fs):
                return do_run()
            f = fs[fidx]
            if f["kind"] == "flag":
                f["value"] = not f["value"]
            elif f["kind"] == "choice":
                idx = f["choices"].index(f["value"]) if f["value"] in f["choices"] else -1
                f["value"] = f["choices"][(idx + 1) % len(f["choices"])]
            else:
                new = _edit_string(stdscr, f["display"], str(f["value"]))
                if new is not None:
                    f["value"] = new
        elif k in (ord("r"), ord("R")):
            return do_run()

    return ("quit", None)


def run() -> int:
    """入口：非交互环境回退 CLI 帮助；否则启动 curses TUI。"""
    if not sys.stdin.isatty():
        cli.build_parser().print_help()
        return 0
    try:
        stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        try:
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
        _, argv = result
        return cli.main(argv)
    return 0
