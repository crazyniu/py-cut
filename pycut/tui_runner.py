"""Run CLI subprocesses while the main thread owns curses rendering and input.

Use the existing command parsers and processing paths. Stop dependent jobs after
a failure; show explicit completion, review, failure, and interruption states."""
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
from .core.progress import parse

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
    """Strip CSI/OSC and nonprintable characters, respecting the optional length limit."""
    safe = "".join(
        c for c in _CONTROL.sub("", _OSC.sub("", line))
        if c.isprintable() or c == "\t"
    )
    return _cap_log_line(safe) if truncate else safe


def _redact(line: str, argv: list[str]) -> str:
    """Remove explicitly supplied API keys from subprocess output."""
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


def _safe_log_line_full(line: str, argv: list[str]) -> str:
    """Redact complete secrets before either terminal or on-disk truncation."""
    line = _redact(_sanitize(line, truncate=False), argv)
    for key_name in ("PYCUT_MAIN_KEY", "PYCUT_BACKUP_KEY", "DEEPSEEK_API_KEY"):
        secret = os.environ.get(key_name, "")
        if secret and secret in line:
            if len(secret) < 4:
                return "[日志已隐藏：包含敏感参数]"
            line = line.replace(secret, "********")
    return line


def _safe_log_line(line: str, argv: list[str]) -> str:
    """Display log: redact before the 4096-character limit, never split secrets."""
    return _cap_log_line(_safe_log_line_full(line, argv))


def _display_command(argv: list[str]) -> str:
    """Render a command with explicit API keys redacted."""
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


def _draw_results(stdscr, results: list[dict], total: int, status: str,
                  history: deque[str], offset: int, show_details: bool) -> int:
    import curses
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header = tui._brand(stdscr)
    labels = {"success": "全部处理完成", "review": "处理完成 · 有待复查项",
              "error": "处理失败 · 后续依赖已停止", "cancelled": "处理已中断"}
    color = "good" if status == "success" else "bad" if status == "error" else "warn"
    completed = sum(r["code"] == 0 and not r["cancelled"] for r in results)
    tui._safe_add(stdscr, header, 1, "━━ " + labels[status] + " ━━", w, color, curses.A_BOLD)
    tui._safe_add(stdscr, header + 1, 1, f"已结束 · 完成流程 {completed}/{total} · 已运行 {len(results)} · 未执行 {total - len(results)}", w, color)
    tui._safe_add(stdscr, header + 2, 1, "Enter/Esc 确认返回 · Q 退出 · ↑↓滚动 · L 日志", w, "accent")
    names = {"slice": "切片", "cover": "字幕擦除", "dedup": "去重", "montage": "混剪"}
    rows = []
    for i, result in enumerate(results, 1):
        events = result["events"]
        label = "已中断" if result["cancelled"] else "失败" if result["code"] else "待复查" if any(e.get("review") for e in events.values()) else "完成"
        rows.append(f"{i}. {names.get(result['command'], result['command'])} · {label} · 退出码 {result['code']}")
        if not events:
            rows.append("  未提供阶段计数；以退出码确认任务结束")
        for stage, event in events.items():
            rows.append(f"  {stage} {event.get('completed', 0)}/{event.get('total', 0)} · 成功 {event.get('success', 0)} · 失败 {event.get('failed', 0)} · 待复查 {event.get('review', 0)} · 跳过 {event.get('skipped', 0)}")
        outputs = dict.fromkeys(e['output'] for e in events.values() if e.get('output'))
        for output in outputs:
            rows.append("  结果目录：" + output)
    if show_details:
        rows.extend(line for line in history if not parse(line))
    wrapped = []
    for line in rows:
        while tui._disp_width(line) > max(2, w - 3):
            part = tui._clip(line, max(2, w - 3))
            wrapped.append(part)
            line = line[len(part):]
        wrapped.append(line)
    rows = wrapped
    count = max(0, h - header - 4)
    start = min(offset, max(0, len(rows) - count))
    for y, row in enumerate(rows[start:start + count], header + 3):
        tui._safe_add(stdscr, y, 1, row, w, "text")
    if h > header + 4:
        tui._safe_add(stdscr, h - 1, 1, "任务已经结束，等待你确认" + (" · ↓查看更多" if len(rows) > count else ""), w, "accent", curses.A_BOLD)
    stdscr.refresh()
    return start


