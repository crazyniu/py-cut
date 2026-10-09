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
        if isinstance(a, argparse._StoreTrueAction):
            kind, val = "flag", bool(a.default)
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
        })
    return fields


def _build_argv(cmd: str, fields: list[dict]) -> list[str]:
    """把表单状态转成 argv（只带与默认不同的项，保持干净）。"""
    argv = [cmd]
    for f in fields:
        v = f["value"]
        if v is None:
            continue
        if f["kind"] == "flag":
            if v:
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


def _loop(stdscr) -> tuple[str, list[str] | None]:
    curses.curs_set(0)
    stdscr.keypad(True)
    cmds = list(cli.COMMANDS.keys())

    phase = "menu"
    sel = 0
    cmd: str | None = None
    fields: list[dict] | None = None
    fidx = 0

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
            assert fields is not None and cmd is not None
            _safe_add(stdscr, 0, 0, f" pycut · {cmd} 参数   "
                                    f"(↑/↓ 移动, Enter 编辑/切换, R 运行, Q 返回)", w)
            for i, f in enumerate(fields):
                y = 2 + i
                if y >= h - 2:
                    break
                mark = "▶ " if i == fidx else "  "
                if f["kind"] == "flag":
                    val = "[x]" if f["value"] else "[ ]"
                elif f["kind"] == "choice":
                    val = str(f["value"])
                elif f["kind"] == "list":
                    val = str(f["value"]) if f["value"] else "(空)"
                else:
                    val = str(f["value"])
                _safe_add(stdscr, y, 2, f"{mark}{f['label']:<14} {val}", w)
            cur = fields[fidx]
            help_line = cur["help"] or ""
            _safe_add(stdscr, h - 1, 0, " " + help_line, w)

        stdscr.refresh()
        k = stdscr.getch()

        if k in (27,):  # Esc
            if phase == "form":
                phase = "menu"
            else:
                return ("quit", None)
        elif k in (ord("q"), ord("Q")):
            if phase == "form":
                phase = "menu"
            else:
                return ("quit", None)
        elif phase == "menu":
            if k in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(cmds)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(cmds)
            elif k in (10, curses.KEY_ENTER, ord(" "), ord("\r")):
                cmd = cmds[sel]
                fields = _collect_params(cmd)
                fidx = 0
                phase = "form"
        else:  # form
            assert fields is not None
            if k in (curses.KEY_UP, ord("k")):
                fidx = (fidx - 1) % len(fields)
            elif k in (curses.KEY_DOWN, ord("j")):
                fidx = (fidx + 1) % len(fields)
            elif k in (10, curses.KEY_ENTER, ord(" "), ord("\r")):
                f = fields[fidx]
                if f["kind"] == "flag":
                    f["value"] = not f["value"]
                elif f["kind"] == "choice":
                    idx = f["choices"].index(f["value"]) if f["value"] in f["choices"] else -1
                    f["value"] = f["choices"][(idx + 1) % len(f["choices"])]
                else:
                    new = _edit_string(stdscr, f["label"], str(f["value"]))
                    if new is not None:
                        f["value"] = new
            elif k in (ord("r"), ord("R")):
                assert cmd is not None
                return ("run", _build_argv(cmd, fields))

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
