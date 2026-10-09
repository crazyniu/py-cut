"""LLM 客户端（OpenAI 兼容 / 纯标准库 urllib）。

设计要点：
  - 不依赖 openai / requests / httpx（环境未装，且应保持在标准库内）。
  - 推理模型需 max_tokens >= 500，否则 content 被 reasoning 吃光返回空。
  - 连续失败达阈值 -> 全局禁用，避免长任务空转。
  - 视觉问答 = 单帧 base64 + 自定义 prompt，通用复用（人脸判定 / 字幕检测）。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

import cv2

from .. import config
from .runtime import STOP
from .utils import log


class LLMClient:
    def __init__(self, url: str, key: str, model: str, enabled: bool = True):
        self.url = url.rstrip("/") + "/chat/completions"
        self.key = key
        self.model = model
        self.enabled = enabled
        self.calls = 0
        self.failures = 0
        self._consec_fail = 0
        self._lock = threading.Lock()
        self._jpg_quality = 80

    def _disable(self, why: str) -> None:
        with self._lock:
            if self.enabled:
                self.enabled = False
                log(f"[LLM] 连续失败 {config.LLM_MAX_CONSEC_FAIL} 次，全局禁用 LLM（{why}）")

    def _post(self, payload: dict) -> tuple[int, str]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return -1, str(e)

    def _extract_json(self, text: str) -> dict | None:
        """防御式解析：去 markdown 围栏 -> 首尾花括号 -> 正则兜底。"""
        if not text:
            return None
        t = text.strip()
        t = re.sub(r"^```(?:json)?", "", t).strip()
        t = re.sub(r"```$", "", t).strip()
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
        m = re.search(r'"face"\s*:\s*(true|false)', t, re.IGNORECASE)
        if m:
            conf = re.search(r'"confidence"\s*:\s*([0-9.]+)', t)
            return {
                "face": m.group(1).lower() == "true",
                "confidence": float(conf.group(1)) if conf else 0.5,
                "reason": "regex_fallback",
            }
        return None

    def ask(self, frame) -> dict | None:
        """人脸判定：返回 {"face": bool, "confidence": float, "reason": str} 或 None。"""
        return self.ask_vision(frame, (
            "判断这张视频截图里是否清晰可见至少一个人的脸（大头/半身/全身有人脸均可）。"
            "只回答 JSON，不要任何解释文字，格式: "
            '{"face": true 或 false, "confidence": 0到1之间的小数, "reason": 简短中文理由}'
        ))

    def ask_vision(self, frame, prompt: str) -> dict | None:
        """通用视觉问答：单帧 + 自定义 prompt，返回解析后的 JSON dict 或 None。"""
        if not self.enabled:
            return None

        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpg_quality])
        if not ok:
            return None
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": config.LLM_MAX_TOKENS,
            "temperature": 0,
        }

        for attempt in range(3):
            if STOP.flag:
                return None
            with self._lock:
                self.calls += 1
            status, text = self._post(payload)
            if status == 200:
                try:
                    obj = json.loads(text)
                    msg = ((obj.get("choices") or [{}])[0]).get("message") or {}
                    content = msg.get("content")
                    finish = ((obj.get("choices") or [{}])[0]).get("finish_reason")
                    # 推理模型：finish_reason=length 时 content 被 reasoning_content 吃光
                    if (not content or not str(content).strip()) and finish == "length":
                        payload = dict(payload, max_tokens=config.LLM_MAX_TOKENS_RETRY)
                        continue
                    parsed = self._extract_json(str(content or ""))
                    if parsed is not None:
                        with self._lock:
                            self._consec_fail = 0
                        return parsed
                    payload = dict(payload, max_tokens=config.LLM_MAX_TOKENS_RETRY)
                    continue
                except json.JSONDecodeError:
                    pass
                with self._lock:
                    self.failures += 1
                    self._consec_fail += 1
                continue

            # 4xx（除可重试状态）直接放弃，不重试
            if 400 <= status < 500 and status not in config.LLM_RETRY_STATUS:
                with self._lock:
                    self.failures += 1
                    self._consec_fail += 1
                log(f"[LLM] HTTP {status}，放弃重试")
                break

            if attempt < 2:
                time.sleep(config.LLM_BACKOFF[attempt])
            with self._lock:
                self.failures += 1
                self._consec_fail += 1

        with self._lock:
            if self._consec_fail >= config.LLM_MAX_CONSEC_FAIL:
                self._disable("连续失败")
        return None


def make_llm(name: str, url: str | None = None, key: str | None = None,
             model: str | None = None, enabled: bool = True) -> LLMClient:
    """按 --gateway 预设 + --llm-* 覆盖构造 LLMClient。"""
    u, k, m = config.apply_llm_overrides(name, url, key, model)
    return LLMClient(u, k, m, enabled=enabled)