def _montage_plan(argv_list: list[list[str]]) -> dict | None:
    """Count planned outputs from the actual commands, never from stage totals."""
    if not argv_list or any(not args or args[0] != "montage" for args in argv_list):
        return None
    counts = []
    for args in argv_list:
        number = "1"
        for i, arg in enumerate(args):
            if arg == "--fission" and i + 1 < len(args):
                number = args[i + 1]
            elif arg.startswith("--fission="):
                number = arg.partition("=")[2]
        try:
            counts.append(max(1, int(number)))
        except ValueError:
            return None
    return {"counts": counts, "total": sum(counts), "completed": 0,
            "files": [], "started_at": time.monotonic()}


def _init_montage_log(batch: dict, argv_list: list[list[str]]) -> None:
    """Create one collision-safe session log in the configured montage output root."""
    roots = []
    for argv in argv_list:
        out_dir = None
        for i, arg in enumerate(argv):
            if arg == "--out-dir" and i + 1 < len(argv):
                out_dir = argv[i + 1]
            elif arg.startswith("--out-dir="):
                out_dir = arg.partition("=")[2]
        if out_dir:
            roots.append(Path(out_dir).expanduser().resolve())
    # TUI always supplies --out-dir; do not create an unrelated log on a guessed path.
    if len(roots) != len(argv_list) or len(set(roots)) != 1:
        return
    root = roots[0]
    # Video folders contain only published media. Keep diagnostics outside out/.
    log_root = (root.parent.parent / "logs" / root.name
                if root.parent.name.lower() == "out"
                else root.parent / (root.name + "_logs"))
    try:
        root.mkdir(parents=True, exist_ok=True)
        log_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for number in range(100):
            suffix = f"-{number}" if number else ""
            path = log_root / f"pycut-{stamp}-{os.getpid()}{suffix}.log"
            try:
                with path.open("x", encoding="utf-8") as output:
                    output.write(f"[会话开始] {time.strftime('%Y-%m-%d %H:%M:%S')} · 输出根目录 {root}\n")
                batch["log_path"] = str(path)
                return
            except FileExistsError:
                continue
        batch["log_error"] = f"无法创建日志：同名文件过多（{log_root}）"
    except OSError as exc:
        batch["log_error"] = f"无法在日志目录写入日志：{exc}"


def _append_run_log(batch: dict | None, message: str) -> None:
    """Flush every CLI line on disk; an I/O failure never changes media success."""
    if not batch or not batch.get("log_path"):
        return
    try:
        with Path(batch["log_path"]).open("a", encoding="utf-8") as output:
            output.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message[:65536] + "\n")
    except (OSError, UnicodeError) as exc:
        batch["log_error"] = f"日志写入中断：{type(exc).__name__}: {exc}"
        batch.pop("log_path", None)


def _record_montage_file(batch: dict | None, event: dict) -> None:
    """Only the successful, validated final-stage event confirms a published file."""
    if (batch is not None and event.get("stage") == "混剪合成"
            and event.get("completed") == 5 and event.get("success", 0) > 0
            and isinstance(event.get("file"), str) and event["file"]):
        path = event["file"]
        files = batch.setdefault("files", [])
        if path not in files:
            files.append(path)


def _finished_videos(events: dict, planned: int) -> int:
    # Only this event confirms complete, checked output files. Internal steps
    # and attempted/failed variants must never increase the finished count.
    return min(planned, events.get("裂变生成", {}).get("success", 0))


def _current_montage_stage(events: dict) -> dict | None:
    """Return measured activity of the current phase, not guessed overall %."""
    phase = events.get("混剪合成", {}).get("completed", 0)
    if phase == 1:
        # Both pools are scanned before background encoding. The most recent
        # event wins so a completed scan cannot conceal a later active stage.
        names = ("背景素材扫描", "画中画素材扫描", "背景片段编码")
    elif phase == 2:
        names = ("画中画编码",)
    else:
        return None
    for name in reversed(events):
        if name in names:
            event = events[name]
            if isinstance(event.get("total"), int) and event["total"] > 0:
                return {"stage": name, **event}
    return None


