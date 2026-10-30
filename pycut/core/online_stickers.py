"""Download temporary Twemoji PNG stickers and retain CC BY 4.0 attribution."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

import cv2
import numpy as np

from .runtime import check_stop
from .utils import log

SOURCES = (
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/1f42d.png",
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/2728.png",
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/1f31f.png",
)
MAX_BYTES = 2 * 1024 * 1024
DOWNLOAD_ROUNDS = 2
ATTRIBUTION = {
    "author": "Twitter, Inc and other contributors",
    "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
}


def _download(url: str) -> tuple[bytes, str]:
    """Try bounded requests to two hosts serving the same pinned Twemoji asset."""
    mirror = url.replace("https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/",
                         "https://cdn.jsdelivr.net/gh/twitter/twemoji@v14.0.2/")
    urls = (url, mirror)
    failures = []
    for attempt in range(DOWNLOAD_ROUNDS):
        for source in urls:
            check_stop()
            host = urlsplit(source).hostname
            log(f"  [贴纸下载] {host} · {url.rsplit('/', 1)[-1]} · 第 {attempt + 1}/{DOWNLOAD_ROUNDS} 轮")
            request = urllib.request.Request(source, headers={"User-Agent": "pycut/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = response.read(MAX_BYTES + 1)
                check_stop()
                if len(data) > MAX_BYTES:
                    raise ValueError("在线贴纸超过 2 MiB")
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("在线贴纸不是有效 PNG")
                image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                if image is None:
                    raise ValueError("在线贴纸不是有效 PNG")
                return data, source
            except InterruptedError:
                raise
            except (OSError, http.client.HTTPException, ValueError) as exc:
                detail = f"{host}: {type(exc).__name__}: {exc}"
                failures.append(detail)
                log(f"  [贴纸下载重试] {detail}")
    message = "；".join(failures)
    raise ValueError(f"贴纸 {url.rsplit('/', 1)[-1]} 下载尝试已耗尽：{message}")


def download_stickers(directory: Path) -> list[Path]:
    """Fetch fresh validated stickers; exhausted retries reject publication."""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    sources = []
    for index, url in enumerate(SOURCES):
        data, source = _download(url)
        sources.append(source)
        destination = directory / f"{index}.png"
        temporary = destination.with_suffix(".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        files.append(destination)
    (directory / "credits.json").write_text(json.dumps({
        **ATTRIBUTION,
        "sources": sources,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return files
