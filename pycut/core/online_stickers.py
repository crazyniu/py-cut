"""生成时下载 Twemoji PNG 贴纸（CC BY 4.0），记录来源与许可。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request

import cv2
import numpy as np

SOURCES = (
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/1f42d.png",
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/2728.png",
    "https://raw.githubusercontent.com/twitter/twemoji/v14.0.2/assets/72x72/1f31f.png",
)
MAX_BYTES = 2 * 1024 * 1024
ATTRIBUTION = {
    "author": "Twitter, Inc and other contributors",
    "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
}


def download_stickers(directory: Path) -> list[Path]:
    """每次请求原站，不用旧缓存；任一下载失败则让调用方拒绝发布。"""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for index, url in enumerate(SOURCES):
        request = urllib.request.Request(url, headers={"User-Agent": "pycut/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("在线贴纸超过 2 MiB")
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("在线贴纸不是有效 PNG")
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
        "sources": list(SOURCES),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return files