def _draw_montage(stdscr, batch: dict, history: deque[str], offset: int,
                  status: str, progress: dict, elapsed: float,
                  show_details: bool, finished: bool) -> int:
    import curses
    h, w = stdscr.getmaxyx()
    if tui._small_screen(stdscr):
        return offset
    stdscr.erase()
    body = tui._frame(stdscr, "混剪进度" if not finished else "混剪结果")
    counts = batch["counts"]
    total = batch["total"]
    done = batch["completed"]
    if not finished:
        done += _finished_videos(progress, batch["current_planned"])
    done = min(total, done)
    per_video = f"每个生成 {counts[0]} 条" if len(set(counts)) == 1 else "按已确认数量生成"
    color = "good" if status == "success" else "bad" if status == "error" else "warn" if status in ("cancelled", "stopping", "review") else "info"
    labels = {"running": "正在生成", "success": "全部生成完成", "error": "生成已停止 · 有视频未完成",
              "cancelled": "已中断", "stopping": "正在停止", "review": "生成结束 · 有待复查项"}
    text = labels.get(status, status)
    if status == "running":
        elapsed = time.monotonic() - batch["started_at"]
        text += f" · 已用时 {int(elapsed // 60)}分{int(elapsed % 60)}秒"
    tui._safe_add(stdscr, body, 2, text, w, color, curses.A_BOLD)
    tui._safe_add(stdscr, body + 1, 2, f"已选 {len(counts)} 个话术视频 · {per_video}", w, "text")
    tui._safe_add(stdscr, body + 2, 2, f"需要生成 {total} 个视频 · 已生成 {done} 个（成片完成率）", w, "text", curses.A_BOLD)
    width = max(8, min(36, w - 15))
    filled = done * width // total
    tui._safe_add(stdscr, body + 3, 2,
                  f"[{'█' * filled}{'░' * (width - filled)}] {done * 100 // total}%", w, color)
    y = body + 5
    # UI shows containing folders; retain batch["files"] as real published files
    # for diagnostics, and do not collapse distinct output directories.
    output_dirs = (list(dict.fromkeys(str(Path(file).parent)
                                     for file in batch.get("files", [])))
                   if finished else [])
    if not finished and status in ("running", "stopping"):
        if status == "stopping":
            activity = "正在停止当前处理，请稍候"
        else:
            phase = progress.get("混剪合成", {}).get("completed")
            action = {0: "正在准备素材", 1: "正在组合视频画面", 2: "正在合成视频画面",
                      3: "正在添加字幕", 4: "正在加入原声", 5: "正在检查并保存"}.get(phase, "正在准备素材")
            attempted = progress.get("裂变生成", {}).get("completed", 0)
            current = min(total, batch["completed"] + attempted + 1)
            dots = "." * (int(time.monotonic() * 2) % 3 + 1)
            segment = progress.get("背景片段编码", {}) if phase == 1 else {}
            count = segment.get("total", 0)
            if count:
                completed = min(count, segment.get("completed", 0))
                action = f"背景片段已编码 {completed}/{count}（{completed * 100 // count}%）"
            activity = f"第 {current} 个视频 · {action}{dots}"
        tui._safe_add(stdscr, body + 4, 2, activity, w, "info")
        y += 1
        current_stage = _current_montage_stage(progress)
        if current_stage is not None and y < h - 3:
            count = min(current_stage["completed"], current_stage["total"])
            maximum = current_stage["total"]
            name = current_stage["stage"]
            detail = current_stage.get("detail", "")
            tui._safe_add(stdscr, y, 2,
                          f"当前阶段 · {name} {count}/{maximum}（{count * 100 // maximum}%） "
                          f"{detail}", w, "info")
            y += 1
            if y < h - 4 and h >= 20:
                segment_width = max(8, min(36, w - 15))
                blocks = count * segment_width // maximum
                tui._safe_add(stdscr, y, 2,
                              f"[{'█' * blocks}{'░' * (segment_width - blocks)}] "
                              f"{count * 100 // maximum}% · 本阶段", w, "info")
                y += 1
        elif y < h - 3:
            detail = progress.get("混剪合成", {}).get("detail")
            if detail:
                tui._safe_add(stdscr, y, 2, "当前任务 · " + detail, w, "dim")
                y += 1
    elif finished and output_dirs:
        remaining = "输出目录：" + output_dirs[-1]
        y = body + 4
        while remaining and y < h - 2:
            part = tui._clip(remaining, w - 3)
            tui._safe_add(stdscr, y, 2, part, w, "good")
            remaining = remaining[len(part):]
            y += 1
        y += 1
    if batch.get("log_path"):
        tui._safe_add(stdscr, y, 2, "日志文件：" + batch["log_path"], w, "accent")
        y += 1
    if batch.get("log_error"):
        tui._safe_add(stdscr, y, 2, "[日志警告] " + batch["log_error"], w, "warn")
        y += 1
    logs = [line for line in history if not parse(line)]
    errors = [line for line in logs if re.search(r"\[(?:[^\]]*(?:失败|错误)|已停止)\]", line)]
    if not show_details and status != "error":
        logs = errors
    # Show each *other* result folder once; the latest one is already above.
    visible = (["输出目录：" + path for path in output_dirs[:-1]] if finished else []) + logs
    if visible:
        label = "其他输出目录与日志" if finished and len(output_dirs) > 1 else (
            "详细日志" if finished else "实时日志 · ↑↓/PgUp/PgDn 滚动（L 收起）")
        if status == "error" and not show_details:
            label = "错误日志"
        elif not show_details and not finished:
            label = "错误日志"
        # Reuse the spare separator row on compact terminals; with a log
        # path or warning that row is occupied, so use the next row instead.
        header_y = y if batch.get("log_path") or batch.get("log_error") else y - 1
        tui._safe_add(stdscr, header_y, 2, label, w,
                      "bad" if errors or status == "error" else "dim")
        first_row = header_y + 1
        rows = max(0, h - first_row - 2)
        offset = min(offset, max(0, len(visible) - rows))
        start = max(0, len(visible) - rows - offset)
        for i, line in enumerate(visible[start:start + rows]):
            tui._safe_add(stdscr, first_row + i, 2, line, w, "dim")
    tui._footer(stdscr,
                "Enter/Esc 返回 · Q 退出 · ↑↓/PgUp/PgDn 滚动 · L 日志" if finished
                else "X 中止 · Ctrl+C 中断 · ↑↓/PgUp/PgDn 滚动 · L 日志",
                "Enter返回  Q退出  L日志" if finished else "X中止  L日志")
    stdscr.refresh()
    return offset


