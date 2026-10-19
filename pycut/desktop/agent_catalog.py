"""Read installed agents' model metadata without inference or credential access.

Only projected model identifiers and names leave this module. Child processes run
in disposable directories, with custom tools and startup integrations disabled.
"""
from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time

from ..core.runtime import STOP
from .local_vision import CodexVision, _binary


def _claude_command(binary: str) -> list[str]:
    # Safe mode preserves the agent's auth/model configuration while disabling
    # inherited context, hooks, skills, installed plugins, and MCP servers.
    return [binary, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--safe-mode", "--tools", "", "--strict-mcp-config", "--mcp-config",
            '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}', "--no-session-persistence"]


class _JsonLines:
    """Own one bounded stdio process; never expose its raw errors or stderr."""

    def __init__(self, command: list[str], cwd: str):
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        self.messages = queue.Queue()

        def read():
            try:
                for line in self.process.stdout:
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.messages.put(message)
            finally:
                self.messages.put(None)

        threading.Thread(target=read, daemon=True).start()

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next(self, deadline: float) -> dict:
        while not STOP.flag and time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                raise RuntimeError("本地 Agent 服务已退出或返回了无效数据")
            if message.get("type") == "control_request":
                raise RuntimeError("本地 Agent 请求了额外权限，已停止")
            return message
        raise TimeoutError("本地 Agent 等待超时或任务已中断")

    def initialize_claude(self, deadline: float) -> dict:
        self.send({"type": "control_request", "request_id": "catalog", "request": {
            "subtype": "initialize", "hooks": {}, "skills": []}})
        while True:
            message = self.next(deadline)
            response = message.get("response") or {}
            if message.get("type") == "control_response" and response.get("request_id") == "catalog":
                if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                    raise RuntimeError("本地 Claude Code 初始化失败")
                return response["response"]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()


def list_models(provider: str = "codex", binary: str = "", timeout: float = 10.0) -> list[dict]:
    """Return real ``{id, name, is_default}`` entries or a sanitized protocol error.

    Codex/Pi entries are image-capable. Claude's SDK catalog does not advertise
    modalities; catalog availability alone does not establish recognition support.
    """
    if provider not in {"codex", "claude", "pi"}:
        raise ValueError("不支持的本地 Agent")
    executable = binary or _binary(provider)
    if not executable:
        raise ValueError("尚未安装所选本地 Agent")
    deadline = time.monotonic() + timeout
    if STOP.flag or time.monotonic() >= deadline:
        raise TimeoutError("读取本地模型列表超时或任务已中断")
    if provider == "codex":
        client = CodexVision(executable)
        try:
            return client.list_models(timeout)
        finally:
            client.close()
    command = _claude_command(executable) if provider == "claude" else [
        executable, "--mode", "rpc", "--no-session", "--no-tools", "--no-extensions", "--no-mcp",
        "--no-skills", "--no-prompt-templates", "--no-context-files"]
    with tempfile.TemporaryDirectory(prefix="pycut-agent-catalog-") as folder:
        client = _JsonLines(command, folder)
        try:
            if provider == "claude":
                entries = client.initialize_claude(deadline).get("models")
            else:
                client.send({"id": "catalog", "type": "get_available_models"})
                while True:
                    response = client.next(deadline)
                    if response.get("id") == "catalog" and response.get("type") == "response":
                        if response.get("success") is not True:
                            raise RuntimeError("本地 PI 模型列表读取失败")
                        entries = (response.get("data") or {}).get("models")
                        break
            if not isinstance(entries, list):
                raise RuntimeError("本地 Agent 返回了无效模型列表")
            models = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RuntimeError("本地 Agent 返回了无效模型列表")
                if provider == "claude":
                    model_id = entry.get("resolvedModel") or entry.get("value")
                    name = entry.get("displayName")
                    is_default = entry.get("value") == "default"
                else:
                    if "image" not in (entry.get("input") or []):
                        continue
                    model_id, agent_provider = entry.get("id"), entry.get("provider")
                    if not isinstance(agent_provider, str) or not agent_provider:
                        raise RuntimeError("本地 PI 返回了无效模型列表")
                    model_id = f"{agent_provider}/{model_id}" if isinstance(model_id, str) and model_id else None
                    name, is_default = entry.get("name"), False
                if not isinstance(model_id, str) or not model_id:
                    raise RuntimeError("本地 Agent 返回了无效模型列表")
                previous = models.get(model_id, {})
                models[model_id] = {"id": model_id, "name": name if isinstance(name, str) and name else model_id,
                                    "is_default": is_default or previous.get("is_default", False)}
            return list(models.values())
        finally:
            client.close()
