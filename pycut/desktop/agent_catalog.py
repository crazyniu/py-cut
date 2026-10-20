"""Read the installed Codex model catalog without inference or credential access."""
from __future__ import annotations

from ..core.runtime import STOP
from .local_vision import CodexVision, _binary


def list_models(provider: str = "codex", binary: str = "", timeout: float = 10.0) -> list[dict]:
    if provider != "codex":
        raise ValueError("桌面端统一使用 Codex")
    executable = binary or _binary("codex")
    if not executable:
        raise ValueError("请先安装并登录 Codex")
    if STOP.flag or timeout <= 0:
        raise TimeoutError("读取 Codex 模型列表超时或已中断")
    client = CodexVision(executable)
    try:
        return client.list_models(timeout)
    finally:
        client.close()