def _draw(stdscr, argv: list[str], index: int, total: int,
          history: deque[str], offset: int, status: str, progress: dict | None = None, elapsed: float = 0, show_details: bool = False, results: list[dict] | None = None, batch: dict | None = None) -> int | None:
    import curses  # noqa: PLC0415

    if batch is not None:
        return _draw_montage(stdscr, batch, history, offset, status, progress or {},
                             elapsed, show_details, results is not None)
    if results is not None:
        return _draw_results(stdscr, results, total, status, history, offset, show_details)
    h, w = stdscr.getmaxyx()
    if tui._small_screen(stdscr):
        return
    stdscr.erase()
    names = {"slice": "切片", "cover": "字幕擦除", "dedup": "去重", "montage": "混剪"}
    body = tui._frame(stdscr, "任务进度" if status in ("running", "stopping") else "处理结果",
               f"{names.get(argv[0], '视频处理')} · 流程 {index}/{total}")
    color = ("info" if status == "running" else "warn" if status == "stopping"
             else "good" if status == "success" else "bad")
    labels = {"running": "运行中", "stopping": "正在停止", "success": "处理完成",
              "error": "处理失败 · 请查看失败项", "cancelled": "已中断"}
    states = dict(progress or {})
    if not progress:
        for line in history:
            event = parse(line)
            if event:
                states[event["stage"]] = event
    if status == "success" and any(e.get("review") for e in states.values()):
        labels["success"] = "处理完成 · 有待复查项"
        color = "warn"
    tui._safe_add(stdscr, body, 2, labels.get(status, status) + (f" · 已用时 {int(elapsed // 60)}分{int(elapsed % 60)}秒" if status == "running" else ""), w, color, curses.A_BOLD)
    y = body + 1
    stage_rows = max(0, min(5, h - y - 6))
    for stage, event in (list(states.items())[-stage_rows:] if stage_rows else []):
        done, count = event.get("completed", 0), event.get("total", 0)
        percent = f"{done * 100 // count}%" if count else "处理中"
        bar_width = 8 if w < 80 else 12
        filled = min(bar_width, done * bar_width // count) if count else 0
        summary = f"{stage}  [{'█' * filled}{'░' * (bar_width - filled)}] {percent}  {done}/{count}"
        summary += f"  成功 {event.get('success', 0)} / 失败 {event.get('failed', 0)}"
        if event.get("review"):
            summary += f" / 待复查 {event['review']}"
        if event.get("skipped"):
            summary += f" / 跳过 {event['skipped']}"
        tui._safe_add(stdscr, y, 2, summary, w, "bad" if event.get("failed") else "warn" if event.get("review") else "text")
        y += 1
    current = next(reversed(states.values()), {}) if states else {}
    if y < h - 4:
        tui._safe_add(stdscr, y, 2, current.get("detail", "正在准备，请稍候…" if status == "running" else "处理已结束，详见结果或错误信息"), w, "info")
        y += 1
    if current.get("output") and y < h - 4:
        tui._safe_add(stdscr, y, 2, "结果目录：" + tui._ellipsize(current["output"], w - 16, tail=True), w, "good")
        y += 1
    logs = [line for line in history if not parse(line)]
    warnings = [line for line in logs if re.search(r"\[(?:[^\]]*(?:失败|错误|警告|残留|待复查)|已停止)\]", line)]
    if warnings and y < h - 4:
        tui._safe_add(stdscr, y, 2, warnings[-1], w, "warn" if "[待复查]" in warnings[-1] else "bad")
        y += 1
    if y < h - 3:
        tui._safe_add(stdscr, y, 2, "详细日志 · L 展开/收起 · ↑↓ 滚动", w, "dim")
        rows = max(0, h - y - 4)
        if not show_details:
            rows = min(3, rows)
        start = max(0, len(logs) - rows - offset)
        for i, line in enumerate(logs[start:start + rows]):
            tui._safe_add(stdscr, y + 1 + i, 2, line, w, "dim")
    footer = ("L 详细日志 · X 中止 · Ctrl+C 中断"
              if status == "running" else
              "正在等待子进程安全停止…" if status == "stopping" else
              "Enter/Esc 返回主菜单 · L 详细日志 · Q 退出")
    compact = ("L日志  X中断  Ctrl+C取消" if status == "running" else
               "等待任务停止…" if status == "stopping" else
               "Enter主菜单  L日志  Q退出")
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
             history: deque[str], batch: dict | None = None) -> tuple[int, bool]:
    """Run a CLI job in its own process group; only the main thread touches curses."""
    cmd = [sys.executable, str(_ROOT / "pycut.py"), *argv]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
            errors="replace", bufsize=1, start_new_session=(os.name != "nt"),
            env={**os.environ, "PYCUT_PROGRESS": "1"},
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
                message = _safe_log_line_full(line.rstrip("\r\n"), argv)
                _append_run_log(batch, message)
                events.put(_cap_log_line(message))
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
    started_at = time.monotonic()
    progress = {}  # A new child must not inherit the previous command’s stages.
    offset = 0
    show_details = batch is not None  # Montage shows a live scrolling task log by default.
    interrupted_at: float | None = None
    last_error = 0
    stdscr.timeout(120)
    try:
        while True:
            while True:
                try:
                    line = events.get_nowait()
                    event = parse(line)
                    if event:
                        # Each new variant must not show the previous video's
                        # completed segment/frame counters as current progress.
                        if (event["stage"] == "混剪合成"
                                and event.get("completed") == 0):
                            for stale in ("背景素材扫描", "画中画素材扫描",
                                          "背景片段编码", "画中画编码"):
                                progress.pop(stale, None)
                        progress.pop(event["stage"], None)
                        progress[event["stage"]] = event
                        _record_montage_file(batch, event)
                    else:
                        if offset:
                            offset = min(600, offset + 1)  # Keep manual scroll anchored.
                        history.append(line)
                except queue.Empty:
                    break
            rc = proc.poll()
            if rc is not None and not reader.is_alive() and events.empty():
                last_error = int(rc) or (2 if read_errors else 0)
                for message in read_errors:
                    history.append(message)
                    _append_run_log(batch, message)
                break
            _draw(stdscr, argv, index, total, history, offset,
                  "stopping" if interrupted_at is not None else "running", progress, time.monotonic() - started_at, show_details, batch=batch)
            try:
                k = stdscr.getch()
            except KeyboardInterrupt:
                k = 3
            if k in (ord("x"), ord("X"), 3) and interrupted_at is None and rc is None:
                interrupted_at = time.monotonic()
                history.append("[用户中断] 正在请求当前任务停止…")
                _interrupt(proc)
            elif k in (ord("l"), ord("L")):
                show_details = not show_details
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
                line = events.get_nowait()
                event = parse(line)
                if event:
                    if (event["stage"] == "混剪合成"
                            and event.get("completed") == 0):
                        for stale in ("背景素材扫描", "画中画素材扫描",
                                      "背景片段编码", "画中画编码"):
                            progress.pop(stale, None)
                    progress.pop(event["stage"], None)
                    progress[event["stage"]] = event
                    _record_montage_file(batch, event)
                else:
                    history.append(line)
            except queue.Empty:
                break
    finally:
        stdscr.timeout(-1)
        if proc.poll() is None:
            _terminate(proc, force=True)
            proc.wait(timeout=5)
    from .core.progress import PREFIX
    import json
    for event in progress.values():
        history.append(PREFIX + json.dumps(event, ensure_ascii=False))
    if interrupted_at is not None:
        return 130, True
    return last_error, False


