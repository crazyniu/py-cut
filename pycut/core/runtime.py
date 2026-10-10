"""运行时可变状态：全局中断标志 + 信号安装。

切片 / 遮盖都是长任务，需支持 Ctrl-C 优雅收尾。STOP.flag 被多处轮询。
"""
from __future__ import annotations

import signal


class _Stop:
    flag = False


STOP = _Stop()


def install_signal_handlers() -> None:
    """注册 SIGINT / SIGTERM -> 置 STOP.flag，长任务检测到后收尾退出。"""

    def handler(signum, _frame):  # noqa: ANN001
        STOP.flag = True
        print("\n[中断] 收到信号，正在收尾停止...", flush=True)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except (ValueError, OSError):
            pass


def reset_stop() -> None:
    """子进程 / 测试复用时清空中断标志。"""
    STOP.flag = False
