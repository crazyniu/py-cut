"""通用小工具：日志、格式化、命令执行。

不依赖 cv2 / ffmpeg 之外的任何第三方库（纯标准库）。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any


def log(msg: str) -> None:
    """统一打印（强制 flush，便于长任务实时看到进度）。"""
    print(msg, flush=True)


def fmt_dur(sec: float) -> str:
    """秒 -> 人类可读时长。"""
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_bytes(n: int) -> str:
    """字节 -> 人类可读大小。"""
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.1f}{unit}"
        val /= step
    return f"{val:.1f}TB"


def run_cmd(args: list[str], timeout: int) -> tuple[int, str, str]:
    """统一 subprocess 调用：绝对不用 shell=True（中文路径/空格/括号）。

    返回 (returncode, stdout, stderr)。
    """
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", f"executable not found: {e}"
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def env_str(key: str, default: str) -> str:
    """读取环境变量，空值回退 default。"""
    return os.environ.get(key, default) or default


def safe_listdir(path: Any) -> list[Any]:
    """os.scandir 包装：macOS 的 find -iname 会静默丢文件，统一走 scandir。"""
    import os as _os  # noqa: PLC0415

    out: list[Any] = []
    try:
        with _os.scandir(str(path)) as it:
            for entry in it:
                out.append(entry)
    except OSError:
        pass
    return out