def run_jobs(stdscr, argv_list: list[list[str]]) -> tuple[int, bool]:
    """Run jobs in dependency order; show results and return (exit_code, quit_tui)."""
    if not argv_list:
        return 0, False
    history: deque[str] = deque(maxlen=600)
    code = 0
    cancelled = False
    last_argv = argv_list[0] if argv_list else []
    executed = 0
    results = []
    batch = _montage_plan(argv_list)
    if batch is not None:
        _init_montage_log(batch, argv_list)
    for i, argv in enumerate(argv_list, 1):
        last_argv = argv
        executed = i
        history.append(f"━━ 任务 {i}/{len(argv_list)}：{argv[0]} ━━")
        _append_run_log(batch, f"━━ 任务 {i}/{len(argv_list)}：{_display_command(argv)} ━━")
        task_history: deque[str] = deque(maxlen=600)
        if batch is not None:
            batch["current_planned"] = batch["counts"][i - 1]
            code, cancelled = _run_one(stdscr, argv, i, len(argv_list), task_history, batch=batch)
        else:
            code, cancelled = _run_one(stdscr, argv, i, len(argv_list), task_history)
        history.extend(task_history)
        _append_run_log(batch, f"[任务结束] {i}/{len(argv_list)} · 退出码 {code} · 中断 {cancelled}")
        results.append({"command": argv[0], "code": code or (130 if cancelled else 0),
                        "cancelled": cancelled,
                        "events": {e["stage"]: e for line in task_history if (e := parse(line))}})
        if batch is not None:
            batch["completed"] += _finished_videos(results[-1]["events"], batch["current_planned"])
        if cancelled and not code:
            code = 130
        if code:
            if i < len(argv_list):
                history.append(f"[已停止] 后续 {len(argv_list) - i} 项未执行，防止使用旧产物")
            break

    status = "cancelled" if cancelled else ("error" if code else "success")
    label = {"cancelled": "已中断", "error": "失败", "success": "成功"}[status]
    summary = (f"[执行结果] {label} · 已运行 {executed}/{len(argv_list)} 项 · "
               f"跳过 {len(argv_list) - executed} 项 · 退出码 {code}")
    history.append(summary)
    _append_run_log(batch, summary)
    if batch is not None and batch.get("log_path"):
        history.append("[日志文件] " + batch["log_path"])
    if status == "success" and any(e.get("review") for r in results for e in r["events"].values()):
        status = "review"
    import curses
    try:
        curses.flushinp()  # Discard keys queued while the last child was finishing.
    except curses.error:
        pass
    offset = 0
    show_details = False
    stdscr.timeout(-1)
    while True:
        start = _draw(stdscr, last_argv, executed, len(argv_list), history, offset, status, show_details=show_details, results=results, batch=batch)
        if start is not None:
            offset = start
        try:
            k = stdscr.getch()
        except KeyboardInterrupt:
            return code or 130, True
        if k in (10, 13, 27):
            return code, False
        if k in (ord("q"), ord("Q")):
            return code, True
        if k in (ord("l"), ord("L")):
            show_details = not show_details
        if k in (ord("k"), 259, 339):
            offset = max(0, offset - (12 if k == 339 else 1))
        if k in (ord("j"), 258, 338):
            offset += 12 if k == 338 else 1
