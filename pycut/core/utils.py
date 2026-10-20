"""Standard-library logging, formatting, and subprocess helpers."""
from __future__ import annotations

import subprocess
import os
import time


def log(msg: str) -> None:
    """Print and flush immediately for live task logs."""
    print(msg, flush=True)


def fmt_dur(sec: float) -> str:
    """Format seconds for display."""
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_bytes(n: int) -> str:
    """Format a byte count for display."""
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.1f}{unit}"
        val /= step
    return f"{val:.1f}TB"


def run_cmd(args: list[str], timeout: int) -> tuple[int, str, str]:
    """Return (returncode, stdout, stderr) using argument lists, never shell=True.

    Preserve paths containing spaces, Unicode, or shell metacharacters."""
    if os.environ.get("PYCUT_DESKTOP_JOB") == "1":
        return _desktop_command(args, timeout)
    try:
        p = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,  # Child media tools must not consume TUI keyboard input.
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has killed/reaped the child, but TimeoutExpired still
        # carries diagnostics emitted before the timeout. Keep the root cause.
        out = (exc.output or b"").decode("utf-8", "replace")
        err = (exc.stderr or b"").decode("utf-8", "replace")
        return -1, out, f"timeout after {timeout}s\n{err}".rstrip()
    except FileNotFoundError as e:
        return -1, "", f"executable not found: {e}"
    except OSError as e:
        return -1, "", f"cannot execute command: {e}"
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def _desktop_command(args, timeout):
    """Keep desktop cancellation responsive while retaining subprocess diagnostics."""
    from .runtime import STOP
    started = time.monotonic()
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return -1, "", f"cannot execute command: {exc}"
    while True:
        try:
            out, err = process.communicate(timeout=0.25)
            return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            stopped = STOP.flag
            if not stopped and time.monotonic() - started < timeout:
                continue
            process.terminate()
            try:
                out, err = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()
            reason = "已中断" if stopped else f"timeout after {timeout}s"
            return -130 if stopped else -1, out.decode("utf-8", "replace"), reason + "\n" + err.decode("utf-8", "replace")
