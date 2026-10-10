"""终端任务运行器：子进程运行 CLI，curses 主线程只负责渲染与键盘输入。

使用已有 CLI/ffmpeg 处理逻辑，不建立第二套参数表。处理失败立即阻断同一向导
中依赖的后续子命令（例如 slice 失败不能对旧的切片结果执行 dedup）。
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

from . import tui

_ROOT = Path(__file__).resolve().parent.parent
_CONTROL = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _sanitize(line: str) -> str:
    """不可把子进程的控制序列原样写入 curses 画布。"""
    return "".join(c for c in _CONTROL.sub("", line) if c.isprintable() or c == "\t")


def _display_command(argv: list[str]) -> str:
    """隐藏未来可能从高级参数传入的 API Key。"""
    out = []
    secret = False
    for arg in argv:
        if secret:
            out.append("********")
            secret = False
        elif arg in ("--llm-key", "--api-key"):
            out.append(arg)
            secret = True
        elif arg.startswith(("--llm-key=", "--api-key=")):
            out.append(arg.split("=", 1)[0] + "=********")
        else:
            out.append(arg)
    return " ".join(out)


def _draw(stdscr, argv: list[str], index: int, total: int,
          history: deque[str], offset: int, status: str) -> None:
    import curses  # noqa: PLC0415

    h, w = stdscr.getmaxyx()
    if tui._small_screen(stdscr):
        return
    stdscr.erase()
    tui._frame(stdscr, "正在处理" if status == "running" else "处理结果",
               f"任务 {index}/{total} · {_display_command(argv)}")
    color = ("info" if status == "running" else "warn" if status == "stopping"
             else "good" if status == "success" else "bad")
    status_label = {"running": "● 运行中", "stopping": "■ 正在中断（等待清理）",
                    "success": "✓ 已完成", "error": "× 执行失败",
                    "cancelled": "■ 已中断"}.get(status, status)
    tui._safe_add(stdscr, 5, 2, status_label, w, color, curses.A_BOLD)
    tui._safe_add(stdscr, 6, 2, "最近日志  /  ↑↓、PgUp、PgDn 滚动", w, "dim")
    rows = max(1, h - 10)
    log = list(history)
    start = max(0, len(log) - rows - offset)
    for i, line in enumerate(log[start:start + rows]):
        tui._safe_add(stdscr, 7 + i, 2, line, w, "dim" if line.startswith("  ") else "text")
    footer = ("X 中止处理 · ↑↓ 滚动 · Ctrl+C 中断"
              if status == "running" else
              "正在等待子进程安全停止…" if status == "stopping" else
              "Enter 返回主菜单 · Q 退出 · ↑↓ 滚动")
    tui._safe_add(stdscr, h - 2, 2, footer, w, "dim", curses.A_REVERSE)
    stdscr.refresh()


def _interrupt(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGINT)
        else:
            proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass


def _terminate(proc: subprocess.Popen, *, force: bool = False) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


def _run_one(stdscr, argv: list[str], index: int, total: int,
             history: deque[str]) -> tuple[int, bool]:
    """在新的进程组中执行 CLI；只由主线程操作 curses。"""
    cmd = [sys.executable, str(_ROOT / "pycut.py"), *argv]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, start_new_session=(os.name != "nt"),
        )
    except OSError as e:
        history.append(f"[无法启动] {type(e).__name__}: {e}")
        return 2, False

    events: queue.Queue[str] = queue.Queue(maxsize=2048)

    def _reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            events.put(_sanitize(line.rstrip("\r\n")))
        proc.stdout.close()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    offset = 0
    interrupted_at: float | None = None
    last_error = 0
    stdscr.timeout(120)
    try:
        while True:
            while True:
                try:
                    history.append(events.get_nowait())
                except queue.Empty:
                    break
            rc = proc.poll()
            if rc is not None and not reader.is_alive() and events.empty():
                last_error = int(rc)
                break
            _draw(stdscr, argv, index, total, history, offset,
                  "stopping" if interrupted_at is not None else "running")
            try:
                k = stdscr.getch()
            except KeyboardInterrupt:
                k = 3
            if k in (ord("x"), ord("X"), 3) and interrupted_at is None and rc is None:
                interrupted_at = time.monotonic()
                history.append("[用户中断] 正在请求当前任务停止…")
                _interrupt(proc)
            elif k in (ord("k"), 259):
                offset = min(max(0, len(history) - 1), offset + 1)
            elif k in (ord("j"), 258):
                offset = max(0, offset - 1)
            elif k == 339:  # KEY_PPAGE
                offset = min(max(0, len(history) - 1), offset + 12)
            elif k == 338:  # KEY_NPAGE
                offset = max(0, offset - 12)
            if interrupted_at is not None and proc.poll() is None:
                elapsed = time.monotonic() - interrupted_at
                if elapsed > 10:
                    _terminate(proc, force=True)
                elif elapsed > 5:
                    _terminate(proc)
        reader.join(timeout=0.3)
        while True:
            try:
                history.append(events.get_nowait())
            except queue.Empty:
                break
    finally:
        stdscr.timeout(-1)
        if proc.poll() is None:
            _terminate(proc, force=True)
            proc.wait(timeout=5)
    if interrupted_at is not None:
        return 130, True
    return last_error, False


def run_jobs(stdscr, argv_list: list[list[str]]) -> tuple[int, bool]:
    """按依赖顺序运行多个 argv；完成后展示结果，返回 (退出码, 是否退出 TUI)。"""
    history: deque[str] = deque(maxlen=600)
    code = 0
    cancelled = False
    last_argv = argv_list[0] if argv_list else []
    executed = 0
    for i, argv in enumerate(argv_list, 1):
        last_argv = argv
        executed = i
        history.append(f"━━ 任务 {i}/{len(argv_list)}：{_display_command(argv)} ━━")
        code, cancelled = _run_one(stdscr, argv, i, len(argv_list), history)
        if code:
            if i < len(argv_list):
                history.append(f"[已停止] 后续 {len(argv_list) - i} 项未执行，防止使用旧产物")
            break

    status = "cancelled" if cancelled else ("error" if code else "success")
    offset = 0
    stdscr.timeout(-1)
    while True:
        _draw(stdscr, last_argv, executed, len(argv_list), history, offset, status)
        try:
            k = stdscr.getch()
        except KeyboardInterrupt:
            return code or 130, True
        if k in (10, 13, ord(" ")):
            return code, False
        if k in (27, ord("q"), ord("Q")):
            return code, True
        if k in (ord("k"), 259, 339):
            offset = min(max(0, len(history) - 1), offset + (12 if k == 339 else 1))
        if k in (ord("j"), 258, 338):
            offset = max(0, offset - (12 if k == 338 else 1))
