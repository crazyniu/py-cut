"""OpenAI-compatible vision client using standard-library urllib.

Reasoning models need an adequate output-token budget. Count unusable responses
as failures; switch to a configured backup after consecutive failures and retain
that delegate for the rest of the client lifetime."""
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
    """One gateway client with optional sticky failover after LLM_FAILOVER_FAILS failures."""

    def __init__(self, url: str, key: str, model: str, enabled: bool = True,
                 label: str = "main", fallback: "LLMClient | None" = None):
        self.url = url.rstrip("/") + "/chat/completions"
        self.key = key
        self.model = model
        self.enabled = enabled
        self.label = label
        self.fallback = fallback
        self.switched_to: str | None = None
        self._calls = 0
        self._failures = 0
        self._delegate: LLMClient | None = None
        self._consec_fail = 0
        self._lock = threading.Lock()
        self._jpg_quality = 80

    @property
    def calls(self) -> int:
        return self._calls + (self._delegate.calls if self._delegate is not None else 0)

    @property
    def failures(self) -> int:
        return self._failures + (self._delegate.failures if self._delegate is not None else 0)

    def _disable(self, why: str) -> None:
        with self._lock:
            if self.enabled:
                self.enabled = False
                log(f"[LLM] 连续失败 {config.LLM_MAX_CONSEC_FAIL} 次，全局禁用 LLM（{why}）")

    def _post(self, payload: dict, *, timeout: float | None = None) -> tuple[int, str]:
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
        until = time.monotonic() + timeout if timeout is not None else None
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
                if until is None:
                    return resp.status, resp.read().decode("utf-8", "replace")
                chunks = []
                while not STOP.flag and time.monotonic() < until:
                    chunk = resp.read1(65536)
                    if not chunk:
                        return resp.status, b"".join(chunks).decode("utf-8", "replace")
                    chunks.append(chunk)
                return -1, "识别请求超时或已中断"
        except urllib.error.HTTPError as e:
            # Error bodies are unnecessary for retry decisions and can also stall.
            e.close()
            return e.code, ""
        except Exception as e:  # noqa: BLE001
            return -1, str(e)

    def _extract_json(self, text: str) -> dict | None:
        """Parse JSON after removing Markdown fences, with brace and regex fallbacks."""
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
        """Return {face, confidence, reason}, or None when vision classification fails."""
        return self.ask_vision(frame, (
            "判断这张视频截图里是否清晰可见至少一个人的脸（大头/半身/全身有人脸均可）。"
            "只回答 JSON，不要任何解释文字，格式: "
            '{"face": true 或 false, "confidence": 0到1之间的小数, "reason": 简短中文理由}'
        ))

    def ask_vision(self, frame, prompt: str, *, deadline: float | None = None) -> dict | None:
        """Send one or more frames and a prompt; return a parsed JSON dict or None."""
        # Sticky failover prevents subsequent requests from oscillating between gateways.
        if self._delegate is not None:
            return self._delegate.ask_vision(frame, prompt, deadline=deadline)
        if not self.enabled:
            return None

        frames = frame if isinstance(frame, (list, tuple)) else [frame]
        if not frames:
            return None
        content = [{"type": "text", "text": prompt}]
        for image in frames:
            ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpg_quality])
            if not ok:
                return None
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "max_tokens": config.LLM_MAX_TOKENS_RETRY if len(frames) > 1 else config.LLM_MAX_TOKENS,
            "temperature": 0,
        }

        for attempt in range(3):
            if STOP.flag or not self.enabled:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                break
            with self._lock:
                self._calls += 1
            status, text = (self._post(payload) if deadline is None else
                            self._post(payload, timeout=max(0.01, min(config.HTTP_TIMEOUT, 15.0, deadline - time.monotonic()))))
            if STOP.flag:
                return None
            if deadline is not None and time.monotonic() >= deadline:
                with self._lock:
                    self._failures += 1
                    self._consec_fail += 1
                break
            if status == 200:
                try:
                    obj = json.loads(text)
                    choices = obj.get("choices") or [{}]
                    first = choices[0]
                    msg = first.get("message") or {}
                    content = msg.get("content")
                    finish = first.get("finish_reason")
                    # HTTP 200 with empty/invalid model content still counts toward failover.
                    if (not content or not str(content).strip()) and finish == "length":
                        payload = dict(payload, max_tokens=config.LLM_MAX_TOKENS_RETRY)
                    else:
                        parsed = self._extract_json(str(content or ""))
                        if parsed is not None:
                            with self._lock:
                                self._consec_fail = 0
                            return parsed
                        payload = dict(payload, max_tokens=config.LLM_MAX_TOKENS_RETRY)
                except (json.JSONDecodeError, TypeError, AttributeError, IndexError, KeyError):
                    pass
                with self._lock:
                    self._failures += 1
                    self._consec_fail += 1
                continue

            # Do not retry non-retryable 4xx responses.
            if 400 <= status < 500 and status not in config.LLM_RETRY_STATUS:
                with self._lock:
                    self._failures += 1
                    self._consec_fail += 1
                log(f"[LLM] HTTP {status}，放弃重试")
                break

            if attempt < 2:
                until = time.monotonic() + config.LLM_BACKOFF[attempt]
                if deadline is not None:
                    until = min(until, deadline)
                while not STOP.flag and time.monotonic() < until:
                    time.sleep(max(0, min(0.1, until - time.monotonic())))
            with self._lock:
                self._failures += 1
                self._consec_fail += 1

        with self._lock:
            consec = self._consec_fail
        if consec >= config.LLM_MAX_CONSEC_FAIL:
            self._disable("连续失败")

        # Retry this request on the backup after consecutive primary failures.
        fb = self.fallback
        if fb is not None and fb.enabled and consec >= config.LLM_FAILOVER_FAILS:
            log(f"[LLM] {self.label} 网关连续失败 {consec} 次，"
                f"自动切换备用网关（{config.gateway_summary(fb.label)}）")
            self._delegate = fb
            self.switched_to = fb.label
            self.fallback = None
            return fb.ask_vision(frame, prompt, deadline=deadline)
        return None


def make_llm(name: str, url: str | None = None, key: str | None = None,
             model: str | None = None, enabled: bool = True,
             use_fallback: bool = True) -> LLMClient:
    """Build a client from gateway defaults and explicit URL/key/model overrides.

    Attach a configured backup only when fallback is enabled and no explicit override
    was supplied; explicit endpoint choices must not silently switch gateways."""
    n = name if name in config.GATEWAYS else config.DEFAULT_GATEWAY
    u, k, m = config.apply_llm_overrides(n, url, key, model)

    fb: LLMClient | None = None
    explicit = bool(url or key or model)
    if use_fallback and not explicit:
        other = config.other_gateway(n)
        if config.key_ready(other):
            ou, ok_, om = config.apply_llm_overrides(other)
            fb = LLMClient(ou, ok_, om, enabled=enabled, label=other)
        else:
            log(f"[LLM] 备用网关 {other} 未配置密钥，不启用自动切换")
    return LLMClient(u, k, m, enabled=enabled, label=n, fallback=fb)
