"""Shared cancellation flag and signal handling for long-running media tasks."""
from __future__ import annotations

import signal


class _Stop:
    flag = False


STOP = _Stop()


def check_stop() -> None:
    """Abort at a processing boundary before starting work or publishing it."""
    if STOP.flag:
        raise InterruptedError("用户已中断任务，未发布结果")


def install_signal_handlers() -> None:
    """Set STOP.flag on SIGINT/SIGTERM; processing loops must poll and stop safely."""

    def handler(signum, _frame):  # noqa: ANN001
        STOP.flag = True
        print("\n[中断] 收到信号，正在收尾停止...", flush=True)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except (ValueError, OSError):
            pass


def reset_stop() -> None:
    """Clear cancellation when reusing a process or test environment."""
    STOP.flag = False
