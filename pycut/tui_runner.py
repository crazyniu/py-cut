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
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MAX_LOG_CHARS = 4096
_SECRET_FLAGS = ("--llm-key", "--api-key")


def _cap_log_line(line: str) -> str:
    """Apply the existing display limit only after redacting whole secrets."""
    if len(line) > _MAX_LOG_CHARS:
        return line[:_MAX_LOG_CHARS] + "… [日志行已截断]"
    return line


def _sanitize(line: str, *, truncate: bool = True) -> str:
    """清理 CSI/OSC 和非打印字符；独立使用时仍遵守原长度上限。"""
    safe = "".join(
        c for c in _CONTROL.sub("", _OSC.sub("", line))
        if c.isprintable() or c == "\t"
    )
    return _cap_log_line(safe) if truncate else safe


def _redact(line: str, argv: list[str]) -> str:
    """从子进程文本中遮盖显式传入的 API Key，避免回显泄漏。"""
    for i, arg in enumerate(argv):
        value = ""
        if arg in _SECRET_FLAGS and i + 1 < len(argv):
            value = argv[i + 1]
        elif any(arg.startswith(flag + "=") for flag in _SECRET_FLAGS):
            value = arg.partition("=")[2]
        if value and len(value) < 4 and value in line:
            return "[日志已隐藏：包含敏感参数]"
        if value:
            line = line.replace(value, "********")
    return line


def _safe_log_line(line: str, argv: list[str]) -> str:
    """Strip terminal controls, redact full secrets, then cap the visible line.

    Truncating first can split a secret at the length boundary and reveal
    almost its entire prefix because _redact can no longer match the key.
    """
    return _cap_log_line(_redact(_sanitize(line, truncate=False), argv))


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
              "Enter/Esc 返回主菜单 · Q 退出 · ↑↓ 滚动")
    compact = ("X中断  ↑↓滚动  Ctrl+C取消" if status == "running" else
               "等待任务停止…" if status == "stopping" else
               "Enter/Esc主菜单  Q退出  ↑↓滚动")
    tui._footer(stdscr, footer, compact)
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
            cmd, cwd=str(_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
            errors="replace", bufsize=1, start_new_session=(os.name != "nt"),
        )
    except OSError as e:
        history.append(_safe_log_line(f"[无法启动] {type(e).__name__}: {e}", argv))
        return 2, False

    events: queue.Queue[str] = queue.Queue(maxsize=2048)

    read_errors: list[str] = []

    def _reader():
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                events.put(_safe_log_line(line.rstrip("\r\n"), argv))
        except (OSError, UnicodeError) as exc:
            # A successful child exit is not success if its output stream failed.
            message = _safe_log_line(f"[日志读取失败] {type(exc).__name__}: {exc}", argv)
            read_errors.append(message)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

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
                last_error = int(rc) or (2 if read_errors else 0)
                for message in read_errors:
                    history.append(message)
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
    if not argv_list:
        return 0, False
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
        if cancelled and not code:
            code = 130
        if code:
            if i < len(argv_list):
                history.append(f"[已停止] 后续 {len(argv_list) - i} 项未执行，防止使用旧产物")
            break

    status = "cancelled" if cancelled else ("error" if code else "success")
    label = {"cancelled": "已中断", "error": "失败", "success": "成功"}[status]
    history.append(
        f"[执行结果] {label} · 已运行 {executed}/{len(argv_list)} 项 · "
        f"跳过 {len(argv_list) - executed} 项 · 退出码 {code}"
    )
    offset = 0
    stdscr.timeout(-1)
    while True:
        _draw(stdscr, last_argv, executed, len(argv_list), history, offset, status)
        try:
            k = stdscr.getch()
        except KeyboardInterrupt:
            return code or 130, True
        if k in (10, 13, ord(" "), 27):
            return code, False
        if k in (ord("q"), ord("Q")):
            return code, True
        if k in (ord("k"), 259, 339):
            offset = min(max(0, len(history) - 1), offset + (12 if k == 339 else 1))
        if k in (ord("j"), 258, 338):
            offset = max(0, offset - (12 if k == 338 else 1))
