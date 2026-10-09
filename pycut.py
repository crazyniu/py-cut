#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pycut 统一入口（所有功能的命令行入口，后续演进为 TUI）。

用法：
    python3 pycut.py slice --file "视频名" --dry-run     # 试跑切片（只分段不产出）
    python3 pycut.py slice --gateway deepseek            # 批量切片（deepseek 网关）
    python3 pycut.py cover --all --gateway deepseek      # 补跑字幕擦除
    python3 -m pycut slice ...                            # 等价入口

说明：
    - 必须用自带 cv2 的解释器运行（/Users/apple/.workbuddy/binaries/python/.../python3）。
    - 不要进 venv（会丢失 cv2）。
"""
import sys

from pycut.cli import main

if __name__ == "__main__":
    sys.exit(main())
