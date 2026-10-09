#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pycut 统一入口（所有功能的命令行入口，后续演进为 TUI）。

用法：
    ./run.sh slice --file "视频名" --dry-run  # 试跑切片（只分段不产出）
    ./run.sh slice                            # 批量切片（默认走主网关）
    ./run.sh cover --all                      # 补跑字幕擦除
    python3 -m pycut slice ...                # 等价入口

说明：
    - 必须用自带 cv2 的解释器运行（/Users/apple/.workbuddy/binaries/python/.../python3），
      日常一律用 ./run.sh（会自动选解释器、装依赖、读 .env）。
    - 不要进 venv（会丢失 cv2）。
    - 网关只有主(main)/备(backup)两个，在 .env 配置；主网关默认 DeepSeek。
"""
import sys

from pycut.cli import main

if __name__ == "__main__":
    sys.exit(main())
